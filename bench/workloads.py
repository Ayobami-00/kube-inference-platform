"""Reviewed synthetic workload contracts for the multi-tenant pilot.

This module describes and samples workload *metadata*. It deliberately does not send
requests or persist prompt/response content; AIPerf remains the text load generator.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import canonical_json, validate_identifier
from .errors import ValidationError
from .paths import repository_root

CLASSES = ("chat", "agentic-style", "batch", "voice")
TENANTS = ("tenant-a", "tenant-b", "tenant-c")
ROOT_KEYS = {
    "schemaVersion",
    "name",
    "rootSeed",
    "tenants",
    "nominalTenantShares",
    "contextInputTokenCap",
    "sharedPrefixFraction",
    "textMix",
    "voiceMix",
    "classes",
}
COMMON_CLASS_KEYS = {"arrival", "turns", "inputTokens", "outputTokens", "thinkTimeMs", "slo"}
SLO_KEYS = {
    "chat": {"ttftP95Ms", "tpotP95Ms", "successRateMin"},
    "agentic-style": {"ttftP95Ms", "taskP95Ms", "successRateMin"},
    "batch": {"withinMs", "withinRateMin", "successRateMin"},
    "voice": {"firstPlayableAudioP95Ms", "interruptionStopP95Ms", "successRateMin"},
}


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    return value


def _keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ValidationError(
            f"{path} fields differ: missing={sorted(missing)}, unknown={sorted(extra)}"
        )


def _positive_int(value: Any, path: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{path} must be an integer >= {minimum}")
    return value


def _fraction(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{path} must be a fraction")
    number = float(value)
    if not (0 < number <= 1 if positive else 0 <= number <= 1):
        raise ValidationError(f"{path} must be in {'(0, 1]' if positive else '[0, 1]'}")
    return number


def _range(value: Any, path: str, *, minimum: int = 0) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValidationError(f"{path} must be a two-element range")
    lower = _positive_int(value[0], f"{path}[0]", minimum=minimum)
    upper = _positive_int(value[1], f"{path}[1]", minimum=lower)
    return lower, upper


def _mix(value: Any, names: set[str], path: str) -> None:
    mix = _mapping(value, path)
    _keys(mix, names, path)
    total = sum(_fraction(amount, f"{path}.{name}", positive=True) for name, amount in mix.items())
    if abs(total - 1.0) > 1e-9:
        raise ValidationError(f"{path} must sum to 1; got {total}")


@dataclass(frozen=True)
class WorkloadContract:
    """Validated committed contract and its canonical digest."""

    name: str
    data: dict[str, Any]
    path: Path
    sha256: str


def load_workload_contract(name: str = "pilot-v1", *, root: Path | None = None) -> WorkloadContract:
    """Load one allowlisted contract without following paths outside its directory."""
    validate_identifier(name, kind="workload contract")
    base = (root or repository_root() / "benchmarks" / "workloads").resolve()
    path = (base / f"{name}.json").resolve()
    if path.parent != base:
        raise ValidationError("workload path escapes the reviewed contract directory")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read workload contract {name}: {error}") from error
    data = _mapping(data, "contract")
    _keys(data, ROOT_KEYS, "contract")
    if data["schemaVersion"] != 1 or data["name"] != name:
        raise ValidationError("workload contract schemaVersion/name mismatch")
    _positive_int(data["rootSeed"], "rootSeed", minimum=0)
    if data["tenants"] != list(TENANTS):
        raise ValidationError("pilot-v1 requires the reviewed tenant-a/b/c set")
    _positive_int(data["contextInputTokenCap"], "contextInputTokenCap")
    _fraction(data["sharedPrefixFraction"], "sharedPrefixFraction")
    _mix(data["nominalTenantShares"], set(TENANTS), "nominalTenantShares")
    _mix(data["textMix"], set(CLASSES) - {"voice"}, "textMix")
    _mix(data["voiceMix"], set(CLASSES), "voiceMix")
    classes = _mapping(data["classes"], "classes")
    _keys(classes, set(CLASSES), "classes")
    for name_key in CLASSES:
        spec = _mapping(classes[name_key], f"classes.{name_key}")
        expected = COMMON_CLASS_KEYS | (
            {"utteranceDurationMs", "interruptionFraction"} if name_key == "voice" else set()
        )
        _keys(spec, expected, f"classes.{name_key}")
        arrival = "scheduled-burst" if name_key == "batch" else "poisson"
        if spec["arrival"] != arrival:
            raise ValidationError(f"classes.{name_key}.arrival must be {arrival}")
        _range(spec["turns"], f"classes.{name_key}.turns", minimum=1)
        input_range = _range(spec["inputTokens"], f"classes.{name_key}.inputTokens", minimum=1)
        if input_range[1] > data["contextInputTokenCap"]:
            raise ValidationError(f"classes.{name_key}.inputTokens exceeds context cap")
        _range(spec["outputTokens"], f"classes.{name_key}.outputTokens", minimum=1)
        _range(spec["thinkTimeMs"], f"classes.{name_key}.thinkTimeMs")
        if name_key == "voice":
            _range(spec["utteranceDurationMs"], "classes.voice.utteranceDurationMs", minimum=1)
            _fraction(spec["interruptionFraction"], "classes.voice.interruptionFraction")
        slo = _mapping(spec["slo"], f"classes.{name_key}.slo")
        _keys(slo, SLO_KEYS[name_key], f"classes.{name_key}.slo")
        for key, value in slo.items():
            if key.endswith("RateMin"):
                _fraction(value, f"classes.{name_key}.slo.{key}", positive=True)
            else:
                _positive_int(value, f"classes.{name_key}.slo.{key}")
    return WorkloadContract(name, data, path, hashlib.sha256(canonical_json(data)).hexdigest())


def derived_seed(contract: WorkloadContract, tenant: str, workload_class: str, trial: int) -> int:
    """Derive a stable 64-bit seed for one class/tenant/trial."""
    if tenant not in TENANTS or workload_class not in CLASSES:
        raise ValidationError("unknown tenant or workload class")
    _positive_int(trial, "trial", minimum=1)
    material = f"{contract.data['rootSeed']}:{tenant}:{workload_class}:{trial}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def preview_trajectories(
    contract: WorkloadContract,
    *,
    tenant: str,
    workload_class: str,
    trial: int = 1,
    sessions: int = 3,
) -> dict[str, Any]:
    """Generate safe trajectory metadata to audit randomization and context capping."""
    _positive_int(sessions, "sessions", minimum=1)
    if sessions > 1000:
        raise ValidationError("sessions must be <= 1000")
    seed = derived_seed(contract, tenant, workload_class, trial)
    randomizer = random.Random(seed)
    spec = contract.data["classes"][workload_class]
    cap = contract.data["contextInputTokenCap"]
    samples = []
    for session_index in range(sessions):
        turns = randomizer.randint(*spec["turns"])
        shared_prefix = randomizer.random() < contract.data["sharedPrefixFraction"]
        context = 0
        turn_samples = []
        for turn_index in range(turns):
            input_tokens = randomizer.randint(*spec["inputTokens"])
            output_tokens = randomizer.randint(*spec["outputTokens"])
            context += input_tokens
            truncated = max(context - cap, 0)
            context = min(context, cap)
            turn_samples.append(
                {
                    "turn": turn_index + 1,
                    "inputTokens": input_tokens,
                    "outputTokenLimit": output_tokens,
                    "contextInputTokens": context,
                    "truncatedInputTokens": truncated,
                    "thinkTimeMs": 0
                    if turn_index == 0
                    else randomizer.randint(*spec["thinkTimeMs"]),
                }
            )
            context = min(context + output_tokens, cap)
        sample = {
            "session": session_index + 1,
            "sharedPrefix": shared_prefix,
            "turns": turn_samples,
        }
        if workload_class == "voice":
            sample["utteranceDurationMs"] = randomizer.randint(*spec["utteranceDurationMs"])
            sample["interrupt"] = randomizer.random() < spec["interruptionFraction"]
        samples.append(sample)
    return {
        "contract": contract.name,
        "contractSha256": contract.sha256,
        "tenant": tenant,
        "class": workload_class,
        "trial": trial,
        "seed": seed,
        "sessions": samples,
    }
