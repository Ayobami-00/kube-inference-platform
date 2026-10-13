"""Run a small authenticated AIPerf checkpoint through the private llm-d Gateway.

This is deliberately not a request load generator: the pinned AIPerf CLI sends
every inference request. Raw prompts, responses, and logs stay under ignored
``benchmarks/.work``; the synthetic tenant token never appears in CLI arguments.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .vast import FleetError

TENANTS = ("tenant-a", "tenant-b", "tenant-c")
CLASSES = ("chat", "agentic", "batch", "voice")
MODEL_REPOSITORY = "Qwen/Qwen2.5-7B-Instruct"
MODEL_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"


def _gateway_url() -> str:
    result = subprocess.run(
        ["kubectl", "-n", "kip-inference", "get", "gateway", "kip-inference-gateway", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise FleetError("private pilot Gateway is unavailable")
    gateway: dict[str, Any] = json.loads(result.stdout)
    if not any(
        condition.get("type") == "Programmed" and condition.get("status") == "True"
        for condition in gateway.get("status", {}).get("conditions", [])
    ):
        raise FleetError("private pilot Gateway is not Programmed")
    addresses = gateway.get("status", {}).get("addresses", [])
    if len(addresses) != 1:
        raise FleetError("private pilot Gateway must have one address")
    address = ipaddress.ip_address(addresses[0]["value"])
    if address.version != 4 or not address.is_private:
        raise FleetError("pilot Gateway address is not private IPv4")
    return f"http://{address}:80"


def _credential(path: Path, tenant: str, workload_class: str) -> str:
    if tenant not in TENANTS or workload_class not in CLASSES:
        raise FleetError("tenant and workload class must be reviewed pilot identities")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600 or path.is_symlink():
        raise FleetError("synthetic tenant credential file must be regular mode 0600")
    credentials = json.loads(path.read_text(encoding="utf-8"))
    if set(credentials) != set(TENANTS) or any(
        set(credentials[item]) != set(CLASSES) for item in TENANTS
    ):
        raise FleetError("synthetic tenant credential set is incomplete")
    token = credentials[tenant][workload_class]
    if not isinstance(token, str) or len(token) < 16:
        raise FleetError("synthetic tenant credential is invalid")
    return token


def _run(tenant: str, workload_class: str, *, credentials_path: Path, root: Path) -> Path:
    gateway = _gateway_url()
    token = _credential(credentials_path, tenant, workload_class)
    config = root / "benchmarks" / "campaigns" / "pilot-smoke.yaml"
    if not config.is_file():
        raise FleetError("reviewed AIPerf pilot smoke configuration is missing")
    run_id = datetime.now(tz=timezone.utc).strftime("pilot-smoke-%Y%m%d-%H%M%S")  # noqa: UP017
    work = root / "benchmarks" / ".work" / run_id / f"{tenant}-{workload_class}"
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = work / "raw"
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_PILOT_TOKEN": token,
            "KIP_PILOT_GATEWAY_URL": gateway,
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
            raise FleetError(
                f"AIPerf exceeded 20 minutes; raw log remains at {log_path}"
            ) from error
    if result.returncode:
        raise FleetError(f"AIPerf exited {result.returncode}; raw log remains at {log_path}")
    summary = artifacts / "profile_export_aiperf.json"
    if not summary.is_file():
        raise FleetError("AIPerf completed without its root profiling summary")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    if payload.get("aiperf_version") != "0.12.0" or payload.get("was_cancelled"):
        raise FleetError("AIPerf profiling summary has drifted or was cancelled")
    completed = (payload.get("completed_request_count") or {}).get("avg")
    successful = (payload.get("request_count") or {}).get("avg")
    failed = (payload.get("error_request_count") or {}).get("avg")
    # AIPerf 0.12 emits completed/error count metrics only when errors occur.
    if (
        completed not in (None, 24)
        or successful != 24
        or failed not in (None, 0)
        or payload.get("error_summary")
    ):
        raise FleetError("AIPerf did not complete all 24 measured requests successfully")
    if payload.get("time_to_first_token", {}).get("count") != 24:
        raise FleetError("AIPerf did not collect TTFT for all measured requests")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Authenticated CPU-only AIPerf fleet smoke")
    parser.add_argument("--tenant", choices=TENANTS, default="tenant-a")
    parser.add_argument("--class", dest="workload_class", choices=CLASSES, default="chat")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(".secrets/pilot-tenant-keys.json"),
    )
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parent.parent
        if not (root / "pyproject.toml").is_file() or not (root / "uv.lock").is_file():
            raise FleetError("run AIPerf from the synced repository tree on kip-control")
        summary = _run(
            args.tenant, args.workload_class, credentials_path=args.credentials, root=root
        )
    except (FleetError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"fleet AIPerf smoke failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "tenant": args.tenant,
                "class": args.workload_class,
                "summary": str(summary),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
