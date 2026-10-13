"""Strict task-owned fleet inventory; generated instances live only in ignored work data."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vast import FleetError

PILOT_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
NODE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
MANAGED_BY = "kube-inference-platform"


@dataclass(frozen=True)
class FleetNode:
    role: str
    name: str
    instance_id: int
    machine_id: int
    private_ip: str
    gpu_model: str | None
    physical_gpu_count: int
    billed_hourly_usd: float


@dataclass(frozen=True)
class FleetInventory:
    pilot_id: str
    api_endpoint: str
    nodes: tuple[FleetNode, ...]


def _positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FleetError(f"inventory {name} must be an integer >= {minimum}")
    return value


def _node(raw: Any) -> FleetNode:
    if not isinstance(raw, dict) or set(raw) != {
        "role",
        "name",
        "instanceId",
        "machineId",
        "privateIpv4",
        "gpuModel",
        "physicalGpuCount",
        "billedHourlyUsd",
    }:
        raise FleetError("inventory node has invalid fields")
    role = raw["role"]
    if role not in {"control-plane", "gpu-worker", "speech-worker"}:
        raise FleetError("inventory node has unsupported role")
    name = raw["name"]
    if not isinstance(name, str) or not NODE_NAME.fullmatch(name):
        raise FleetError("inventory node has invalid Kubernetes name")
    try:
        private_ip = ipaddress.IPv4Address(raw["privateIpv4"])
    except (ValueError, TypeError) as error:
        raise FleetError("inventory node privateIpv4 must be an IPv4 address") from error
    if not private_ip.is_private or private_ip.is_loopback or private_ip.is_link_local:
        raise FleetError("inventory node privateIpv4 must be a private mesh address")
    gpu_count = _positive_int(raw["physicalGpuCount"], "physicalGpuCount", minimum=0)
    model = raw["gpuModel"]
    if role == "control-plane":
        if (gpu_count == 0) != (model is None):
            raise FleetError("control-plane GPU model/count must reflect physical hardware")
    elif gpu_count < 1 or not isinstance(model, str) or not model.strip():
        raise FleetError("GPU worker must identify at least one physical GPU")
    rate = raw["billedHourlyUsd"]
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (float, int))
        or not math.isfinite(rate)
        or rate <= 0
    ):
        raise FleetError("inventory billedHourlyUsd must be positive")
    return FleetNode(
        role=role,
        name=name,
        instance_id=_positive_int(raw["instanceId"], "instanceId"),
        machine_id=_positive_int(raw["machineId"], "machineId"),
        private_ip=str(private_ip),
        gpu_model=model,
        physical_gpu_count=gpu_count,
        billed_hourly_usd=float(rate),
    )


def load_inventory(path: Path) -> FleetInventory:
    """Validate exact node identity and ownership; never persist credentials here."""
    if path.is_symlink():
        raise FleetError("fleet inventory must not be a symlink")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("fleet inventory is missing or malformed") from error
    if not isinstance(raw, dict) or set(raw) != {
        "schemaVersion",
        "managedBy",
        "pilotId",
        "apiEndpoint",
        "nodes",
    }:
        raise FleetError("fleet inventory fields differ")
    if raw["schemaVersion"] != 1 or raw["managedBy"] != MANAGED_BY:
        raise FleetError("fleet inventory is not KIP-owned schema v1")
    pilot_id = raw["pilotId"]
    if not isinstance(pilot_id, str) or not PILOT_ID.fullmatch(pilot_id):
        raise FleetError("fleet pilotId is invalid")
    endpoint = raw["apiEndpoint"]
    if endpoint != "api.kip.internal:6443":
        raise FleetError("fleet API endpoint must be the reviewed private DNS name")
    if not isinstance(raw["nodes"], list) or len(raw["nodes"]) not in {3, 4}:
        raise FleetError("pilot inventory must contain three nodes, or four during voice")
    nodes = tuple(_node(value) for value in raw["nodes"])
    roles = [node.role for node in nodes]
    if roles.count("control-plane") != 1 or roles.count("gpu-worker") != 2:
        raise FleetError("pilot inventory requires one control plane and two GPU workers")
    if len(nodes) == 4 and roles.count("speech-worker") != 1:
        raise FleetError("fourth pilot node must be the speech worker")
    if len(nodes) == 3 and "speech-worker" in roles:
        raise FleetError("speech worker cannot replace a model worker")
    for attribute in ("name", "instance_id", "machine_id", "private_ip"):
        if len({getattr(node, attribute) for node in nodes}) != len(nodes):
            raise FleetError(f"pilot inventory has duplicate {attribute}")
    if sum(node.billed_hourly_usd for node in nodes) > 4.0:
        raise FleetError("pilot billed hourly rate exceeds $4")
    return FleetInventory(pilot_id, endpoint, nodes)
