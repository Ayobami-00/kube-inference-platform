"""Worker join rejects malformed credentials before touching host state."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "deploy/fleet/worker-join.sh"


def invoke(plan: dict[str, object] | None, **environment: str) -> subprocess.CompletedProcess[str]:
    values = {**os.environ, "NO_COLOR": "1", **environment}
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        input=json.dumps(plan) if plan is not None else "",
        env=values,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_worker_join_requires_explicit_mesh_identity() -> None:
    result = invoke(None)
    assert result.returncode == 2
    assert "explicit WireGuard NODE_IP" in result.stderr


def test_worker_join_rejects_secret_schema_drift_without_host_mutation() -> None:
    result = invoke(
        {
            "schemaVersion": 1,
            "controlIpv4": "10.200.0.1",
            "token": "abcdef.0123456789abcdef",
            "caHash": "sha256:" + "a" * 64,
            "extra": "not-reviewed",
        },
        KIP_NODE_IP="10.200.0.2",
    )
    assert result.returncode == 2
    assert "exact-schema private join plan" in result.stderr
    assert "apt-get" not in result.stdout + result.stderr
