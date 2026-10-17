"""Short, credential-safe AIPerf checkpoint through the private Lambda Gateway.

This is a transport smoke, not a representative mixed-workload campaign or SLO
trial. AIPerf generates and times every inference request.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYS = Path("/var/lib/kube-inference-platform/tenant-keys.json")
CONFIG = ROOT / "benchmarks/campaigns/lambda-gateway-smoke.yaml"
MODELS = {
    "qwen3-8b": ("chat", "Qwen/Qwen3-8B", "b968826d9c46dd6066d109eabc6255188de91218"),
    "qwen3-next-80b-a3b-instruct": (
        "agentic",
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        "9c7f2fbe84465e40164a94cc16cd30b6999b0cc7",
    ),
}


def private_token(path: Path, tenant: str, workload_class: str) -> str:
    if (
        path.is_symlink()
        or not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or path.parent.stat().st_mode & 0o077
    ):
        raise ValueError("tenant credential file must be private and regular")
    records = json.loads(path.read_text(encoding="utf-8"))
    token = records[tenant][workload_class]
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError("selected synthetic tenant credential is invalid")
    return token


def gateway_address() -> str:
    env = {**os.environ, "KUBECONFIG": "/etc/kubernetes/admin.conf"}
    result = subprocess.run(
        ["kubectl", "-n", "kip-inference", "get", "gateway", "kip-inference-gateway", "-o", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    gateway = json.loads(result.stdout)
    addresses = gateway.get("status", {}).get("addresses", [])
    if not addresses or addresses[0].get("type") != "IPAddress":
        raise ValueError("private Gateway has no IP address")
    address = addresses[0]["value"]
    if not isinstance(address, str) or not address.startswith("10."):
        raise ValueError("Gateway address is not a reviewed private ClusterIP")
    return f"http://{address}"


def validate_artifacts(artifacts: Path) -> dict[str, object]:
    summary = json.loads((artifacts / "profile_export_aiperf.json").read_text(encoding="utf-8"))
    if (
        summary.get("aiperf_version") != "0.12.0"
        or summary.get("was_cancelled")
        or summary.get("error_summary")
        or (summary.get("request_count") or {}).get("avg") != 24
        or (summary.get("time_to_first_token") or {}).get("count") != 24
    ):
        raise ValueError("AIPerf did not complete all 24 measured requests")
    raw = artifacts / "profile_export_raw.jsonl"
    done = 0
    warmup = 0
    with raw.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            phase = (row.get("metadata") or {}).get("benchmark_phase")
            if phase not in {"warmup", "profiling"}:
                raise ValueError("AIPerf raw record has an unknown phase")
            packets = [
                packet
                for response in row.get("responses", [])
                for packet in response.get("packets", [])
                if packet.get("name") == "data"
            ]
            if row.get("status") != 200 or not packets or packets[-1].get("value") != "[DONE]":
                raise ValueError("AIPerf response failed or SSE stream did not finish")
            if phase == "profiling":
                done += 1
            else:
                warmup += 1
    if done != 24 or warmup != 4:
        raise ValueError("AIPerf raw stream count differs from warmup/measured request count")
    return {
        "completed": done,
        "warmupCompleted": warmup,
        "ttftP95Ms": (summary.get("time_to_first_token") or {}).get("p95"),
        "e2eP95Ms": (summary.get("request_latency") or {}).get("p95"),
        "outputTokensPerSecond": (summary.get("output_token_throughput") or {}).get("avg"),
    }


def run(model: str, tenant: str) -> dict[str, object]:
    workload_class, repository, revision = MODELS[model]
    token = private_token(KEYS, tenant, workload_class)
    address = gateway_address()
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise ValueError("reviewed AIPerf config is missing")
    run_id = datetime.now(UTC).strftime("lambda-gateway-smoke-%Y%m%d-%H%M%S")
    work = ROOT / "benchmarks/.work" / run_id / model / tenant
    work.mkdir(parents=True, mode=0o700, exist_ok=False)
    artifacts = work / "raw"
    log_path = work / "aiperf.log"
    env = {
        **os.environ,
        "KIP_LAMBDA_MODEL": model,
        "KIP_LAMBDA_GATEWAY_URL": address,
        "KIP_LAMBDA_TOKEN": token,
        "KIP_LAMBDA_ARTIFACTS": str(artifacts),
    }
    command = [
        str(ROOT / ".venv/bin/aiperf"),
        "profile",
        "--config",
        str(CONFIG),
        "--header",
        f"X-Gateway-Base-Model-Name:{model}",
        "--tokenizer",
        repository,
        "--tokenizer-revision",
        revision,
        "--apply-chat-template",
        "--export-level",
        "raw",
    ]
    fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )
    if result.returncode:
        raise ValueError(
            f"AIPerf failed with exit {result.returncode}; inspect private log {log_path}"
        )
    metrics = validate_artifacts(artifacts)
    return {"status": "PASS", "runId": run_id, "model": model, "tenant": tenant, **metrics}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a short authenticated llm-d Gateway smoke")
    parser.add_argument("--model", choices=tuple(MODELS), required=True)
    parser.add_argument("--tenant", choices=("tenant-a", "tenant-b", "tenant-c"), required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.model, args.tenant), sort_keys=True))
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Lambda Gateway smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
