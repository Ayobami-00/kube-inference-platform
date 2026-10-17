"""Reviewed Lambda mixed-workload campaign contract and deterministic trial seeds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .workloads import CLASSES, load_workload_contract

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = ROOT / "benchmarks/campaigns/lambda-v1.json"
_ROUTING = {
    "chat": "interactive",
    "voice": "interactive",
    "agentic-style": "reasoning-batch",
    "batch": "reasoning-batch",
}
_GATES = {
    "baseline": ("both", "always"),
    "mtp": ("reasoning-batch", "decode-bottleneck"),
    "expert-parallel": ("reasoning-batch", "moe-bottleneck"),
    "tp2-pp2": ("reasoning-batch", "parallelism-bottleneck"),
    "prefill-decode": ("interactive", "measured-high-speed-fabric"),
    "nvfp4": ("interactive", "same-b200-bf16-control"),
}


def load_campaign(path: Path = CAMPAIGN_PATH) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("reviewed Lambda campaign contract is unavailable")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("reviewed Lambda campaign contract is invalid JSON") from error
    if not isinstance(data, dict) or set(data) != {
        "schemaVersion",
        "name",
        "workloadContract",
        "rootSeed",
        "modelRouting",
        "tenantIdentity",
        "offeredRateFractions",
        "scenarios",
        "warmupMinutes",
        "cooldownMinutes",
        "minimumCompletedPerClassPerNominalTrial",
        "variants",
    }:
        raise ValueError("Lambda campaign schema drift")
    workload = load_workload_contract("pilot-v1")
    if (
        data["schemaVersion"] != 1
        or data["name"] != "lambda-v1"
        or data["workloadContract"] != workload.name
        or data["rootSeed"] != workload.data["rootSeed"]
        or data["modelRouting"] != _ROUTING
        or data["tenantIdentity"] != "independent-of-model-and-class"
        or data["offeredRateFractions"] != {"nominal": 0.7, "stress": 1.0, "burst": 1.5}
        or data["warmupMinutes"] != 2
        or data["cooldownMinutes"] != 2
        or data["minimumCompletedPerClassPerNominalTrial"] != 100
    ):
        raise ValueError("Lambda campaign workload, seed, or timing drift")
    scenarios = data["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != {
        "isolated-chat",
        "isolated-agentic",
        "isolated-batch",
        "isolated-voice",
        "nominal-text",
        "nominal-full",
        "stress",
        "burst",
        "noisy-tenant",
        "worker-loss",
    }:
        raise ValueError("Lambda campaign scenarios are not reviewed")
    for name, item in scenarios.items():
        if (
            not isinstance(item, dict)
            or set(item) != {"classes", "minutes", "trials"}
            or not isinstance(item["classes"], list)
            or not item["classes"]
            or len(set(item["classes"])) != len(item["classes"])
            or set(item["classes"]) - set(CLASSES)
            or type(item["minutes"]) is not int
            or not 1 <= item["minutes"] <= 30
            or type(item["trials"]) is not int
            or item["trials"] != (3 if name.startswith(("isolated-", "nominal-")) else 1)
        ):
            raise ValueError(f"Lambda campaign scenario {name} is invalid")
    variants = data["variants"]
    if not isinstance(variants, dict) or set(variants) != set(_GATES):
        raise ValueError("Lambda campaign variants are not reviewed")
    for name, (pool, gate) in _GATES.items():
        if variants[name] != {"pool": pool, "gate": gate}:
            raise ValueError(f"Lambda campaign variant {name} gate drift")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return {
        **data,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "workloadSha256": workload.sha256,
    }


def trial_seed(campaign: dict[str, Any], scenario: str, trial: int) -> int:
    if scenario not in campaign["scenarios"] or type(trial) is not int:
        raise ValueError("unreviewed Lambda campaign scenario or trial")
    if not 1 <= trial <= campaign["scenarios"][scenario]["trials"]:
        raise ValueError("Lambda campaign trial out of range")
    source = f"{campaign['rootSeed']}:{campaign['name']}:{scenario}:{trial}".encode()
    return int.from_bytes(hashlib.sha256(source).digest()[:8], "big")
