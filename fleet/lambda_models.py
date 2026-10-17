"""Review and validate Lambda's immutable model-pool selection."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .lambda_cloud import LambdaError

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "deploy/lambda/models.json"
_REVISION = re.compile(r"[0-9a-f]{40}")
_POOL_KEYS = {
    "model",
    "revision",
    "servedName",
    "runtime",
    "dtype",
    "maxModelLen",
    "replicas",
    "gpusPerReplica",
    "tensorParallelSize",
    "pipelineParallelSize",
    "expertParallel",
    "speculation",
    "workloadClasses",
}


def _revision(value: Any) -> bool:
    return isinstance(value, str) and _REVISION.fullmatch(value) is not None


def load_models(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LambdaError("reviewed Lambda model profile is unavailable")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LambdaError("reviewed Lambda model profile is invalid JSON") from error
    if not isinstance(data, dict) or set(data) != {
        "schemaVersion",
        "status",
        "compatibilityLock",
        "gpuDriverPolicy",
        "pools",
        "speech",
        "optionalNvfp4",
    }:
        raise LambdaError("reviewed Lambda model profile schema is invalid")
    if data["schemaVersion"] != 1 or data["status"] != "artifact-locked-target-unvalidated":
        raise LambdaError("Lambda model profile version or validation status drift")
    if data["compatibilityLock"] != "deploy/fleet/versions.json":
        raise LambdaError("Lambda model profile compatibility lock drift")
    driver = data["gpuDriverPolicy"]
    if driver != {
        "mode": "preinstalled",
        "cudaRuntime": "13.0",
        "minimumDriverMajor": 580,
        "onMismatch": "stop-before-scaling",
    }:
        raise LambdaError("Lambda driver policy drift")
    pools = data["pools"]
    if not isinstance(pools, dict) or set(pools) != {"interactive", "reasoning-batch"}:
        raise LambdaError("Lambda model pools are not reviewed")
    expected = {
        "interactive": {
            "model": "Qwen/Qwen3-8B",
            "servedName": "qwen3-8b",
            "replicas": 2,
            "gpusPerReplica": 1,
            "tensorParallelSize": 1,
            "pipelineParallelSize": 1,
            "workloadClasses": ["chat", "voice"],
        },
        "reasoning-batch": {
            "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
            "servedName": "qwen3-next-80b-a3b-instruct",
            "replicas": 1,
            "gpusPerReplica": 4,
            "tensorParallelSize": 4,
            "pipelineParallelSize": 1,
            "workloadClasses": ["agentic-style", "batch"],
        },
    }
    for name, pool in pools.items():
        allowed = _POOL_KEYS | ({"chatTemplateKwargs"} if name == "interactive" else set())
        if not isinstance(pool, dict) or set(pool) != allowed:
            raise LambdaError(f"Lambda {name} profile fields are invalid")
        if any(pool.get(key) != value for key, value in expected[name].items()):
            raise LambdaError(f"Lambda {name} profile deployment drift")
        if (
            not _revision(pool.get("revision"))
            or pool.get("runtime") != "vllm"
            or pool.get("dtype") != "bfloat16"
            or pool.get("maxModelLen") != 8192
            or pool.get("expertParallel") is not False
            or pool.get("speculation") is not None
        ):
            raise LambdaError(f"Lambda {name} profile runtime drift")
    if pools["interactive"]["chatTemplateKwargs"] != {"enable_thinking": False}:
        raise LambdaError("interactive pool must use reviewed non-thinking chat template")
    speech = data["speech"]
    if (
        not isinstance(speech, dict)
        or set(speech) != {"nodeRole", "gpus", "asr", "tts"}
        or speech["nodeRole"] != "control-speech"
        or speech["gpus"] != 1
    ):
        raise LambdaError("Lambda speech profile is invalid")
    for key, model in {
        "asr": "nvidia/parakeet-tdt-0.6b-v3",
        "tts": "hexgrad/Kokoro-82M",
    }.items():
        item = speech[key]
        if not isinstance(item, dict) or set(item) != {"model", "revision"}:
            raise LambdaError("Lambda speech model fields are invalid")
        if item["model"] != model or not _revision(item["revision"]):
            raise LambdaError("Lambda speech model revision drift")
    nvfp4 = data["optionalNvfp4"]
    if (
        not isinstance(nvfp4, dict)
        or set(nvfp4) != {"gpu", "baseModel", "checkpoint", "revision", "sameHardwareBf16Required"}
        or nvfp4["gpu"] != "B200"
        or nvfp4["baseModel"] != pools["interactive"]["model"]
        or nvfp4["checkpoint"] != "nvidia/Qwen3-8B-NVFP4"
        or not _revision(nvfp4["revision"])
        or nvfp4["sameHardwareBf16Required"] is not True
    ):
        raise LambdaError("Lambda NVFP4 comparison is not matched or reviewed")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return {**data, "sha256": hashlib.sha256(canonical).hexdigest()}
