"""Replay a reviewed synthetic text trace through the private llm-d Gateway.

AIPerf sends all requests. This wrapper only validates provenance, supplies a
synthetic tenant credential via the child environment, and checks aggregates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.workloads import load_workload_contract

from .aiperf_smoke import MODEL_REPOSITORY, MODEL_REVISION, _credential, _gateway_url
from .vast import FleetError

TEXT_CLASSES = {"chat": "chat", "agentic-style": "agentic", "batch": "batch"}
PROFILES = {"pilot-text-replay", "pilot-text-matrix"}
RUN_ID_PATTERN = re.compile(r"pilot-text-[0-9]{8}-[0-9]{6}(?:-[a-z0-9-]{1,24})?\Z")


def _run_id(value: str | None) -> str:
    """Use one reviewed run identity for independently launched cohorts."""
    candidate = value or datetime.now(UTC).strftime("pilot-text-%Y%m%d-%H%M%S")
    if not RUN_ID_PATTERN.fullmatch(candidate):
        raise FleetError("pilot text run ID must use the reviewed timestamped name format")
    return candidate


def _profile(root: Path, name: str) -> Path:
    if name not in PROFILES:
        raise FleetError("AIPerf profile must be a reviewed text configuration")
    path = root / "benchmarks" / "campaigns" / f"{name}.yaml"
    if not path.is_file() or path.is_symlink():
        raise FleetError("reviewed AIPerf text replay configuration is missing")
    return path


def stream_outcomes(
    path: Path, expected: int, *, allow_rejected: bool = False
) -> tuple[dict[str, int], set[str]]:
    """Inspect only status and stream completion from private AIPerf packets.

    Never return or print raw packets, request headers, prompts, or responses.
    """
    if not path.is_file() or path.is_symlink():
        raise FleetError("AIPerf did not export private raw stream evidence")
    statuses: dict[str, int] = {}
    done_ids: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            identifier = (row.get("metadata") or {}).get("x_request_id")
            responses = row.get("responses")
            if not isinstance(identifier, str) or not identifier or identifier in statuses:
                raise FleetError("AIPerf raw stream evidence is incomplete or duplicated")
            status = row.get("status")
            if status == 429 and allow_rejected:
                if responses not in ([], None):
                    raise FleetError("rejected AIPerf request unexpectedly contains stream packets")
                statuses[identifier] = 429
                continue
            if status != 200 or not isinstance(responses, list) or not responses:
                raise FleetError("AIPerf raw stream evidence is incomplete or duplicated")
            packets = [
                packet
                for response in responses
                if isinstance(response, dict)
                for packet in response.get("packets", [])
                if isinstance(packet, dict) and packet.get("name") == "data"
            ]
            if not packets or sum(packet.get("value") == "[DONE]" for packet in packets) != 1:
                raise FleetError("AIPerf stream lacks exactly one SSE [DONE] marker")
            if packets[-1].get("value") != "[DONE]":
                raise FleetError("AIPerf stream continued after SSE [DONE]")
            statuses[identifier] = 200
            done_ids.add(identifier)
    if len(statuses) != expected:
        raise FleetError("AIPerf raw stream count does not match planned turns")
    return statuses, done_ids


def validate_raw_streams(path: Path, expected: int) -> set[str]:
    """Require one HTTP 200 and final SSE [DONE] for every measured turn."""
    statuses, done_ids = stream_outcomes(path, expected)
    if len(done_ids) != expected or any(status != 200 for status in statuses.values()):
        raise FleetError("AIPerf raw stream count does not match planned turns")
    return done_ids


def _dataset(
    root: Path, tenant: str, workload_class: str, trial: int
) -> tuple[Path, dict[str, Any]]:
    if workload_class not in TEXT_CLASSES or trial < 1:
        raise FleetError("text replay requires a reviewed class and positive trial")
    contract = load_workload_contract()
    path = (
        root
        / "benchmarks"
        / ".work"
        / "datasets"
        / contract.name
        / tenant
        / workload_class
        / f"trial-{trial}.jsonl"
    )
    manifest_path = path.with_suffix(".manifest.json")
    for item in (path, manifest_path):
        if not item.is_file() or item.is_symlink() or stat.S_IMODE(item.stat().st_mode) != 0o600:
            raise FleetError("generated pilot dataset and manifest must be regular mode 0600 files")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("contractSha256") != contract.sha256
        or manifest.get("tenant") != tenant
        or manifest.get("class") != workload_class
        or manifest.get("trial") != trial
        or manifest.get("datasetSha256") != hashlib.sha256(path.read_bytes()).hexdigest()
    ):
        raise FleetError("generated pilot dataset provenance or content has drifted")
    if manifest.get("sessionCount", 0) < 1 or manifest.get("plannedTurnCount", 0) < 1:
        raise FleetError("generated pilot dataset is empty")
    return path, manifest


def _run(
    root: Path,
    *,
    tenant: str,
    workload_class: str,
    trial: int,
    credentials_path: Path,
    run_id: str | None = None,
    profile: str = "pilot-text-replay",
) -> dict[str, Any]:
    path, manifest = _dataset(root, tenant, workload_class, trial)
    gateway = _gateway_url()
    token = _credential(credentials_path, tenant, TEXT_CLASSES[workload_class])
    config = _profile(root, profile)
    run_id = _run_id(run_id)
    work = root / "benchmarks" / ".work" / run_id / f"{tenant}-{workload_class}"
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = work / "raw"
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_PILOT_TOKEN": token,
            "KIP_PILOT_GATEWAY_URL": gateway,
            "KIP_PILOT_DATASET": str(path),
            "KIP_PILOT_DATASET_FORMAT": "single_turn"
            if workload_class == "batch"
            else "multi_turn",
            "KIP_PILOT_ARTIFACTS": str(artifacts),
        }
    )
    command = [
        "uv",
        "run",
        "--group",
        "benchmark",
        "--frozen",
        "aiperf",
        "profile",
        "--config",
        str(config),
        "--tokenizer",
        MODEL_REPOSITORY,
        "--tokenizer-revision",
        MODEL_REVISION,
        "--apply-chat-template",
        "--export-level",
        "raw",
    ]
    log_path = work / "aiperf.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as log:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=1200,
            )
        except subprocess.TimeoutExpired as error:
            raise FleetError(f"AIPerf text replay timed out; see {log_path}") from error
    if result.returncode:
        raise FleetError(f"AIPerf text replay exited {result.returncode}; see {log_path}")
    summary_path = artifacts / "profile_export_aiperf.json"
    if not summary_path.is_file():
        raise FleetError("AIPerf text replay produced no aggregate summary")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expected = manifest["plannedTurnCount"]
    if (
        summary.get("aiperf_version") != "0.12.0"
        or summary.get("was_cancelled")
        or summary.get("error_summary")
        or (summary.get("request_count") or {}).get("avg") != expected
        or (summary.get("time_to_first_token") or {}).get("count") != expected
    ):
        raise FleetError("AIPerf text replay did not complete every planned turn successfully")
    validate_raw_streams(artifacts / "profile_export_raw.jsonl", expected)
    return {
        "status": "PASS",
        "runId": run_id,
        "tenant": tenant,
        "class": workload_class,
        "trial": trial,
        "sessions": manifest["sessionCount"],
        "completedTurns": expected,
        "sseDoneTurns": expected,
        "ttftP95Ms": summary["time_to_first_token"].get("p95"),
        "datasetSha256": manifest["datasetSha256"],
        "summary": str(summary_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a reviewed synthetic AIPerf pilot trace")
    parser.add_argument("--tenant", choices=("tenant-a", "tenant-b", "tenant-c"), required=True)
    parser.add_argument(
        "--class", dest="workload_class", choices=tuple(TEXT_CLASSES), required=True
    )
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--run-id", help="shared timestamped ID for simultaneous reviewed cohorts")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="pilot-text-replay")
    parser.add_argument("--credentials", type=Path, default=Path(".secrets/pilot-tenant-keys.json"))
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parent.parent
        outcome = _run(
            root,
            tenant=args.tenant,
            workload_class=args.workload_class,
            trial=args.trial,
            credentials_path=args.credentials,
            run_id=args.run_id,
            profile=args.profile,
        )
    except (FleetError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"fleet AIPerf text replay failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
