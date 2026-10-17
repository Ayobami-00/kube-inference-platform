from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.lambda_cloud import LambdaError
from fleet.lambda_models import DEFAULT_PATH, load_models


def test_reviewed_model_pools_have_immutable_revisions_and_roles() -> None:
    profile = load_models()
    assert len(profile["sha256"]) == 64
    assert profile["pools"]["interactive"]["workloadClasses"] == ["chat", "voice"]
    assert profile["pools"]["reasoning-batch"]["gpusPerReplica"] == 4
    assert profile["optionalNvfp4"]["sameHardwareBf16Required"] is True


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("interactive", "revision", "main"),
        ("interactive", "replicas", 3),
        ("interactive", "dtype", "float8"),
        ("reasoning-batch", "tensorParallelSize", 2),
        ("reasoning-batch", "speculation", {"method": "ngram"}),
    ],
)
def test_model_profile_drift_is_refused(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    data = json.loads(DEFAULT_PATH.read_text())
    data["pools"][section][key] = value
    path = tmp_path / "models.json"
    path.write_text(json.dumps(data))
    with pytest.raises(LambdaError, match="drift"):
        load_models(path)


def test_unreviewed_model_field_is_refused(tmp_path: Path) -> None:
    data = json.loads(DEFAULT_PATH.read_text())
    data["pools"]["interactive"]["trustRemoteCode"] = True
    path = tmp_path / "models.json"
    path.write_text(json.dumps(data))
    with pytest.raises(LambdaError, match="fields"):
        load_models(path)
