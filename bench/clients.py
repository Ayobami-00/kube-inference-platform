"""Small, dependency-free clients for Prometheus and OpenCost report inputs."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .errors import BenchError, ValidationError


def _opencost_window(start: str, end: str) -> str:
    """Return the outward-rounded whole-second window accepted by OpenCost."""

    def parse(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValidationError("OpenCost window timestamps must be ISO 8601") from error
        if parsed.tzinfo is None:
            raise ValidationError("OpenCost window timestamps must include a UTC offset")
        return parsed.astimezone(UTC)

    started = parse(start)
    ended = parse(end)
    if ended <= started:
        raise ValidationError("OpenCost window end must be after its start")
    started = started.replace(microsecond=0)
    if ended.microsecond:
        ended = ended.replace(microsecond=0) + timedelta(seconds=1)
    return f"{started.strftime('%Y-%m-%dT%H:%M:%SZ')},{ended.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _validate_base_url(value: str, service: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError(f"{service} base URL must be HTTP(S)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError(
            f"{service} base URL must not contain credentials, query, or fragment"
        )
    return value.rstrip("/")


@dataclass(frozen=True)
class JsonHttpClient:
    """Bounded JSON-over-HTTP transport."""

    base_url: str
    service: str
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url, self.service))
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise ValidationError("HTTP client timeout must be in (0, 120] seconds")

    def get(self, path: str, query: dict[str, str | int | float]) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path:
            raise ValidationError(f"invalid {self.service} API path")
        url = f"{self.base_url}{path}?{urlencode(query)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                if response.status != 200:
                    raise BenchError(f"{self.service} returned HTTP {response.status}")
                payload = json.load(response)
        except HTTPError as error:
            raise BenchError(f"{self.service} returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise BenchError(f"cannot query {self.service}: {error}") from error
        if not isinstance(payload, dict):
            raise BenchError(f"{self.service} returned a non-object JSON response")
        return payload


class PrometheusClient:
    """Read-only Prometheus HTTP API client."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 15) -> None:
        self._http = JsonHttpClient(base_url, "Prometheus", timeout_seconds)

    def query_range(
        self,
        expression: str,
        *,
        start: str | float,
        end: str | float,
        step: str = "5s",
    ) -> list[dict[str, Any]]:
        """Query an exact report interval without interpreting the result series."""
        if not expression.strip() or len(expression) > 4096:
            raise ValidationError("Prometheus expression must be non-empty and <= 4096 characters")
        payload = self._http.get(
            "/api/v1/query_range",
            {"query": expression, "start": start, "end": end, "step": step},
        )
        if payload.get("status") != "success":
            raise BenchError(f"Prometheus query failed: {payload.get('error', 'unknown error')}")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "matrix":
            raise BenchError("Prometheus range query did not return matrix data")
        result = data.get("result")
        if not isinstance(result, list):
            raise BenchError("Prometheus range query has invalid result data")
        return result


class OpenCostClient:
    """Read-only OpenCost allocation and inference-cost client."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 15) -> None:
        self._http = JsonHttpClient(base_url, "OpenCost", timeout_seconds)

    def allocation(
        self,
        *,
        start: str,
        end: str,
        aggregate: str = "namespace,controllerKind,controller",
        share_idle: bool = True,
    ) -> dict[str, Any]:
        """Fetch bill-reconciling allocation data for an exact UTC interval."""
        payload = self._http.get(
            "/allocation/compute",
            {
                "window": _opencost_window(start, end),
                "aggregate": aggregate,
                "shareIdle": str(share_idle).lower(),
                "includeIdle": "true",
                "idleByNode": "true",
                "accumulate": "true",
            },
        )
        if payload.get("code") not in {None, 200}:
            raise BenchError(
                f"OpenCost allocation query failed: {payload.get('message', 'unknown')}"
            )
        return payload

    def inference_cost(
        self,
        *,
        start: str,
        end: str,
        cost_basis: str,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        """Fetch OpenCost inference accounting in allocation or usage mode."""
        if cost_basis not in {"allocation", "usage"}:
            raise ValidationError("OpenCost cost basis must be allocation or usage")
        query = {
            "window": _opencost_window(start, end),
            "costBasis": cost_basis,
            "aggregate": "model_name,namespace",
        }
        if model_name is not None:
            if not model_name.strip() or any(character in model_name for character in "+:\n\r"):
                raise ValidationError("OpenCost model filter contains an unsupported character")
            query["filter"] = f"model_name:{model_name}"
        payload = self._http.get(
            "/inferenceCost/total",
            query,
        )
        if payload.get("code") not in {None, 200}:
            raise BenchError(
                f"OpenCost inference query failed: {payload.get('message', 'unknown')}"
            )
        return payload


class GrafanaClient:
    """Write benchmark annotations through a loopback-only Grafana endpoint."""

    __slots__ = ("_base_url", "_password", "_timeout_seconds", "_username")

    def __init__(
        self, base_url: str, username: str, password: str, *, timeout_seconds: float = 15
    ) -> None:
        validated = _validate_base_url(base_url, "Grafana")
        hostname = urlsplit(validated).hostname
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValidationError("Grafana credentials may only be sent to a loopback endpoint")
        if not username or not password:
            raise ValidationError("Grafana credentials must be non-empty")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValidationError("HTTP client timeout must be in (0, 120] seconds")
        self._base_url = validated
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"GrafanaClient(base_url={self._base_url!r}, credentials=<redacted>)"

    def create_region_annotation(
        self,
        *,
        start_milliseconds: int,
        end_milliseconds: int,
        tags: list[str],
        text: str,
    ) -> int:
        """Create one exact-window region annotation and return its Grafana ID."""
        if start_milliseconds < 0 or end_milliseconds <= start_milliseconds:
            raise ValidationError("Grafana annotation requires a positive time region")
        if not tags or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValidationError("Grafana annotation tags must be non-empty strings")
        if not text.strip():
            raise ValidationError("Grafana annotation text must be non-empty")
        return self._create_annotation(
            {
                "time": start_milliseconds,
                "timeEnd": end_milliseconds,
                "isRegion": True,
                "tags": tags,
                "text": text,
            }
        )

    def create_point_annotation(
        self, *, timestamp_milliseconds: int, tags: list[str], text: str
    ) -> int:
        """Create one point annotation before benchmark traffic begins."""
        if timestamp_milliseconds < 0:
            raise ValidationError("Grafana annotation timestamp must be non-negative")
        if not tags or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValidationError("Grafana annotation tags must be non-empty strings")
        if not text.strip():
            raise ValidationError("Grafana annotation text must be non-empty")
        return self._create_annotation(
            {
                "time": timestamp_milliseconds,
                "isRegion": False,
                "tags": tags,
                "text": text,
            }
        )

    def _create_annotation(self, payload: dict[str, Any]) -> int:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        request = Request(
            f"{self._base_url}/api/annotations",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                if response.status not in {200, 201}:
                    raise BenchError(f"Grafana returned HTTP {response.status}")
                payload = json.load(response)
        except HTTPError as error:
            raise BenchError(f"Grafana returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise BenchError("cannot record Grafana benchmark annotation") from error
        annotation_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(annotation_id, bool) or not isinstance(annotation_id, int):
            raise BenchError("Grafana annotation response did not contain a numeric ID")
        return annotation_id
