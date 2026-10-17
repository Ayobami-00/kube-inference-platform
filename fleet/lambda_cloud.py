"""Small, credential-safe Lambda Cloud API adapter for the reviewed campaign."""

from __future__ import annotations

import json
import re
import stat
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

API_BASE = "https://cloud.lambda.ai/api/v1"
KEY_FILE = Path(".secrets/lambda-api-key")
_SAFE_ERROR = re.compile(r"^[a-z0-9-]{1,60}(?:/[a-z0-9-]{1,80})?$|^unavailable$")
_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
_PATHS = {
    ("GET", "/instance-types"),
    ("GET", "/instances"),
    ("GET", "/images"),
    ("GET", "/ssh-keys"),
    ("POST", "/instance-operations/launch"),
    ("POST", "/instance-operations/terminate"),
}


class LambdaError(Exception):
    """An actionable error that never includes credentials or response bodies."""


def read_key(path: Path = KEY_FILE) -> str:
    if path.is_symlink() or not path.is_file():
        raise LambdaError("Lambda API key must be a regular, non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise LambdaError("Lambda API key must have mode 0600")
    key = path.read_text(encoding="utf-8").strip()
    if not _KEY.fullmatch(key):
        raise LambdaError("Lambda API key file is empty or malformed")
    return key


class LambdaClient:
    """Use the approved curl client; pass the secret on stdin, never argv."""

    def __init__(self, key_file: Path = KEY_FILE) -> None:
        self._key = read_key(key_file)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if (method, path) not in _PATHS or (method == "POST") != (body is not None):
            raise LambdaError("unsupported Lambda API request")
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--retry",
            "0",
            "--config",
            "-",
            "--request",
            method,
            "--write-out",
            "\n%{http_code}",
        ]
        if body is not None:
            command.extend(
                (
                    "--header",
                    "Content-Type: application/json",
                    "--data",
                    json.dumps(body, separators=(",", ":")),
                )
            )
        command.append(API_BASE + path)
        config = (
            f'header = "Authorization: Bearer {self._key}"\nheader = "Accept: application/json"\n'
        )
        try:
            result = subprocess.run(
                command, input=config, text=True, capture_output=True, timeout=25, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LambdaError(f"Lambda API connection failed ({type(error).__name__})") from None
        if result.returncode:
            raise LambdaError(f"Lambda API transport failed (curl exit {result.returncode})")
        try:
            raw, status_text = result.stdout.rsplit("\n", 1)
            status = int(status_text)
            payload = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise LambdaError("Lambda API returned a malformed response") from error
        if not 200 <= status < 300:
            if isinstance(payload, dict) and payload.get("error_code") == 1010:
                raise LambdaError("Lambda API edge denied this client (Cloudflare 1010)")
            item = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = item.get("code", "unavailable") if isinstance(item, dict) else "unavailable"
            if not isinstance(code, str) or not _SAFE_ERROR.fullmatch(code):
                code = "unavailable"
            raise LambdaError(f"Lambda API HTTP {status}: {code}")
        if not isinstance(payload, dict) or "data" not in payload:
            raise LambdaError("Lambda API response lacks data")
        return payload["data"]

    def instance_types(self) -> dict[str, Any]:
        value = self.request("GET", "/instance-types")
        if not isinstance(value, dict):
            raise LambdaError("Lambda instance types response is malformed")
        return value

    def instances(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/instances")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise LambdaError("Lambda instance list is malformed")
        return value

    def images(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/images")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise LambdaError("Lambda image list is malformed")
        return value

    def ssh_keys(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/ssh-keys")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise LambdaError("Lambda SSH key list is malformed")
        return value

    def launch(self, body: dict[str, Any]) -> str:
        """No retry on an uncertain outcome; caller journals intent before this call."""
        try:
            value = self.request("POST", "/instance-operations/launch", body)
        except LambdaError as error:
            raise LambdaError(
                f"Lambda launch outcome may be uncertain ({error}); inspect owned tags before retry"
            ) from None
        ids = value.get("instance_ids") if isinstance(value, dict) else None
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
            raise LambdaError("Lambda launch returned no unique instance ID; inspect owned tags")
        return ids[0]

    def terminate(self, instance_id: str) -> None:
        if not isinstance(instance_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", instance_id):
            raise LambdaError("invalid Lambda instance ID")
        try:
            self.request("POST", "/instance-operations/terminate", {"instance_ids": [instance_id]})
        except LambdaError as error:
            raise LambdaError(
                f"Lambda termination outcome may be uncertain ({error}); inspect instance state"
            ) from None


@dataclass(frozen=True)
class Offer:
    name: str
    gpu: str
    gpus: int
    vcpus: int
    memory_gib: int
    storage_gib: int
    price_cents_per_hour: int
    regions: frozenset[str]

    @property
    def hourly_usd(self) -> Decimal:
        return Decimal(self.price_cents_per_hour) / 100


def parse_offers(raw: dict[str, Any]) -> list[Offer]:
    offers: list[Offer] = []
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise LambdaError("Lambda offer entry is malformed")
        item = entry.get("instance_type")
        regions = entry.get("regions_with_capacity_available")
        if not isinstance(item, dict) or not isinstance(regions, list):
            raise LambdaError("Lambda offer fields are malformed")
        specs = item.get("specs")
        if not isinstance(specs, dict) or item.get("name") != key:
            raise LambdaError("Lambda offer identity is inconsistent")
        numeric = ("gpus", "vcpus", "memory_gib", "storage_gib")
        if any(type(specs.get(field)) is not int or specs[field] < 0 for field in numeric):
            raise LambdaError("Lambda offer resources are invalid")
        cents = item.get("price_cents_per_hour")
        if type(cents) is not int or cents <= 0:
            raise LambdaError("Lambda offer price is invalid")
        if item.get("architecture") != "x86_64":
            continue
        names = []
        for region in regions:
            name = region.get("name") if isinstance(region, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{2,40}", name):
                raise LambdaError("Lambda region is invalid")
            names.append(name)
        gpu = item.get("gpu_description")
        if not isinstance(gpu, str):
            raise LambdaError("Lambda GPU description is invalid")
        offers.append(
            Offer(
                key,
                gpu,
                specs["gpus"],
                specs["vcpus"],
                specs["memory_gib"],
                specs["storage_gib"],
                cents,
                frozenset(names),
            )
        )
    return offers


def role_candidates(offers: list[Offer], role: str) -> list[Offer]:
    roles = {
        "control-speech": (r"gpu_1x_a10", 1, 8, 32, 250),
        "small-1": (r"gpu_1x_h100_sxm[0-9]*", 1, 16, 64, 250),
        "small-2": (r"gpu_1x_h100_sxm[0-9]*", 1, 16, 64, 250),
        "paired-small-candidate": (r"gpu_2x_h100_sxm[0-9]*", 2, 32, 128, 500),
        "large": (r"gpu_4x_h100_sxm[0-9]*", 4, 64, 256, 1000),
        "nvfp4": (r"gpu_1x_b200_sxm[0-9]*", 1, 16, 64, 250),
        "adaptive-small": (r"gpu_1x_a100_sxm[0-9]+", 1, 16, 64, 250),
        "adaptive-large": (r"gpu_8x_a100_80gb_sxm[0-9]+", 8, 64, 256, 1000),
    }
    if role not in roles:
        raise LambdaError("unknown campaign node role")
    name_pattern, count, cpu, memory, disk = roles[role]
    return sorted(
        (
            offer
            for offer in offers
            if re.fullmatch(name_pattern, offer.name)
            and offer.gpus == count
            and offer.vcpus >= cpu
            and offer.memory_gib >= memory
            and offer.storage_gib >= disk
            and offer.regions
        ),
        key=lambda item: (item.price_cents_per_hour, item.name),
    )


def preferred_region(offers: list[Offer]) -> tuple[str, dict[str, Offer]]:
    """Require all core roles in one region before any rental."""
    roles = ("control-speech", "small-1", "small-2", "large")
    choices = {role: role_candidates(offers, role) for role in roles}
    if any(not value for value in choices.values()):
        raise LambdaError("A10, 1x H100 SXM, and 4x H100 SXM offers are not all available")
    regions = set.intersection(*(set(value[0].regions) for value in choices.values()))
    if not regions:
        # Multiple types per role may have different regional availability.
        regions = set.intersection(
            *({region for offer in value for region in offer.regions} for value in choices.values())
        )
    if not regions:
        raise LambdaError("no region currently has every required Lambda instance type")
    ranked = []
    for region in regions:
        selected = {
            role: next(offer for offer in value if region in offer.regions)
            for role, value in choices.items()
        }
        total = sum(offer.price_cents_per_hour for offer in selected.values())
        ranked.append((total, region, selected))
    _, region, selected = min(ranked, key=lambda item: (item[0], item[1]))
    return region, selected


def adaptive_a100_region(offers: list[Offer]) -> tuple[str, dict[str, Offer]]:
    """A10 + A100-40 + A100-80 fallback; second small replica shares 8-GPU VM."""
    requested = {
        "control-speech": "control-speech",
        "small-1": "adaptive-small",
        "large": "adaptive-large",
    }
    choices = {role: role_candidates(offers, selector) for role, selector in requested.items()}
    if any(not value for value in choices.values()):
        raise LambdaError("same-region A10/A100-40/A100-80 fallback lacks capacity")
    regions = set.intersection(
        *({region for offer in value for region in offer.regions} for value in choices.values())
    )
    if not regions:
        raise LambdaError("A10/A100 fallback types lack a common region")
    ranked = []
    for region in regions:
        selected = {
            role: next(offer for offer in value if region in offer.regions)
            for role, value in choices.items()
        }
        ranked.append((sum(o.price_cents_per_hour for o in selected.values()), region, selected))
    _, region, selected = min(ranked, key=lambda item: (item[0], item[1]))
    return region, selected
