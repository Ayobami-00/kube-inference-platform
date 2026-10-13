#!/usr/bin/env python3
"""Verify all 12 pilot identities and reject spoofing without printing keys."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TENANTS = ("tenant-a", "tenant-b", "tenant-c")
CLASSES = ("chat", "agentic", "batch", "voice")


def request_status(url: str, headers: dict[str, str], body: dict | None = None) -> int:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            if body is not None:
                payload = json.load(response)
                choices = payload.get("choices")
                if (
                    not isinstance(choices, list)
                    or not choices
                    or not choices[0].get("message", {}).get("content")
                ):
                    raise ValueError("authorized chat completion was empty")
            return response.status
    except HTTPError as error:
        return error.code


def run(url: str, keys_path: Path) -> dict[str, object]:
    credentials = json.loads(keys_path.read_text(encoding="utf-8"))
    if set(credentials) != set(TENANTS) or any(
        set(credentials[tenant]) != set(CLASSES) for tenant in TENANTS
    ):
        raise ValueError("pilot credentials have unexpected tenant/class names")
    models_url = f"{url}/v1/models"
    if request_status(models_url, {}) != 403:
        raise ValueError("Gateway accepted an unauthenticated request")
    checked = 0
    for tenant in TENANTS:
        for workload_class in CLASSES:
            token = credentials[tenant][workload_class]
            headers = {"Authorization": f"Bearer {token}"}
            if request_status(models_url, headers) != 200:
                raise ValueError(f"authorized model discovery failed for {tenant}/{workload_class}")
            if (
                request_status(
                    models_url,
                    {**headers, "x-llm-d-inference-objective": "kip-voice"},
                )
                != 403
            ):
                raise ValueError("canonical objective spoofing was accepted")
            if (
                request_status(
                    models_url,
                    {**headers, "x-gateway-inference-fairness-id": "tenant-c"},
                )
                != 403
            ):
                raise ValueError("deprecated fairness spoofing was accepted")
            body = {
                "model": "qwen2.5-7b-instruct",
                "messages": [{"role": "user", "content": "Reply with ready."}],
                "temperature": 0,
                "max_tokens": 16,
            }
            if (
                request_status(
                    f"{url}/v1/chat/completions",
                    {**headers, "Content-Type": "application/json"},
                    body,
                )
                != 200
            ):
                raise ValueError(f"authorized chat failed for {tenant}/{workload_class}")
            checked += 1
    return {"unauthenticated": "DENIED", "spoofedHeaders": "DENIED", "identitiesChecked": checked}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify private pilot tenant authorization")
    parser.add_argument("--url", required=True)
    parser.add_argument("--keys-path", required=True, type=Path)
    args = parser.parse_args()
    if not args.url.startswith("http://"):
        parser.error("--url must be the private HTTP Gateway")
    try:
        print(json.dumps(run(args.url.rstrip("/"), args.keys_path), sort_keys=True))
    except (OSError, ValueError, URLError) as error:
        print(f"tenant smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
