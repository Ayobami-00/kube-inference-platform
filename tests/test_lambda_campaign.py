from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_campaign import CAMPAIGN_PATH, load_campaign, trial_seed


def test_campaign_inherits_reviewed_workloads_and_is_seeded() -> None:
    campaign = load_campaign()
    assert campaign["rootSeed"] == 42
    assert campaign["modelRouting"]["voice"] == "interactive"
    assert campaign["modelRouting"]["agentic-style"] == "reasoning-batch"
    assert len(campaign["sha256"]) == 64
    assert trial_seed(campaign, "nominal-full", 1) == trial_seed(campaign, "nominal-full", 1)
    assert trial_seed(campaign, "nominal-full", 1) != trial_seed(campaign, "nominal-full", 2)
    with pytest.raises(ValueError, match="out of range"):
        trial_seed(campaign, "nominal-full", 4)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("modelRouting", "voice", "reasoning-batch"),
        ("offeredRateFractions", "burst", 2.0),
        ("variants", "nvfp4", {"pool": "interactive", "gate": "always"}),
    ],
)
def test_campaign_rejects_routing_rate_and_hardware_gate_drift(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    data = json.loads(CAMPAIGN_PATH.read_text())
    data[section][key] = value
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="drift"):
        load_campaign(path)
