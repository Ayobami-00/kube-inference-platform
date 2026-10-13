#!/usr/bin/env python3
"""Target-local WireGuard mesh setup; private keys never leave the node.

The orchestrator sends a reviewed, public-key-only JSON peer plan on stdin.
Only /32 routes for the other pilot nodes are installed; the host default route
is deliberately untouched. The persistent private key is mode 0600 and is
never included in terminal output, arguments, reports, or inventory.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MESH_NET = ipaddress.IPv4Network("10.200.0.0/24")
WG_PORT = 51820
WG_MTU = 1380
WG_HOME = Path(os.environ.get("KIP_WG_HOME", "/etc/wireguard"))
KEY_PATH = WG_HOME / "kip.key"
CONFIG_PATH = WG_HOME / "wg-kip.conf"
HOSTS_PATH = Path(os.environ.get("KIP_HOSTS_PATH", "/etc/hosts"))
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
API_HOST = "api.kip.internal"
HOSTS_MARKER = "# KIP-managed pilot API"


class MeshError(Exception):
    """Unsafe or unavailable private-mesh state."""


def _run(*args: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MeshError(f"mesh command failed: {args[0]}") from error
    return result.stdout.strip()


def _public_key(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 44 or "\n" in value:
        raise MeshError("WireGuard peer public key has invalid encoding")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise MeshError("WireGuard peer public key is not base64") from error
    if len(decoded) != 32:
        raise MeshError("WireGuard peer public key must be 32 bytes")
    return value


def _mesh_ip(value: Any) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise MeshError("mesh address must be IPv4") from error
    if address not in MESH_NET or address in {MESH_NET.network_address, MESH_NET.broadcast_address}:
        raise MeshError("mesh address is outside the reviewed private /24")
    return address


def validate_plan(raw: Any) -> tuple[ipaddress.IPv4Address, list[dict[str, Any]]]:
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "node", "peers"}:
        raise MeshError("mesh plan fields differ from reviewed schema")
    if raw["schemaVersion"] != 1 or not isinstance(raw["node"], dict):
        raise MeshError("mesh plan schema is unsupported")
    if set(raw["node"]) != {"name", "privateIpv4"}:
        raise MeshError("mesh node fields are invalid")
    node_name = raw["node"]["name"]
    if not isinstance(node_name, str) or not NAME.fullmatch(node_name):
        raise MeshError("mesh node name is invalid")
    own_ip = _mesh_ip(raw["node"]["privateIpv4"])
    peers = raw["peers"]
    if not isinstance(peers, list) or not 1 <= len(peers) <= 3:
        raise MeshError("mesh plan requires one to three peers")
    seen_names = {node_name}
    seen_ips = {own_ip}
    seen_keys: set[str] = set()
    validated: list[dict[str, Any]] = []
    for peer in peers:
        if not isinstance(peer, dict) or set(peer) != {
            "name",
            "privateIpv4",
            "publicKey",
            "endpointIpv4",
            "endpointPort",
        }:
            raise MeshError("mesh peer fields are invalid")
        name = peer["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen_names:
            raise MeshError("mesh peer name is invalid or duplicated")
        private_ip = _mesh_ip(peer["privateIpv4"])
        if private_ip in seen_ips:
            raise MeshError("mesh peer address is duplicated")
        key = _public_key(peer["publicKey"])
        if key in seen_keys:
            raise MeshError("mesh peer public key is duplicated")
        try:
            endpoint = ipaddress.IPv4Address(peer["endpointIpv4"])
        except (ValueError, TypeError) as error:
            raise MeshError("mesh peer endpoint must be IPv4") from error
        port = peer["endpointPort"]
        if (
            not endpoint.is_global
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise MeshError("mesh peer endpoint or UDP port is unsafe")
        seen_names.add(name)
        seen_ips.add(private_ip)
        seen_keys.add(key)
        validated.append(
            {
                "name": name,
                "privateIpv4": private_ip,
                "publicKey": key,
                "endpointIpv4": endpoint,
                "endpointPort": port,
            }
        )
    validated.sort(key=lambda peer: str(peer["privateIpv4"]))
    if ipaddress.IPv4Address("10.200.0.1") not in seen_ips:
        raise MeshError("mesh plan does not contain the control-plane API address")
    return own_ip, validated


def _atomic_secret(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise MeshError("mesh setup must run as root on the target VM")
    if WG_HOME.is_symlink() or KEY_PATH.is_symlink() or CONFIG_PATH.is_symlink():
        raise MeshError("WireGuard paths must not be symlinks")


def _ensure_wireguard() -> None:
    if shutil.which("wg") and shutil.which("wg-quick"):
        return
    _run("apt-get", "update", "-qq")
    _run("apt-get", "install", "-y", "--no-install-recommends", "wireguard-tools")
    if not shutil.which("wg") or not shutil.which("wg-quick"):
        raise MeshError("wireguard-tools installation did not provide wg and wg-quick")


def ensure_key() -> str:
    _require_root()
    _ensure_wireguard()
    if not KEY_PATH.exists():
        _atomic_secret(KEY_PATH, _run("wg", "genkey") + "\n")
    if KEY_PATH.stat().st_mode & 0o077:
        raise MeshError("existing WireGuard key permissions must be 0600 or stricter")
    private = KEY_PATH.read_text(encoding="ascii").strip()
    return _public_key(_run("wg", "pubkey", input_text=private + "\n"))


def render_config(own_ip: ipaddress.IPv4Address, peers: list[dict[str, Any]], private: str) -> str:
    lines = [
        "# KIP-managed WireGuard pilot mesh; do not publish this file",
        "[Interface]",
        f"Address = {own_ip}/32",
        f"ListenPort = {WG_PORT}",
        f"PrivateKey = {private}",
        f"MTU = {WG_MTU}",
    ]
    for peer in peers:
        lines.extend(
            [
                "",
                f"# {peer['name']}",
                "[Peer]",
                f"PublicKey = {peer['publicKey']}",
                f"AllowedIPs = {peer['privateIpv4']}/32",
                f"Endpoint = {peer['endpointIpv4']}:{peer['endpointPort']}",
                "PersistentKeepalive = 25",
            ]
        )
    return "\n".join(lines) + "\n"


def _ensure_api_hosts() -> None:
    if HOSTS_PATH.is_symlink() or not HOSTS_PATH.is_file():
        raise MeshError("host resolver file is missing or a symlink")
    current = HOSTS_PATH.read_text(encoding="utf-8")
    lines = current.splitlines()
    managed = [line for line in lines if HOSTS_MARKER in line]
    if len(managed) > 1:
        raise MeshError("multiple KIP API resolver entries exist")
    for line in lines:
        tokens = line.split("#", 1)[0].split()
        if API_HOST in tokens[1:] and line not in managed:
            raise MeshError("unmanaged api.kip.internal resolver entry exists")
    desired_line = f"10.200.0.1 {API_HOST} {HOSTS_MARKER}"
    if managed == [desired_line]:
        return
    updated = "\n".join(line for line in lines if line not in managed)
    updated = updated.rstrip("\n") + "\n" + desired_line + "\n"
    original_mode = HOSTS_PATH.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(prefix=".kip-hosts.", dir=HOSTS_PATH.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, original_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, HOSTS_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _check_hostname(name: str) -> str:
    current = _run("hostname")
    if current == name:
        return current
    if current != "ubuntu":
        raise MeshError("unrelated hostname exists; refusing pilot identity change")
    if Path("/etc/kubernetes/admin.conf").exists() or Path("/etc/kubernetes/kubelet.conf").exists():
        raise MeshError("destroy the old single-node cluster before renaming this VM")
    return current


def _ensure_hostname(name: str) -> None:
    if _check_hostname(name) == name:
        return
    _run("hostnamectl", "set-hostname", name)
    if _run("hostname") != name:
        raise MeshError("systemd did not apply the reviewed fleet hostname")


def configure(raw: Any, *, link_only: bool = False) -> dict[str, Any]:
    own_ip, peers = validate_plan(raw)
    node_name = raw["node"]["name"]
    if not link_only:
        _check_hostname(node_name)
    public = ensure_key()
    private = KEY_PATH.read_text(encoding="ascii").strip()
    desired = render_config(own_ip, peers, private)
    if CONFIG_PATH.exists():
        current = CONFIG_PATH.read_text(encoding="ascii")
        if current == desired:
            _run("systemctl", "is-active", "--quiet", "wg-quick@wg-kip")
            if not link_only:
                _ensure_api_hosts()
                _ensure_hostname(node_name)
            return {
                "status": "unchanged",
                "address": str(own_ip),
                "peerCount": len(peers),
                "publicKey": public,
            }
        if not current.startswith("# KIP-managed WireGuard pilot mesh;"):
            raise MeshError("unmanaged WireGuard interface configuration exists")
    _atomic_secret(CONFIG_PATH, desired)
    _run("systemctl", "enable", "wg-quick@wg-kip")
    _run("systemctl", "restart", "wg-quick@wg-kip")
    _run("systemctl", "is-active", "--quiet", "wg-quick@wg-kip")
    if not link_only:
        _ensure_api_hosts()
        _ensure_hostname(node_name)
    return {
        "status": "link-configured" if link_only else "configured",
        "address": str(own_ip),
        "peerCount": len(peers),
        "publicKey": public,
    }


def check(raw: Any) -> dict[str, Any]:
    """Read-only handshake, packet-loss, and 1348-byte path-MTU gate."""
    own_ip, peers = validate_plan(raw)
    if not CONFIG_PATH.is_file() or not CONFIG_PATH.read_text(encoding="ascii").startswith(
        "# KIP-managed WireGuard pilot mesh"
    ):
        raise MeshError("owned WireGuard interface is absent")
    _run("systemctl", "is-active", "--quiet", "wg-quick@wg-kip")
    address_payload = json.loads(_run("ip", "-j", "-4", "addr", "show", "dev", "wg-kip"))
    if not isinstance(address_payload, list) or len(address_payload) != 1:
        raise MeshError("WireGuard interface address is unavailable")
    addresses = address_payload[0].get("addr_info", [])
    if not any(isinstance(item, dict) and item.get("local") == str(own_ip) for item in addresses):
        raise MeshError("WireGuard interface has the wrong private node address")
    handshake_lines = _run("wg", "show", "wg-kip", "latest-handshakes").splitlines()
    handshakes: dict[str, int] = {}
    for line in handshake_lines:
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdigit():
            raise MeshError("WireGuard handshake data is malformed")
        handshakes[fields[0]] = int(fields[1])
    now = int(time.time())
    result = []
    for peer in peers:
        key = peer["publicKey"]
        age = now - handshakes.get(key, 0)
        if age < 0 or age > 120:
            raise MeshError(f"WireGuard handshake is absent or stale for {peer['name']}")
        address = str(peer["privateIpv4"])
        ping_output = _run("ping", "-n", "-c", "3", "-W", "2", address)
        if re.search(r"(?<![0-9])0% packet loss\b", ping_output) is None:
            raise MeshError(f"private path has packet loss to {peer['name']}")
        _run("ping", "-n", "-M", "do", "-s", "1320", "-c", "1", "-W", "2", address)
        result.append(
            {
                "name": peer["name"],
                "privateIpv4": address,
                "handshakeAgeSeconds": age,
                "packetLossPercent": 0,
                "mtuPayloadBytes": 1320,
            }
        )
    return {"status": "healthy", "node": raw["node"]["name"], "peers": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="KIP target-local private WireGuard mesh")
    parser.add_argument("action", choices=("key", "link", "configure", "check"))
    args = parser.parse_args()
    try:
        if args.action == "key":
            result = {"publicKey": ensure_key()}
        elif args.action in {"link", "configure"}:
            result = configure(json.load(sys.stdin), link_only=args.action == "link")
        else:
            result = check(json.load(sys.stdin))
    except (MeshError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
