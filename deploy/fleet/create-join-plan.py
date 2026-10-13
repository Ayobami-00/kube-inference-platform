#!/usr/bin/env python3
"""Emit a short-lived kubeadm join plan only into a protected pipe.

Run on the owned control plane and pipe stdout directly to worker-join.sh on
the target worker. Never invoke interactively or redirect the plan to a log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOKEN = re.compile(r"[a-z0-9]{6}\.[a-z0-9]{16}\Z")
CA_CERT = Path("/etc/kubernetes/pki/ca.crt")


class JoinPlanError(Exception):
    """A protected join plan cannot be created safely."""


def _run(*command: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise JoinPlanError(f"{command[0]} failed") from error
    return result.stdout


def ca_public_key_hash(path: Path = CA_CERT) -> str:
    """Hash the DER SubjectPublicKeyInfo exactly as kubeadm discovery expects."""
    if not path.is_file() or path.is_symlink():
        raise JoinPlanError("control-plane CA certificate is unavailable")
    pem = _run("openssl", "x509", "-pubkey", "-noout", "-in", str(path))
    der = _run("openssl", "pkey", "-pubin", "-outform", "DER", input_bytes=pem)
    if not der:
        raise JoinPlanError("control-plane CA public key is empty")
    return "sha256:" + hashlib.sha256(der).hexdigest()


def create_plan() -> dict[str, object]:
    if os.geteuid() != 0 or _run("hostname").strip() != b"kip-control":
        raise JoinPlanError("run as root on the reviewed kip-control node")
    resolved = _run("getent", "ahostsv4", "api.kip.internal").split()
    if not resolved or resolved[0] != b"10.200.0.1":
        raise JoinPlanError("private API name does not resolve to the mesh control IP")
    ready = _run("kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf", "get", "--raw=/readyz")
    if ready.strip() != b"ok":
        raise JoinPlanError("control-plane API is not ready")
    ca_hash = ca_public_key_hash()
    token = _run("kubeadm", "token", "create", "--ttl", "30m").decode("ascii").strip()
    if not TOKEN.fullmatch(token):
        raise JoinPlanError("kubeadm returned a malformed join token")
    return {"caHash": ca_hash, "controlIpv4": "10.200.0.1", "schemaVersion": 1, "token": token}


def main() -> int:
    if len(sys.argv) != 1 or sys.stdout.isatty():
        print("join plan requires a non-interactive protected stdout pipe", file=sys.stderr)
        return 2
    try:
        plan = create_plan()
        sys.stdout.write(json.dumps(plan, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (JoinPlanError, BrokenPipeError) as error:
        print(f"join plan failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
