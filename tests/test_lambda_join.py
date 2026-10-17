"""Credential-safe, ownership-first Lambda worker join fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.lambda_cloud import LambdaError
from fleet.lambda_join import _address, join
from fleet.lambda_rental import _save_private, new_lease


def _node(role: str, region: str, identity: str) -> dict:
    return {
        "role": role,
        "phase": "active",
        "name": f"kip-lambda-test-{role}",
        "instanceId": identity,
        "instanceType": "fixture",
        "region": region,
        "priceCentsPerHour": 100,
        "createdAt": "2026-10-13T10:00:00Z",
        "endedAt": None,
    }


def _instance(node: dict, public: str, private: str) -> dict:
    return {
        "id": node["instanceId"],
        "name": node["name"],
        "status": "active",
        "region": {"name": node["region"]},
        "ip": public,
        "private_ip": private,
        "tags": [
            {"key": "kip-campaign", "value": "fixed-by-test"},
            {"key": "kip-role", "value": node["role"]},
        ],
    }


def test_join_rejects_cross_region_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = new_lease("us-east-1")
    lease["campaignId"] = "a" * 32
    lease["nodes"]["control-speech"] = _node("control-speech", "us-east-1", "control00")
    lease["nodes"]["small-1"] = _node("small-1", "us-west-3", "worker000")
    path = tmp_path / "lease.json"
    _save_private(path, lease)
    instances = [
        _instance(lease["nodes"]["control-speech"], "192.0.2.1", "10.0.0.1"),
        _instance(lease["nodes"]["small-1"], "192.0.2.2", "10.0.0.2"),
    ]
    for item in instances:
        item["tags"][0]["value"] = lease["campaignId"]
    monkeypatch.setattr(
        "fleet.lambda_join.LambdaClient",
        lambda: type("Client", (), {"instances": lambda self: instances})(),
    )
    monkeypatch.setattr(
        "fleet.lambda_join._ssh", lambda *_args, **_kwargs: pytest.fail("SSH invoked")
    )
    with pytest.raises(LambdaError, match="cross-region"):
        join("small-1", path)


def test_join_sends_token_only_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lease = new_lease("us-east-1")
    lease["campaignId"] = "a" * 32
    lease["nodes"]["control-speech"] = _node("control-speech", "us-east-1", "control00")
    lease["nodes"]["small-1"] = _node("small-1", "us-east-1", "worker000")
    path = tmp_path / "lease.json"
    _save_private(path, lease)
    instances = [
        _instance(lease["nodes"]["control-speech"], "1.2.3.4", "10.0.0.1"),
        _instance(lease["nodes"]["small-1"], "5.6.7.8", "10.0.0.2"),
    ]
    for item in instances:
        item["tags"][0]["value"] = lease["campaignId"]
    monkeypatch.setattr(
        "fleet.lambda_join.LambdaClient",
        lambda: type("Client", (), {"instances": lambda self: instances})(),
    )
    calls: list[tuple[str, str, str | None]] = []

    def fake_ssh(ip: str, command: str, *, input_text: str | None = None, timeout: int = 30) -> str:
        calls.append((ip, command, input_text))
        if command == "hostname":
            return lease["nodes"]["control-speech" if ip == "1.2.3.4" else "small-1"]["name"]
        if "ping -c" in command:
            return "ready"
        if "token create" in command:
            return "abcdef.0123456789abcdef"
        if "openssl x509" in command:
            return "a" * 64
        if command == "kubectl get nodes -o json":
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": lease["nodes"]["control-speech"]["name"],
                                "labels": {},
                                "annotations": {},
                            },
                            "status": {
                                "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]
                            },
                        },
                        {
                            "metadata": {
                                "name": lease["nodes"]["small-1"]["name"],
                                "labels": {},
                                "annotations": {},
                            },
                            "status": {
                                "addresses": [{"type": "InternalIP", "address": "10.0.0.2"}]
                            },
                        },
                    ]
                }
            )
        return "Fleet worker join completed: 7 passed, 0 warned, 0 failed"

    monkeypatch.setattr("fleet.lambda_join._ssh", fake_ssh)
    join("small-1", path)
    captured = capsys.readouterr()
    assert "abcdef.0123456789abcdef" not in captured.out + captured.err
    assert "abcdef.0123456789abcdef" not in " ".join(command for _, command, _ in calls)
    join_inputs = [payload for _, _, payload in calls if payload is not None]
    assert len(join_inputs) == 1 and "abcdef.0123456789abcdef" in join_inputs[0]


def test_join_address_contract() -> None:
    assert _address({"ip": "1.2.3.4"}, "ip") == "1.2.3.4"
    assert _address({"private_ip": "10.1.2.3"}, "private_ip") == "10.1.2.3"
    with pytest.raises(LambdaError):
        _address({"ip": "10.1.2.3"}, "ip")
    with pytest.raises(LambdaError):
        _address({"private_ip": "192.0.2.1"}, "private_ip")
