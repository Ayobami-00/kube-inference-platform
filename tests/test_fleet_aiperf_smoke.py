"""Pilot text smoke delegates load to AIPerf without exposing tenant keys."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from fleet import aiperf_smoke
from fleet.vast import FleetError


def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    # Build the full cross-product without putting any real token in a fixture.
    path.write_text(
        json.dumps(
            {
                tenant: {
                    workload_class: f"synthetic-token-{tenant}-{workload_class}"
                    for workload_class in aiperf_smoke.CLASSES
                }
                for tenant in aiperf_smoke.TENANTS
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def test_credential_requires_private_complete_file(tmp_path: Path) -> None:
    path = credential_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert aiperf_smoke._credential(path, "tenant-a", "chat").startswith("synthetic-token-")
    os.chmod(path, 0o644)
    with pytest.raises(FleetError, match="0600"):
        aiperf_smoke._credential(path, "tenant-a", "chat")


def test_smoke_invokes_aiperf_without_token_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = credential_file(tmp_path)
    config = tmp_path / "benchmarks" / "campaigns" / "pilot-smoke.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("schemaVersion: '2.0'\n", encoding="utf-8")
    monkeypatch.setattr(aiperf_smoke, "_gateway_url", lambda: "http://10.100.38.114:80")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update({"command": command, "env": kwargs["env"]})
        artifacts = Path(kwargs["env"]["KIP_PILOT_ARTIFACTS"])
        artifacts.mkdir(parents=True)
        (artifacts / "profile_export_aiperf.json").write_text(
            json.dumps(
                {
                    "aiperf_version": "0.12.0",
                    "was_cancelled": False,
                    "completed_request_count": None,
                    "request_count": {"avg": 24},
                    "error_request_count": None,
                    "error_summary": [],
                    "time_to_first_token": {"count": 24},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(aiperf_smoke.subprocess, "run", fake_run)
    summary = aiperf_smoke._run("tenant-a", "chat", credentials_path=token_file, root=tmp_path)
    token = aiperf_smoke._credential(token_file, "tenant-a", "chat")
    assert summary.name == "profile_export_aiperf.json"
    assert token not in " ".join(captured["command"])
    assert captured["env"]["KIP_PILOT_TOKEN"] == token
    assert captured["command"][5:7] == ["aiperf", "profile"]
