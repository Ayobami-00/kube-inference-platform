"""Private mesh plans are deterministic, narrow, and cannot leak a node key."""

from __future__ import annotations

import base64
import ipaddress
import json
from pathlib import Path

import pytest

from deploy.fleet import mesh

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(1, 33))).decode()


def plan() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "node": {"name": "kip-control", "privateIpv4": "10.200.0.1"},
        "peers": [
            {
                "name": "kip-gpu-2",
                "privateIpv4": "10.200.0.3",
                "publicKey": KEY_B,
                "endpointIpv4": "203.0.113.11",
                "endpointPort": 58051,
            },
            {
                "name": "kip-gpu-1",
                "privateIpv4": "10.200.0.2",
                "publicKey": KEY_A,
                "endpointIpv4": "198.51.100.10",
                "endpointPort": 58050,
            },
        ],
    }


def test_mesh_renders_only_narrow_peer_routes_and_stable_order() -> None:
    raw = plan()
    # Documentation-only IP blocks are not globally routable in ipaddress;
    # the production plan requires real provider endpoints.
    raw["peers"][0]["endpointIpv4"] = "8.8.8.8"  # type: ignore[index]
    raw["peers"][1]["endpointIpv4"] = "1.1.1.1"  # type: ignore[index]
    own, peers = mesh.validate_plan(raw)
    assert own == ipaddress.IPv4Address("10.200.0.1")
    config = mesh.render_config(own, peers, "private-fixture")
    assert config.index("# kip-gpu-1") < config.index("# kip-gpu-2")
    assert "AllowedIPs = 10.200.0.2/32" in config
    assert "AllowedIPs = 10.200.0.3/32" in config
    assert "0.0.0.0/0" not in config
    assert "MTU = 1380" in config


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda x: x.update({"unexpected": True}), "fields"),
        (lambda x: x["node"].update({"privateIpv4": "10.200.1.1"}), "outside"),
        (lambda x: x["peers"][0].update({"privateIpv4": "10.200.0.1"}), "duplicated"),
        (lambda x: x["peers"][0].update({"publicKey": "bad\nkey"}), "encoding"),
        (lambda x: x["peers"][0].update({"endpointIpv4": "127.0.0.1"}), "unsafe"),
        (lambda x: x["peers"][0].update({"endpointPort": 0}), "unsafe"),
        (lambda x: x["peers"][0].update({"name": "bad\n[Interface]"}), "name"),
    ],
)
def test_mesh_rejects_unsafe_topology(change: object, message: str) -> None:
    raw = plan()
    change(raw)  # type: ignore[operator]
    with pytest.raises(mesh.MeshError, match=message):
        mesh.validate_plan(raw)


def test_mesh_configuration_keeps_private_key_on_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = plan()
    raw["peers"][0]["endpointIpv4"] = "8.8.8.8"  # type: ignore[index]
    raw["peers"][1]["endpointIpv4"] = "1.1.1.1"  # type: ignore[index]
    home = tmp_path / "wireguard"
    monkeypatch.setattr(mesh, "WG_HOME", home)
    monkeypatch.setattr(mesh, "KEY_PATH", home / "kip.key")
    monkeypatch.setattr(mesh, "CONFIG_PATH", home / "wg-kip.conf")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    monkeypatch.setattr(mesh, "HOSTS_PATH", hosts)
    monkeypatch.setattr(mesh, "_require_root", lambda: None)
    monkeypatch.setattr(mesh, "_ensure_wireguard", lambda: None)
    commands: list[tuple[str, ...]] = []
    hostname = ["ubuntu"]

    def fake_run(*args: str, input_text: str | None = None) -> str:
        commands.append(args)
        if args == ("wg", "genkey"):
            return KEY_A
        if args == ("wg", "pubkey"):
            assert input_text == KEY_A + "\n"
            return KEY_B
        if args == ("hostname",):
            return hostname[0]
        if args == ("hostnamectl", "set-hostname", "kip-control"):
            hostname[0] = "kip-control"
        return ""

    monkeypatch.setattr(mesh, "_run", fake_run)
    first = mesh.configure(raw, link_only=True)
    assert first == {
        "status": "link-configured",
        "address": "10.200.0.1",
        "peerCount": 2,
        "publicKey": KEY_B,
    }
    assert KEY_A not in json.dumps(first)
    assert (home / "kip.key").stat().st_mode & 0o777 == 0o600
    assert (home / "wg-kip.conf").stat().st_mode & 0o777 == 0o600
    assert "PrivateKey = " + KEY_A in (home / "wg-kip.conf").read_text()
    assert "api.kip.internal" not in hosts.read_text()
    assert hostname[0] == "ubuntu"
    assert mesh.configure(raw)["status"] == "unchanged"
    assert "10.200.0.1 api.kip.internal # KIP-managed pilot API" in hosts.read_text()
    assert hostname[0] == "kip-control"
    assert mesh.configure(raw)["status"] == "unchanged"
    assert sum(args[:2] == ("systemctl", "restart") for args in commands) == 1
    raw["peers"][0]["endpointIpv4"] = "8.8.4.4"  # type: ignore[index]
    assert mesh.configure(raw, link_only=True)["status"] == "link-configured"
    assert "Endpoint = 8.8.4.4:58051" in (home / "wg-kip.conf").read_text()
    assert sum(args[:2] == ("systemctl", "restart") for args in commands) == 2


def test_mesh_refuses_unmanaged_private_api_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text("10.200.0.99 api.kip.internal\n")
    monkeypatch.setattr(mesh, "HOSTS_PATH", hosts)
    with pytest.raises(mesh.MeshError, match="unmanaged"):
        mesh._ensure_api_hosts()
    assert hosts.read_text() == "10.200.0.99 api.kip.internal\n"


def test_mesh_check_requires_current_handshakes_zero_loss_and_path_mtu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = plan()
    raw["peers"][0]["endpointIpv4"] = "8.8.8.8"  # type: ignore[index]
    raw["peers"][1]["endpointIpv4"] = "1.1.1.1"  # type: ignore[index]
    config = tmp_path / "wg-kip.conf"
    config.write_text("# KIP-managed WireGuard pilot mesh\n")
    monkeypatch.setattr(mesh, "CONFIG_PATH", config)
    monkeypatch.setattr(mesh.time, "time", lambda: 1000)
    loss = ["0% packet loss"]

    def fake_run(*args: str, input_text: str | None = None) -> str:
        if args[:2] == ("ip", "-j"):
            return json.dumps([{"addr_info": [{"local": "10.200.0.1"}]}])
        if args[:3] == ("wg", "show", "wg-kip"):
            return f"{KEY_A} 950\n{KEY_B} 950"
        if args[0] == "ping":
            return f"3 packets transmitted, 3 received, {loss[0]}, time 20ms"
        return ""

    monkeypatch.setattr(mesh, "_run", fake_run)
    result = mesh.check(raw)
    assert result["status"] == "healthy"
    assert len(result["peers"]) == 2
    assert all(peer["handshakeAgeSeconds"] == 50 for peer in result["peers"])
    loss[0] = "100% packet loss"
    with pytest.raises(mesh.MeshError, match="packet loss"):
        mesh.check(raw)
