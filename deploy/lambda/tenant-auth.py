#!/usr/bin/env python3
"""Fail-closed external authorization for the two Lambda model pools.

Credentials encode tenant and workload class; clients never provide their own
llm-d fairness identity or priority. The selected model header only chooses
the already-reviewed pool-specific objective.
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
MODELS = {
    "qwen3-8b": "kip-qwen3-8b",
    "qwen3-next-80b-a3b-instruct": "kip-qwen3-next",
}
CLASS_BAND = {"chat": "interactive", "agentic": "interactive", "batch": "batch", "voice": "voice"}
TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
CONTROL_HEADERS = frozenset(
    {
        "x-gateway-inference-fairness-id",
        "x-gateway-inference-objective",
        "x-gateway-model-name-rewrite",
        "x-slo-ttft-ms",
        "x-slo-tpot-ms",
    }
)


def load_credentials(path: Path) -> dict[str, dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != set(TENANTS):
        raise ValueError("tenant credential set must contain exactly A, B, and C")
    seen: set[str] = set()
    for tenant in TENANTS:
        classes = value[tenant]
        if not isinstance(classes, dict) or set(classes) != set(CLASSES):
            raise ValueError("each tenant must have four class credentials")
        for name in CLASSES:
            token = classes[name]
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or token in seen:
                raise ValueError("tenant credential format or uniqueness is invalid")
            seen.add(token)
    return value


def authorize(
    headers: Mapping[str, str], credentials: Mapping[str, Mapping[str, str]]
) -> tuple[int, dict[str, str]]:
    pairs = [(key.lower(), value) for key, value in headers.items()]
    if len(pairs) != len({key for key, _ in pairs}):
        return 403, {}
    normalized = dict(pairs)
    if any(
        name.startswith(("x-llm-d-", "x-kip-")) or name in CONTROL_HEADERS for name in normalized
    ):
        return 403, {}
    model = normalized.get("x-gateway-base-model-name")
    if model not in MODELS:
        return 403, {}
    auth = normalized.get("authorization", "")
    if not auth.startswith("Bearer ") or not TOKEN_RE.fullmatch(auth[7:]):
        return 403, {}
    match: tuple[str, str] | None = None
    for tenant in TENANTS:
        for name in CLASSES:
            if hmac.compare_digest(auth[7:], credentials[tenant][name]):
                match = (tenant, name)
    if match is None:
        return 403, {}
    tenant, workload_class = match
    return 200, {
        "x-llm-d-inference-fairness-id": tenant,
        "x-llm-d-inference-objective": f"{MODELS[model]}-{CLASS_BAND[workload_class]}",
        "x-kip-tenant": tenant,
        "x-kip-workload-class": workload_class,
    }


def handler_type(credentials: Mapping[str, Mapping[str, str]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._respond()

        def do_POST(self) -> None:  # noqa: N802
            self._respond()

        def _respond(self) -> None:
            if self.path == "/readyz":
                status, output = 200, {}
            elif self.path == "/authorize":
                status, output = authorize(self.headers, credentials)
            else:
                status, output = 404, {}
            self.send_response(status)
            for name, value in output.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def main() -> None:
    path = Path(os.environ.get("KIP_TENANT_KEYS_PATH", "/var/run/kip-tenant-keys/tokens.json"))
    credentials = load_credentials(path)
    ThreadingHTTPServer(("0.0.0.0", 8080), handler_type(credentials)).serve_forever(
        poll_interval=0.5
    )


if __name__ == "__main__":
    main()
