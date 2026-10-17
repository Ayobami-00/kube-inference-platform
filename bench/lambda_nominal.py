"""Reviewed ten-minute, nine-cohort text baseline through the private Gateway.

AIPerf performs all request generation and timing. This runner fixes the
tenant/class mix and seeds; a comparison may replay the same trial only after
the serving variant and hardware have been independently recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .lambda_campaign import load_campaign
from .lambda_gateway_smoke import MODELS, ROOT, gateway_address
from .lambda_trace import CONFIG, NINE_COHORTS, _cohort
from .pilot_dataset import LAMBDA_TOKENIZERS, _schedule
from .workloads import derived_seed, load_workload_contract

# The minimum turns across all three tenants are 126 chat, 108 agentic-style,
# and 102 batch. Expected turn proportions are approximately 50/30/20.
# All fixed-seed first-session schedules finish inside the reviewed 10 minutes.
CLASS_PLAN = {
    "chat": {"sessionsPerTenant": 21, "offeredSessionsPerSecondPerTenant": 0.05},
    "agentic-style": {"sessionsPerTenant": 9, "offeredSessionsPerSecondPerTenant": 0.025},
    "batch": {"sessionsPerTenant": 34, "offeredSessionsPerSecondPerTenant": 0.06},
}
VARIANTS = ("baseline", "large-flow6", "large-flow8")
RUN_ID = re.compile(r"lambda-nominal-(?:baseline|large-flow6|large-flow8)-[0-9]{8}-[0-9]{6}\Z")


def verify_variant(variant: str) -> str:
    if variant not in VARIANTS:
        raise ValueError("nominal serving variant is not reviewed")
    llmd = runpy.run_path(str(ROOT / "deploy/lambda/llmd-install.py"))
    flows = runpy.run_path(str(ROOT / "deploy/lambda/flow-variant.py"))
    control = subprocess.run(
        ["hostname"], capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()
    if not llmd["CONTROL"].fullmatch(control):
        raise ValueError("nominal trials run only on the owned Lambda control plane")
    desired = flows["desired_values"](llmd, control, variant)
    current = json.loads(
        subprocess.run(
            ["helm", "get", "values", "kip-qwen3-next", "-n", "kip-inference", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
            env={**os.environ, "KUBECONFIG": "/etc/kubernetes/admin.conf"},
        ).stdout
    )
    value_hash = hashlib.sha256(
        json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    digest = llmd["LOCK"]["inference"]["routerGatewayChartDigest"]
    if (
        current.pop("kip", None)
        != {
            "owner": llmd["OWNER"],
            "chartDigest": digest,
            "valuesSha256": value_hash,
        }
        or current != desired
    ):
        raise ValueError("active llm-d large-pool values differ from the reviewed variant")
    return value_hash


def inspect_cohort(work: Path, tenant: str, workload_class: str, trial: int) -> dict:
    cohort = work / f"{tenant}-{workload_class}"
    manifest_path = cohort / "dataset.manifest.json"
    dataset_path = cohort / "dataset.jsonl"
    raw_dir = cohort / "raw"
    for path in (cohort, manifest_path, dataset_path, raw_dir):
        if path.is_symlink() or not path.exists():
            raise ValueError(f"{tenant}/{workload_class} private artifacts are missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("plannedTurnCount")
    item = CLASS_PLAN[workload_class]
    contract = load_workload_contract()
    if (
        manifest.get("tenant") != tenant
        or manifest.get("class") != workload_class
        or manifest.get("trial") != trial
        or manifest.get("contractSha256") != contract.sha256
        or manifest.get("seed") != derived_seed(contract, tenant, workload_class, trial)
        or manifest.get("sessionCount") != item["sessionsPerTenant"]
        or manifest.get("offeredSessionsPerSecond") != item["offeredSessionsPerSecondPerTenant"]
        or manifest.get("datasetSha256") != hashlib.sha256(dataset_path.read_bytes()).hexdigest()
        or type(expected) is not int
        or expected < item["sessionsPerTenant"]
    ):
        raise ValueError(f"{tenant}/{workload_class} dataset provenance drifted")
    summary = json.loads((raw_dir / "profile_export_aiperf.json").read_text(encoding="utf-8"))
    if summary.get("aiperf_version") != "0.12.0" or summary.get("was_cancelled"):
        raise ValueError(f"{tenant}/{workload_class} AIPerf run is unreviewed or cancelled")
    seen: set[str] = set()
    statuses: dict[int, int] = {200: 0, 429: 0}
    with (raw_dir / "profile_export_raw.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            metadata = record.get("metadata") or {}
            identifier = metadata.get("x_request_id")
            status = record.get("status")
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in seen
                or metadata.get("benchmark_phase") != "profiling"
                or type(status) is not int
                or status not in statuses
            ):
                raise ValueError(f"{tenant}/{workload_class} has invalid request identity/status")
            seen.add(identifier)
            packets = [
                packet
                for response in record.get("responses", [])
                for packet in response.get("packets", [])
                if packet.get("name") == "data"
            ]
            done = sum(packet.get("value") == "[DONE]" for packet in packets)
            if (status == 200 and (done != 1 or packets[-1].get("value") != "[DONE]")) or (
                status == 429 and done
            ):
                raise ValueError(f"{tenant}/{workload_class} has an incomplete or invalid stream")
            statuses[status] += 1
    errors = summary.get("error_summary")
    if not isinstance(errors, list) or any(
        not isinstance(row, dict)
        or row.get("count") != statuses[429]
        or (row.get("error_details") or {}).get("code") != 429
        for row in errors
    ):
        raise ValueError(f"{tenant}/{workload_class} has unaccounted AIPerf errors")
    if (
        len(seen) != expected
        or sum(row["count"] for row in errors) != statuses[429]
        or (summary.get("request_count") or {}).get("avg") != statuses[200]
        or (summary.get("time_to_first_token") or {}).get("count") != statuses[200]
    ):
        raise ValueError(f"{tenant}/{workload_class} request transitions do not reconcile")
    return {
        "tenant": tenant,
        "class": workload_class,
        "scheduled": expected,
        "sent": len(seen),
        "completed": statuses[200],
        "rejected": statuses[429],
        "failed": 0,
    }


def finalize(run_id: str, root: Path = ROOT) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("nominal baseline run ID is not reviewed")
    work = root / "benchmarks/.work" / run_id
    if work.is_symlink() or not work.is_dir():
        raise ValueError("private nominal run is missing or unsafe")
    manifest_path = work / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = validate_plan(manifest.get("trial"))
    if (
        manifest.get("runId") != run_id
        or manifest.get("variant") not in VARIANTS
        or not run_id.startswith(f"lambda-nominal-{manifest['variant']}-")
        or manifest.get("status") not in {"running", "complete"}
        or any(manifest.get(key) != value for key, value in plan.items())
    ):
        raise ValueError("nominal run manifest is incomplete or drifted")
    cohorts = [
        inspect_cohort(work, tenant, workload_class, manifest["trial"])
        for tenant, workload_class, _, _ in NINE_COHORTS
    ]
    completed_by_class = {
        workload_class: sum(row["completed"] for row in cohorts if row["class"] == workload_class)
        for workload_class in CLASS_PLAN
    }
    if any(
        completed < load_campaign()["minimumCompletedPerClassPerNominalTrial"]
        for completed in completed_by_class.values()
    ):
        raise ValueError("nominal class has fewer than 100 completed turns")
    totals = {
        key: sum(row[key] for row in cohorts)
        for key in ("scheduled", "sent", "completed", "rejected", "failed")
    }
    if totals["scheduled"] != totals["sent"] or totals["sent"] != sum(
        totals[key] for key in ("completed", "rejected", "failed")
    ):
        raise ValueError("nominal request outcome transitions do not reconcile")
    manifest.update(
        status="complete",
        completedByClass=completed_by_class,
        totalCompleted=totals["completed"],
        outcomes=totals,
        cohorts=cohorts,
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chmod(manifest_path, 0o600)
    return {
        "runId": run_id,
        "variant": manifest["variant"],
        "status": "complete",
        "completedByClass": completed_by_class,
        "outcomes": totals,
    }


def validate_plan(trial: int) -> dict:
    campaign = load_campaign()
    scenario = campaign["scenarios"]["nominal-text"]
    if type(trial) is not int or not 1 <= trial <= scenario["trials"]:
        raise ValueError("nominal text trial must be one of the three reviewed seeds")
    if scenario["minutes"] != 10 or set(scenario["classes"]) != set(CLASS_PLAN):
        raise ValueError("nominal-text campaign contract drifted")
    contract = load_workload_contract()
    from random import Random

    max_offered = 0
    for tenant, workload_class, _, _ in NINE_COHORTS:
        item = CLASS_PLAN[workload_class]
        last = _schedule(
            Random(derived_seed(contract, tenant, workload_class, trial)),
            workload_class,
            item["sessionsPerTenant"],
            item["offeredSessionsPerSecondPerTenant"],
        )[-1]
        max_offered = max(max_offered, last)
    if max_offered >= scenario["minutes"] * 60_000:
        raise ValueError("reviewed nominal arrivals exceed the ten-minute window")
    minimum = {
        workload_class: (
            3 * item["sessionsPerTenant"] * contract.data["classes"][workload_class]["turns"][0]
        )
        for workload_class, item in CLASS_PLAN.items()
    }
    if any(
        value < campaign["minimumCompletedPerClassPerNominalTrial"] for value in minimum.values()
    ):
        raise ValueError("nominal plan cannot reach the per-class P95 sample floor")
    return {
        "campaignSha256": campaign["sha256"],
        "workloadSha256": contract.sha256,
        "trial": trial,
        "minutes": scenario["minutes"],
        "minimumScheduledTurnsByClass": minimum,
        "lastOfferedMs": max_offered,
        "classPlan": CLASS_PLAN,
    }


def run(trial: int, variant: str = "baseline") -> dict:
    plan = validate_plan(trial)
    epp_hash = verify_variant(variant)
    if not CONFIG.is_file() or CONFIG.is_symlink() or set(LAMBDA_TOKENIZERS) != set(MODELS):
        raise ValueError("reviewed AIPerf configuration or tokenizer pins are unavailable")
    from transformers.models.auto.tokenization_auto import AutoTokenizer

    tokenizers = {
        model: AutoTokenizer.from_pretrained(repository, revision=revision, trust_remote_code=False)
        for model, (repository, revision) in LAMBDA_TOKENIZERS.items()
    }
    address = gateway_address()
    run_id = datetime.now(UTC).strftime(f"lambda-nominal-{variant}-%Y%m%d-%H%M%S")
    work = ROOT / "benchmarks/.work" / run_id
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {
        **plan,
        "runId": run_id,
        "variant": variant,
        "eppValuesSha256": epp_hash,
        "status": "running",
    }
    (work / "run.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chmod(work / "run.json", 0o600)
    with ThreadPoolExecutor(max_workers=len(NINE_COHORTS)) as pool:
        futures = []
        for tenant, workload_class, model, credential_class in NINE_COHORTS:
            item = CLASS_PLAN[workload_class]
            futures.append(
                pool.submit(
                    _cohort,
                    ROOT,
                    run_id,
                    address,
                    trial,
                    tenant,
                    workload_class,
                    model,
                    credential_class,
                    tokenizers[model],
                    item["sessionsPerTenant"],
                    item["offeredSessionsPerSecondPerTenant"],
                )
            )
        for future in futures:
            try:
                future.result()
            except ValueError as error:
                if "AIPerf aggregate is incomplete" not in str(error):
                    raise
    return finalize(run_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a reviewed Lambda nominal text trial")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--trial", type=int, choices=(1, 2, 3))
    action.add_argument("--finalize", metavar="RUN_ID")
    parser.add_argument("--variant", choices=VARIANTS, default="baseline")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                finalize(args.finalize) if args.finalize else run(args.trial, args.variant),
                sort_keys=True,
            )
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Lambda nominal baseline failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
