"""Strict suite and whole-node cost-profile validation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ValidationError
from .paths import cost_profiles_root, repository_root, suites_root

IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
COST_PROFILE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RUN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

_SUITE_KEYS = {
    "schemaVersion",
    "name",
    "description",
    "target",
    "workload",
    "warmup",
    "sweep",
    "telemetry",
    "retention",
}
_TARGET_KEYS = {"runtime", "modelProfile", "servedModel", "endpoint"}
_WORKLOAD_KEYS = {
    "protocol",
    "streaming",
    "inputTokens",
    "outputTokens",
    "temperature",
    "seed",
    "ignoreEos",
}
_WARMUP_KEYS = {"requests", "concurrency", "excludeFromMetrics"}
_SWEEP_KEYS = {
    "concurrency",
    "requestsPerCell",
    "trialsPerCell",
    "cooldownSeconds",
    "confidenceLevel",
    "iterationOrder",
}
_TELEMETRY_KEYS = {"sampleIntervalMilliseconds", "prometheusContextSeconds"}
_RETENTION_KEYS = {"resultsDays", "timeoutSeconds"}
_COST_KEYS = {
    "schemaVersion",
    "name",
    "currency",
    "nodeHourlyRateUsd",
    "provider",
    "machineType",
    "region",
    "source",
    "effectiveFrom",
    "physicalGpuCount",
    "powerIncludedInRate",
}
_COST_OPTIONAL_KEYS = {"sourceType"}
_RUNTIME_MODEL_KEYS = {"id", "revision", "servedName", "huggingFaceSecretName"}


def _require_exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValidationError(f"{where} has invalid fields: {'; '.join(details)}")


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be a JSON object")
    return value


def _require_int(value: Any, where: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{where} must be an integer >= {minimum}")
    return value


def _require_number(value: Any, where: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= minimum:
        raise ValidationError(f"{where} must be a number > {minimum}")
    return float(value)


def validate_identifier(value: str, *, kind: str = "identifier", run_id: bool = False) -> str:
    """Validate an allowlist-safe file/Kubernetes identifier."""
    pattern = RUN_ID_PATTERN if run_id else IDENTIFIER_PATTERN
    if not pattern.fullmatch(value) or ".." in value or "/" in value or "\\" in value:
        raise ValidationError(f"invalid {kind}: {value!r}")
    return value


def canonical_json(value: Any) -> bytes:
    """Return a stable JSON encoding with a trailing newline."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def config_hash(value: Any) -> str:
    """Hash the canonical configuration representation."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValidationError(f"{description} does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read {description} {path}: {error}") from error
    return _require_mapping(raw, description)


@dataclass(frozen=True)
class Suite:
    """A validated committed AIPerf benchmark suite."""

    name: str
    data: dict[str, Any]
    path: Path
    sha256: str


@dataclass(frozen=True)
class RuntimeModelIdentity:
    """Immutable tokenizer/model identity read from a committed runtime profile."""

    repository: str
    revision: str
    served_name: str


def load_runtime_model_identity(profile_name: str) -> RuntimeModelIdentity:
    """Resolve benchmark tokenizer identity from the reviewed vLLM profile."""
    validate_identifier(profile_name, kind="runtime model profile")
    base = (repository_root() / "deploy" / "runtime" / "vllm" / "profiles").resolve()
    path = (base / f"{profile_name}.json").resolve()
    if path.parent != base:
        raise ValidationError("runtime profile path escapes the committed profile directory")
    data = _load_json(path, "runtime model profile")
    model = _require_mapping(data.get("model"), "runtime model profile.model")
    _require_exact_keys(model, _RUNTIME_MODEL_KEYS, "runtime model profile.model")
    for key in ("id", "revision", "servedName"):
        if not isinstance(model[key], str) or not model[key].strip():
            raise ValidationError(f"runtime model profile.model.{key} must be non-empty")
    if not re.fullmatch(r"[a-f0-9]{40}", model["revision"]):
        raise ValidationError("runtime model profile.model.revision must be a commit SHA")
    return RuntimeModelIdentity(model["id"], model["revision"], model["servedName"])


def load_suite(name: str, *, root: Path | None = None) -> Suite:
    """Load and strictly validate a committed suite by allowlisted name."""
    validate_identifier(name, kind="suite name")
    base = (root or suites_root()).resolve()
    path = (base / f"{name}.json").resolve()
    if path.parent != base:
        raise ValidationError("suite path escapes the committed suite directory")
    data = _load_json(path, "benchmark suite")
    _require_exact_keys(data, _SUITE_KEYS, "suite")
    if data["schemaVersion"] != 1 or data["name"] != name:
        raise ValidationError("suite schemaVersion must be 1 and name must match the filename")
    if not isinstance(data["description"], str) or not data["description"].strip():
        raise ValidationError("suite.description must be a non-empty string")

    target = _require_mapping(data["target"], "suite.target")
    _require_exact_keys(target, _TARGET_KEYS, "suite.target")
    for key in ("runtime", "modelProfile", "servedModel"):
        if not isinstance(target[key], str) or not target[key].strip():
            raise ValidationError(f"suite.target.{key} must be a non-empty string")
    if target["runtime"] != "vllm":
        raise ValidationError("v0.1 benchmark suites require the vllm runtime adapter")
    model_identity = load_runtime_model_identity(target["modelProfile"])
    if model_identity.served_name != target["servedModel"]:
        raise ValidationError(
            "suite target servedModel does not match the committed runtime profile"
        )
    endpoint = urlsplit(target["endpoint"])
    if endpoint.scheme != "http" or not endpoint.hostname or endpoint.query or endpoint.fragment:
        raise ValidationError(
            "suite.target.endpoint must be an internal HTTP URL without query/fragment"
        )
    if endpoint.username or endpoint.password:
        raise ValidationError("suite.target.endpoint must not contain credentials")
    try:
        if endpoint.hostname and ipaddress.ip_address(endpoint.hostname):
            raise ValidationError(
                "suite.target.endpoint must use Kubernetes DNS, not an IP address"
            )
    except ValueError:
        pass
    if not endpoint.path.endswith("/v1"):
        raise ValidationError("suite.target.endpoint must end in /v1")

    workload = _require_mapping(data["workload"], "suite.workload")
    _require_exact_keys(workload, _WORKLOAD_KEYS, "suite.workload")
    if workload["protocol"] != "openai-chat" or workload["streaming"] is not True:
        raise ValidationError("v0.1 suites require streaming OpenAI chat")
    _require_int(workload["inputTokens"], "suite.workload.inputTokens")
    _require_int(workload["outputTokens"], "suite.workload.outputTokens")
    if workload["temperature"] != 0 or workload["ignoreEos"] is not True:
        raise ValidationError("v0.1 baseline requires temperature=0 and ignoreEos=true")
    if isinstance(workload["seed"], bool) or not isinstance(workload["seed"], int):
        raise ValidationError("suite.workload.seed must be an integer")

    warmup = _require_mapping(data["warmup"], "suite.warmup")
    _require_exact_keys(warmup, _WARMUP_KEYS, "suite.warmup")
    _require_int(warmup["requests"], "suite.warmup.requests")
    _require_int(warmup["concurrency"], "suite.warmup.concurrency")
    if warmup["excludeFromMetrics"] is not True:
        raise ValidationError("warmup must be excluded from measured metrics")

    sweep = _require_mapping(data["sweep"], "suite.sweep")
    _require_exact_keys(sweep, _SWEEP_KEYS, "suite.sweep")
    concurrency = sweep["concurrency"]
    if not isinstance(concurrency, list) or not concurrency:
        raise ValidationError("suite.sweep.concurrency must be a non-empty list")
    values = [_require_int(item, "suite.sweep.concurrency[]") for item in concurrency]
    if values != sorted(set(values)):
        raise ValidationError("suite.sweep.concurrency must contain sorted unique values")
    _require_int(sweep["requestsPerCell"], "suite.sweep.requestsPerCell")
    if _require_int(sweep["trialsPerCell"], "suite.sweep.trialsPerCell") > 10:
        raise ValidationError("suite.sweep.trialsPerCell must be <= 10")
    _require_int(sweep["cooldownSeconds"], "suite.sweep.cooldownSeconds", minimum=0)
    confidence = _require_number(sweep["confidenceLevel"], "suite.sweep.confidenceLevel")
    if confidence != 0.95:
        raise ValidationError("v0.1 suites require a 0.95 confidence level")
    if sweep["iterationOrder"] != "independent":
        raise ValidationError("v0.1 suites require independent iteration order")

    telemetry = _require_mapping(data["telemetry"], "suite.telemetry")
    _require_exact_keys(telemetry, _TELEMETRY_KEYS, "suite.telemetry")
    _require_int(telemetry["sampleIntervalMilliseconds"], "telemetry sample interval")
    _require_int(telemetry["prometheusContextSeconds"], "Prometheus context")
    retention = _require_mapping(data["retention"], "suite.retention")
    _require_exact_keys(retention, _RETENTION_KEYS, "suite.retention")
    if _require_int(retention["resultsDays"], "results retention") > 365:
        raise ValidationError("suite.retention.resultsDays must be <= 365")
    if _require_int(retention["timeoutSeconds"], "benchmark timeout") > 21600:
        raise ValidationError("suite.retention.timeoutSeconds must be <= 21600")
    return Suite(name=name, data=data, path=path, sha256=config_hash(data))


@dataclass(frozen=True)
class CostProfile:
    """A validated, published whole-node cost profile."""

    name: str
    data: dict[str, Any]
    path: Path
    sha256: str

    @property
    def node_hourly_rate(self) -> float:
        return float(self.data["nodeHourlyRateUsd"])

    @property
    def gpu_hourly_rate(self) -> float:
        return self.node_hourly_rate / int(self.data["physicalGpuCount"])


def load_cost_profile(name: str, *, root: Path | None = None) -> CostProfile:
    """Load a strictly validated, committed whole-node price profile."""
    if not COST_PROFILE_PATTERN.fullmatch(name):
        raise ValidationError(f"invalid cost profile name: {name!r}")
    base = (root or cost_profiles_root()).resolve()
    path = (base / f"{name}.json").resolve()
    if path.parent != base:
        raise ValidationError("cost profile path escapes the committed profile directory")
    data = _load_json(path, "cost profile")
    missing = sorted(_COST_KEYS - data.keys())
    unknown = sorted(data.keys() - _COST_KEYS - _COST_OPTIONAL_KEYS)
    if missing or unknown:
        raise ValidationError(
            "cost profile has invalid fields: "
            + "; ".join(
                part
                for part in (
                    f"missing {', '.join(missing)}" if missing else "",
                    f"unknown {', '.join(unknown)}" if unknown else "",
                )
                if part
            )
        )
    if data.get("sourceType", "public-listing") not in {
        "public-listing",
        "user-confirmed-billing",
    }:
        raise ValidationError("cost profile sourceType is invalid")
    if data["schemaVersion"] != 1 or data["name"] != name:
        raise ValidationError("cost profile schemaVersion must be 1 and name must match filename")
    if data["currency"] != "USD":
        raise ValidationError("v0.1 cost profiles must use USD")
    hourly_rate = _require_number(data["nodeHourlyRateUsd"], "cost profile nodeHourlyRateUsd")
    if hourly_rate >= 1_000_000:
        raise ValidationError("cost profile nodeHourlyRateUsd must be less than 1000000")
    gpu_count = _require_int(data["physicalGpuCount"], "cost profile physicalGpuCount")
    if gpu_count > 1024:
        raise ValidationError("cost profile physicalGpuCount must be <= 1024")
    for key in ("provider", "machineType", "region", "source"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValidationError(f"cost profile {key} must be a non-empty string")
        maximum = 512 if key == "source" else 128
        if len(data[key]) > maximum:
            raise ValidationError(f"cost profile {key} must be <= {maximum} characters")
        if "REPLACE_ME" in data[key] or data[key].startswith("REPLACE_WITH_"):
            raise ValidationError(f"cost profile {key} contains an unconfigured placeholder")
    source = urlsplit(data["source"])
    if (
        source.scheme != "https"
        or not source.hostname
        or source.username
        or source.password
        or source.query
        or source.fragment
    ):
        raise ValidationError(
            "cost profile source must be a public HTTPS URL without credentials, query, or fragment"
        )
    try:
        effective_from = date.fromisoformat(data["effectiveFrom"])
    except (TypeError, ValueError) as error:
        raise ValidationError("cost profile effectiveFrom must be YYYY-MM-DD") from error
    if effective_from > date.today():
        raise ValidationError("cost profile effectiveFrom must not be in the future")
    if data["powerIncludedInRate"] is not True:
        raise ValidationError("v0.1 rates must include power to prevent double counting")
    return CostProfile(name=name, data=data, path=path, sha256=config_hash(data))
