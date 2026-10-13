"""The control-plane join plan must never surface a bootstrap token in logs."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/fleet/create-join-plan.py"
SPEC = importlib.util.spec_from_file_location("kip_create_join_plan", SCRIPT)
assert SPEC and SPEC.loader
join = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(join)


def test_plan_is_exact_and_token_is_not_in_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, input_bytes: bytes | None = None) -> bytes:
        calls.append(args)
        if args == ("hostname",):
            return b"kip-control\n"
        if args[0] == "getent":
            return b"10.200.0.1 STREAM api.kip.internal\n"
        if args[0] == "kubectl":
            return b"ok\n"
        if args[0] == "kubeadm":
            return b"abcdef.0123456789abcdef\n"
        raise AssertionError(args)

    monkeypatch.setattr(join.os, "geteuid", lambda: 0)
    monkeypatch.setattr(join, "_run", fake_run)
    monkeypatch.setattr(join, "ca_public_key_hash", lambda: "sha256:" + "a" * 64)
    assert join.create_plan() == {
        "caHash": "sha256:" + "a" * 64,
        "controlIpv4": "10.200.0.1",
        "schemaVersion": 1,
        "token": "abcdef.0123456789abcdef",
    }
    assert ("kubeadm", "token", "create", "--ttl", "30m") in calls


def test_no_token_created_if_api_or_identity_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, input_bytes: bytes | None = None) -> bytes:
        calls.append(args)
        if args == ("hostname",):
            return b"foreign-control\n"
        raise AssertionError(args)

    monkeypatch.setattr(join.os, "geteuid", lambda: 0)
    monkeypatch.setattr(join, "_run", fake_run)
    with pytest.raises(join.JoinPlanError, match="reviewed kip-control"):
        join.create_plan()
    assert all(command[0] != "kubeadm" for command in calls)
