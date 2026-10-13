"""Deterministic synthetic traces stay inside the reviewed token/context contract."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from bench.errors import ValidationError
from bench.pilot_dataset import generate_text_dataset, save_generated
from bench.workloads import load_workload_contract


def count_words(value: str) -> int:
    return len(value.split())


@pytest.mark.parametrize("workload_class", ["chat", "agentic-style", "batch"])
def test_reproducible_synthetic_trace_respects_reviewed_ranges(workload_class: str) -> None:
    contract = load_workload_contract()
    kwargs = {
        "tenant": "tenant-b",
        "workload_class": workload_class,
        "trial": 2,
        "sessions": 12,
        "offered_sessions_per_second": 1.5,
        "count_tokens": count_words,
    }
    first = generate_text_dataset(contract, **kwargs)
    assert first == generate_text_dataset(contract, **kwargs)
    assert first.manifest["sessionCount"] == 12
    assert first.manifest["contentPolicy"] == "synthetic-fixed-lexicon-only"
    spec = contract.data["classes"][workload_class]
    assert spec["inputTokens"][0] <= first.manifest["realizedInputTokensMin"]
    assert first.manifest["realizedInputTokensMax"] <= spec["inputTokens"][1]
    assert spec["outputTokens"][0] <= first.manifest["requestedOutputTokensMin"]
    assert first.manifest["requestedOutputTokensMax"] <= spec["outputTokens"][1]
    rows = [json.loads(line) for line in first.jsonl.splitlines()]
    assert len(rows) == 12
    for row in rows:
        turns = [row] if workload_class == "batch" else row["turns"]
        assert spec["turns"][0] <= len(turns) <= spec["turns"][1]
        assert "timestamp" in turns[0]
        if workload_class != "batch":
            assert row["timestamp"] == turns[0]["timestamp"]
        assert turns[-1]["extra"]["ignore_eos"] is True
        assert (
            sum(count_words(turn["text"]) + turn.get("output_length", 0) for turn in turns) <= 6144
        )
        if workload_class == "agentic-style":
            assert all(
                turn["text"].startswith("Synthetic tool result:")
                for turn in turns
                if turn.get("role") != "system"
            )
    assert (
        first.manifest["datasetSha256"]
        != generate_text_dataset(contract, **{**kwargs, "trial": 3}).manifest["datasetSha256"]
    )


def test_batch_bursts_are_ordered_and_voice_requires_audio_harness() -> None:
    contract = load_workload_contract()
    dataset = generate_text_dataset(
        contract,
        tenant="tenant-c",
        workload_class="batch",
        trial=1,
        sessions=8,
        offered_sessions_per_second=1,
        count_tokens=count_words,
    )
    timestamps = [json.loads(line)["timestamp"] for line in dataset.jsonl.splitlines()]
    assert timestamps == sorted(timestamps)
    assert set(timestamps) == {0, 2667, 5333, 8000}
    slower = generate_text_dataset(
        contract,
        tenant="tenant-c",
        workload_class="batch",
        trial=1,
        sessions=8,
        offered_sessions_per_second=0.5,
        count_tokens=count_words,
    )
    assert slower.manifest["lastOfferedMs"] == 16000
    assert slower.manifest["arrivalSchedule"] == "four-burst-scaled-v2"
    with pytest.raises(ValidationError, match="reviewed tenant and class"):
        generate_text_dataset(
            contract,
            tenant="tenant-a",
            workload_class="voice",
            trial=1,
            sessions=1,
            offered_sessions_per_second=1,
            count_tokens=count_words,
        )


def test_raw_trace_save_is_private_idempotent_and_refuses_drift(tmp_path: Path) -> None:
    dataset = generate_text_dataset(
        load_workload_contract(),
        tenant="tenant-a",
        workload_class="chat",
        trial=1,
        sessions=2,
        offered_sessions_per_second=1,
        count_tokens=count_words,
    )
    destination = tmp_path / "ignored" / "trace.jsonl"
    save_generated(dataset, destination)
    save_generated(dataset, destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.with_suffix(".manifest.json").stat().st_mode) == 0o600
    destination.write_text("drift", encoding="utf-8")
    with pytest.raises(ValidationError, match="drift"):
        save_generated(dataset, destination)
