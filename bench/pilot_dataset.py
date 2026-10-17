"""Deterministic synthetic text traces for the reviewed pilot workload contract.

The generated JSONL is a local, ignored AIPerf input, never a published report.
It contains no customer prompts, model output, credentials, or network addresses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .paths import repository_root
from .workloads import TENANTS, WorkloadContract, derived_seed, load_workload_contract

WORDS = (
    "amber",
    "atlas",
    "balance",
    "beacon",
    "cobalt",
    "compass",
    "copper",
    "delta",
    "ember",
    "fable",
    "forest",
    "harbor",
    "island",
    "lantern",
    "marble",
    "meadow",
    "meridian",
    "orbit",
    "orchid",
    "paper",
    "quartz",
    "ribbon",
    "signal",
    "silver",
    "summit",
    "tandem",
    "timber",
    "velvet",
    "window",
    "zenith",
)
SHARED_PREFIX = (
    "This is a synthetic pilot request. Use only the supplied fictional labels. "
    "No real user, account, document, or private data is represented."
)
TOKENIZER_REPOSITORY = "Qwen/Qwen2.5-7B-Instruct"
TOKENIZER_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
LAMBDA_TOKENIZERS = {
    "qwen3-8b": ("Qwen/Qwen3-8B", "b968826d9c46dd6066d109eabc6255188de91218"),
    "qwen3-next-80b-a3b-instruct": (
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        "9c7f2fbe84465e40164a94cc16cd30b6999b0cc7",
    ),
}


@dataclass(frozen=True)
class GeneratedDataset:
    """Content and safe provenance for one tenant/class/trial."""

    jsonl: str
    manifest: dict[str, Any]


def _text_for_tokens(
    target: int,
    randomizer: random.Random,
    count_tokens: Callable[[str], int],
    *,
    prefix: str = "",
) -> tuple[str, int]:
    """Use a fixed lexicon and a tokenizer-guided search, without model weights."""
    words = [randomizer.choice(WORDS) for _ in range(target * 3)]
    left, right = 1, len(words)
    best = ("", 0)
    while left <= right:
        middle = (left + right) // 2
        candidate = prefix + " ".join(words[:middle])
        actual = count_tokens(candidate)
        if abs(actual - target) < abs(best[1] - target):
            best = (candidate, actual)
        if actual < target:
            left = middle + 1
        elif actual > target:
            right = middle - 1
        else:
            return candidate, actual
    if abs(best[1] - target) > 3:
        raise ValidationError("synthetic tokenizer could not realize the requested length")
    return best


def _schedule(
    randomizer: random.Random, workload_class: str, sessions: int, rate: float
) -> list[int]:
    if rate <= 0 or rate > 1000:
        raise ValidationError("pilot offered session rate must be in (0, 1000]")
    if workload_class == "batch":
        # Four explicit launch waves spread across the requested average
        # arrival horizon. The old fixed one-second waves ignored `rate` and
        # could silently turn a 100-job, 1/s trial into a 100-job/3s burst.
        interval_ms = 1000 * sessions / (3 * rate)
        return [round(interval_ms * min(3, index * 4 // sessions)) for index in range(sessions)]
    offset = 0.0
    values: list[int] = []
    for _ in range(sessions):
        offset += randomizer.expovariate(rate)
        values.append(round(offset * 1000))
    return values


def generate_text_dataset(
    contract: WorkloadContract,
    *,
    tenant: str,
    workload_class: str,
    trial: int,
    sessions: int,
    offered_sessions_per_second: float,
    count_tokens: Callable[[str], int],
    tokenizer_repository: str = TOKENIZER_REPOSITORY,
    tokenizer_revision: str = TOKENIZER_REVISION,
) -> GeneratedDataset:
    """Translate reviewed ranges into AIPerf multi/single-turn JSONL records."""
    if tenant not in TENANTS or workload_class not in {"chat", "agentic-style", "batch"}:
        raise ValidationError("text dataset requires a reviewed tenant and class")
    if isinstance(sessions, bool) or not isinstance(sessions, int) or not 1 <= sessions <= 10000:
        raise ValidationError("pilot sessions must be an integer in [1, 10000]")
    if isinstance(trial, bool) or not isinstance(trial, int) or trial < 1:
        raise ValidationError("pilot trial must be a positive integer")
    if (tokenizer_repository, tokenizer_revision) not in {
        (TOKENIZER_REPOSITORY, TOKENIZER_REVISION),
        *LAMBDA_TOKENIZERS.values(),
    }:
        raise ValidationError("tokenizer must be a reviewed immutable model revision")
    seed = derived_seed(contract, tenant, workload_class, trial)
    randomizer = random.Random(seed)
    spec = contract.data["classes"][workload_class]
    timestamps = _schedule(randomizer, workload_class, sessions, offered_sessions_per_second)
    cap = contract.data["contextInputTokenCap"]
    content_cap = cap - 512  # Reserve chat-template and model-output headroom.
    rows: list[str] = []
    input_lengths: list[int] = []
    output_lengths: list[int] = []
    turn_counts: list[int] = []
    shared_prefix_count = 0
    for session_index in range(sessions):
        turns = randomizer.randint(*spec["turns"])
        shared = (
            workload_class != "batch"
            and randomizer.random() < contract.data["sharedPrefixFraction"]
        )
        shared_prefix_count += int(shared)
        # The first dispatchable turn must carry its timestamp. A leading
        # system-only turn makes AIPerf 0.12 reject fixed_schedule before its
        # multi-turn loader can hoist that system prompt.
        accumulated = 0
        records: list[dict[str, Any]] = []
        for turn_index in range(turns):
            remaining = turns - turn_index - 1
            minimum_future = remaining * (spec["inputTokens"][0] + spec["outputTokens"][0] + 3)
            input_max = min(
                spec["inputTokens"][1],
                content_cap - accumulated - minimum_future - spec["outputTokens"][0] - 3,
            )
            if input_max < spec["inputTokens"][0]:
                raise ValidationError("pilot context cap cannot fit the reviewed minimum turns")
            target = randomizer.randint(spec["inputTokens"][0], input_max)
            label = "Synthetic tool result: " if workload_class == "agentic-style" else ""
            prefix = label + (SHARED_PREFIX + " " if shared and turn_index == 0 else "")
            text, actual = _text_for_tokens(target, randomizer, count_tokens, prefix=prefix)
            output_max = min(
                spec["outputTokens"][1],
                content_cap - accumulated - actual - minimum_future,
            )
            if output_max < spec["outputTokens"][0]:
                raise ValidationError("pilot context cap cannot fit the requested output")
            output = randomizer.randint(spec["outputTokens"][0], output_max)
            accumulated += actual + output
            input_lengths.append(actual)
            output_lengths.append(output)
            entry: dict[str, Any] = {
                "text": text,
                "output_length": output,
                "extra": {"temperature": 0, "seed": seed % (2**31), "ignore_eos": True},
            }
            if turn_index == 0:
                entry["timestamp"] = timestamps[session_index]
            else:
                entry["delay"] = randomizer.randint(*spec["thinkTimeMs"])
            records.append(entry)
        if accumulated > content_cap:
            raise ValidationError("pilot generated context exceeded its safety cap")
        turn_counts.append(turns)
        if workload_class == "batch":
            row = records[0]
        else:
            row = {
                "session_id": f"{tenant}-{workload_class}-{trial}-{session_index + 1}",
                # AIPerf 0.12 detects fixed-schedule timing only at the root
                # JSONL record, while its multi-turn loader reads the first
                # turn's matching timestamp for actual dispatch.
                "timestamp": timestamps[session_index],
                "turns": records,
            }
        rows.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    jsonl = "\n".join(rows) + "\n"
    manifest = {
        "schemaVersion": 1,
        "contract": contract.name,
        "contractSha256": contract.sha256,
        "tenant": tenant,
        "class": workload_class,
        "trial": trial,
        "seed": seed,
        "sessionCount": sessions,
        "plannedTurnCount": sum(turn_counts),
        "offeredSessionsPerSecond": offered_sessions_per_second,
        "arrivalSchedule": (
            "four-burst-scaled-v2" if workload_class == "batch" else "seeded-poisson-v1"
        ),
        "firstOfferedMs": timestamps[0],
        "lastOfferedMs": timestamps[-1],
        "sharedPrefixSessions": shared_prefix_count,
        "realizedInputTokensMin": min(input_lengths),
        "realizedInputTokensMax": max(input_lengths),
        "requestedOutputTokensMin": min(output_lengths),
        "requestedOutputTokensMax": max(output_lengths),
        "datasetSha256": hashlib.sha256(jsonl.encode()).hexdigest(),
        "tokenizerRepository": tokenizer_repository,
        "tokenizerRevision": tokenizer_revision,
        "contentPolicy": "synthetic-fixed-lexicon-only",
    }
    return GeneratedDataset(jsonl=jsonl, manifest=manifest)


def save_generated(dataset: GeneratedDataset, destination: Path) -> None:
    """Write an ignored raw dataset and safe manifest without replacing drifted data."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = destination.with_suffix(".manifest.json")
    payloads = (
        (destination, dataset.jsonl),
        (manifest_path, json.dumps(dataset.manifest, indent=2, sort_keys=True) + "\n"),
    )
    for path, content in payloads:
        if path.exists():
            if path.is_symlink() or path.read_text(encoding="utf-8") != content:
                raise ValidationError("generated pilot dataset destination contains drift")
            continue
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate reviewed synthetic AIPerf pilot data")
    parser.add_argument("--tenant", required=True, choices=TENANTS)
    parser.add_argument(
        "--class", dest="workload_class", required=True, choices=("chat", "agentic-style", "batch")
    )
    parser.add_argument("--trial", required=True, type=int)
    parser.add_argument("--sessions", required=True, type=int)
    parser.add_argument("--rate", required=True, type=float)
    parser.add_argument("--model", choices=tuple(LAMBDA_TOKENIZERS))
    args = parser.parse_args()
    try:
        from transformers import AutoTokenizer

        contract = load_workload_contract()
        repository, revision = (
            LAMBDA_TOKENIZERS[args.model]
            if args.model
            else (TOKENIZER_REPOSITORY, TOKENIZER_REVISION)
        )
        tokenizer = AutoTokenizer.from_pretrained(
            repository, revision=revision, trust_remote_code=False
        )
        dataset = generate_text_dataset(
            contract,
            tenant=args.tenant,
            workload_class=args.workload_class,
            trial=args.trial,
            sessions=args.sessions,
            offered_sessions_per_second=args.rate,
            count_tokens=lambda text: len(tokenizer.encode(text, add_special_tokens=False)),
            tokenizer_repository=repository,
            tokenizer_revision=revision,
        )
        root = repository_root()
        dataset_root = root / "benchmarks/.work/datasets" / contract.name
        if args.model:
            dataset_root /= args.model
        destination = dataset_root / args.tenant / args.workload_class / f"trial-{args.trial}.jsonl"
        save_generated(dataset, destination)
    except (ValidationError, OSError, ValueError) as error:
        print(f"pilot dataset generation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"path": str(destination), "manifest": dataset.manifest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
