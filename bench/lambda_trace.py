"""Replay three deterministic synthetic tenant traces with AIPerf.

This short trace is an integration checkpoint, not the calibrated/SLO campaign.
There is deliberately no custom HTTP load-generation loop.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .lambda_campaign import load_campaign
from .lambda_gateway_smoke import KEYS, MODELS, ROOT, gateway_address, private_token
from .pilot_dataset import LAMBDA_TOKENIZERS, generate_text_dataset, save_generated
from .workloads import load_workload_contract

CONFIG = ROOT / "benchmarks/campaigns/lambda-text-replay.yaml"
COHORTS = (
    ("tenant-a", "chat", "qwen3-8b", "chat"),
    ("tenant-b", "agentic-style", "qwen3-next-80b-a3b-instruct", "agentic"),
    ("tenant-c", "batch", "qwen3-next-80b-a3b-instruct", "batch"),
)
SESSIONS = 6
OFFERED_SESSIONS_PER_SECOND = 0.25
NINE_COHORTS = tuple(
    (tenant, workload_class, model, credential_class)
    for tenant in ("tenant-a", "tenant-b", "tenant-c")
    for workload_class, model, credential_class in (
        ("chat", "qwen3-8b", "chat"),
        ("agentic-style", "qwen3-next-80b-a3b-instruct", "agentic"),
        ("batch", "qwen3-next-80b-a3b-instruct", "batch"),
    )
)
MATRICES = {
    "three": (COHORTS, SESSIONS, OFFERED_SESSIONS_PER_SECOND, "lambda-three-class"),
    "nine": (NINE_COHORTS, 2, 0.08, "lambda-nine-cohort"),
}


def validate_packets(path: Path, expected: int) -> int:
    seen: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            metadata = row.get("metadata") or {}
            identifier = metadata.get("x_request_id")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError("AIPerf trace contains missing or duplicate request IDs")
            if metadata.get("benchmark_phase") != "profiling":
                raise ValueError("AIPerf trace contains an unexpected non-measured phase")
            packets = [
                packet
                for response in row.get("responses", [])
                for packet in response.get("packets", [])
                if packet.get("name") == "data"
            ]
            if (
                row.get("status") != 200
                or not packets
                or packets[-1].get("value") != "[DONE]"
                or sum(packet.get("value") == "[DONE]" for packet in packets) != 1
            ):
                raise ValueError("AIPerf trace has a failed or incomplete SSE response")
            seen.add(identifier)
    if len(seen) != expected:
        raise ValueError("AIPerf trace sent count differs from scheduled turns")
    return len(seen)


def _cohort(
    root: Path,
    run_id: str,
    address: str,
    trial: int,
    tenant: str,
    workload_class: str,
    model: str,
    credential_class: str,
    tokenizer: Any,
    sessions: int,
    rate: float,
) -> dict[str, Any]:
    repository, revision = LAMBDA_TOKENIZERS[model]
    contract = load_workload_contract()
    dataset = generate_text_dataset(
        contract,
        tenant=tenant,
        workload_class=workload_class,
        trial=trial,
        sessions=sessions,
        offered_sessions_per_second=rate,
        count_tokens=lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
        tokenizer_repository=repository,
        tokenizer_revision=revision,
    )
    work = root / "benchmarks/.work" / run_id / f"{tenant}-{workload_class}"
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    dataset_path = work / "dataset.jsonl"
    save_generated(dataset, dataset_path)
    token = private_token(KEYS, tenant, credential_class)
    artifacts = work / "raw"
    env = {
        **os.environ,
        "KIP_LAMBDA_MODEL": model,
        "KIP_LAMBDA_GATEWAY_URL": address,
        "KIP_LAMBDA_TOKEN": token,
        "KIP_LAMBDA_DATASET": str(dataset_path),
        "KIP_LAMBDA_DATASET_FORMAT": "single_turn" if workload_class == "batch" else "multi_turn",
        "KIP_LAMBDA_ARTIFACTS": str(artifacts),
    }
    command = [
        str(root / ".venv/bin/aiperf"),
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
    log_path = work / "aiperf.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1200,
            check=False,
        )
    if result.returncode:
        raise ValueError(
            f"{tenant}/{workload_class} AIPerf exited {result.returncode}; see {log_path}"
        )
    summary = json.loads((artifacts / "profile_export_aiperf.json").read_text(encoding="utf-8"))
    expected = dataset.manifest["plannedTurnCount"]
    if (
        summary.get("aiperf_version") != "0.12.0"
        or summary.get("was_cancelled")
        or summary.get("error_summary")
        or (summary.get("request_count") or {}).get("avg") != expected
        or (summary.get("time_to_first_token") or {}).get("count") != expected
    ):
        raise ValueError(f"{tenant}/{workload_class} AIPerf aggregate is incomplete")
    completed = validate_packets(artifacts / "profile_export_raw.jsonl", expected)
    return {
        "tenant": tenant,
        "class": workload_class,
        "model": model,
        "trial": trial,
        "seed": dataset.manifest["seed"],
        "sessionsScheduled": sessions,
        "turnsScheduled": expected,
        "sent": completed,
        "completed": completed,
        "streamDone": completed,
        "datasetSha256": dataset.manifest["datasetSha256"],
        "ttftP95Ms": summary["time_to_first_token"]["p95"],
        "e2eP95Ms": summary["request_latency"]["p95"],
        "outputTokensPerSecond": summary["output_token_throughput"]["avg"],
    }


def run(trial: int, matrix: str = "three") -> dict[str, Any]:
    campaign = load_campaign()
    if trial not in {1, 2, 3} or matrix not in MATRICES or campaign["rootSeed"] != 42:
        raise ValueError("trace trial is not reviewed")
    if not CONFIG.is_file() or CONFIG.is_symlink():
        raise ValueError("reviewed AIPerf trace configuration is unavailable")
    if set(LAMBDA_TOKENIZERS) != set(MODELS):
        raise ValueError("Lambda tokenizer and serving model pins drifted")
    # Transformers' lazy root export can race during three concurrent imports.
    # Resolve each reviewed tokenizer serially before starting cohort workers.
    from transformers.models.auto.tokenization_auto import AutoTokenizer

    tokenizers = {
        model: AutoTokenizer.from_pretrained(repository, revision=revision, trust_remote_code=False)
        for model, (repository, revision) in LAMBDA_TOKENIZERS.items()
    }
    address = gateway_address()
    cohorts, sessions, rate, prefix = MATRICES[matrix]
    run_id = datetime.now(UTC).strftime(f"{prefix}-%Y%m%d-%H%M%S")
    run_root = ROOT / "benchmarks/.work" / run_id
    run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=len(cohorts)) as pool:
        futures = [
            pool.submit(
                _cohort,
                ROOT,
                run_id,
                address,
                trial,
                *cohort,
                tokenizers[cohort[2]],
                sessions,
                rate,
            )
            for cohort in cohorts
        ]
        cohorts = [future.result() for future in futures]
    if sum(row["completed"] for row in cohorts) != sum(row["turnsScheduled"] for row in cohorts):
        raise ValueError("trace count reconciliation failed")
    return {
        "status": "PASS",
        "kind": "short-integration-trace-not-slo-campaign",
        "matrix": matrix,
        "runId": run_id,
        "campaignSha256": campaign["sha256"],
        "workloadSha256": campaign["workloadSha256"],
        "cohorts": cohorts,
        "totalCompleted": sum(row["completed"] for row in cohorts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run short reviewed three-tenant AIPerf traces")
    parser.add_argument("--trial", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--matrix", choices=tuple(MATRICES), default="three")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.trial, args.matrix), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Lambda trace failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
