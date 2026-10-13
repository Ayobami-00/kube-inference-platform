#!/usr/bin/env python3
"""Pilot-only, fail-closed tenant/class authorization for Envoy external auth.

The service never logs credentials, prompts, or request bodies. Envoy sends
only identity and documented llm-d control headers to this service.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TENANTS = ("tenant-a", "tenant-b", "tenant-c")
CLASSES = ("chat", "agentic", "batch", "voice")
OBJECTIVE = {
    "chat": "kip-interactive",
    "agentic": "kip-interactive",
    "batch": "kip-batch",
    "voice": "kip-voice",
}
DEPRECATED_CONTROL_HEADERS = frozenset(
    {
        "x-gateway-inference-fairness-id",
        "x-gateway-inference-objective",
        "x-gateway-model-name-rewrite",
        "x-slo-ttft-ms",
        "x-slo-tpot-ms",
    }
)
TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")


def load_credentials(path: Path) -> dict[str, dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != set(TENANTS):
        raise ValueError("tenant credential set must contain exactly A, B, and C")
    tokens: set[str] = set()
    for tenant in TENANTS:
        classes = value[tenant]
        if not isinstance(classes, dict) or set(classes) != set(CLASSES):
            raise ValueError("each tenant needs exactly four workload-class credentials")
        for workload_class in CLASSES:
            token = classes[workload_class]
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or token in tokens:
                raise ValueError("credential format or uniqueness is invalid")
            tokens.add(token)
    return value


def authorize(
    headers: Mapping[str, str], credentials: Mapping[str, Mapping[str, str]]
) -> tuple[int, dict[str, str]]:
    """Return an HTTP status and only trusted headers; never echo input values."""
    pairs = [(key.lower(), value) for key, value in headers.items()]
    if len(pairs) != len({key for key, _ in pairs}):
        return 403, {}
    normalized = dict(pairs)
    if any(
        name.startswith(("x-llm-d-", "x-kip-")) or name in DEPRECATED_CONTROL_HEADERS
        for name in normalized
    ):
        return 403, {}
    authorization = normalized.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return 403, {}
    token = authorization[7:]
    if not TOKEN_RE.fullmatch(token):
        return 403, {}
    match: tuple[str, str] | None = None
    for tenant in TENANTS:
        for workload_class in CLASSES:
            if hmac.compare_digest(token, credentials[tenant][workload_class]):
                match = (tenant, workload_class)
    if match is None:
        return 403, {}
    tenant, workload_class = match
    return 200, {
        "x-llm-d-inference-fairness-id": tenant,
        "x-llm-d-inference-objective": OBJECTIVE[workload_class],
        "x-kip-tenant": tenant,
        "x-kip-workload-class": workload_class,
    }


def handler_type(credentials: Mapping[str, Mapping[str, str]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._respond()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._respond()

        def _respond(self) -> None:
            if self.path == "/readyz":
                status, response_headers = 200, {}
            elif self.path == "/authorize":
                status, response_headers = authorize(self.headers, credentials)
            else:
                status, response_headers = 404, {}
            self.send_response(status)
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            # HTTP logs may contain Authorization or raw request paths.
            return

    return Handler


def main() -> None:
    path = Path(os.environ.get("KIP_TENANT_KEYS_PATH", "/var/run/kip-tenant-keys/tokens.json"))
    credentials = load_credentials(path)
    server = ThreadingHTTPServer(("0.0.0.0", 8080), handler_type(credentials))
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
