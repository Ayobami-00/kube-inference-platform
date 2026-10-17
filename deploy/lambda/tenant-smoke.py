#!/usr/bin/env python3
"""Private, credential-safe smoke test for the Lambda tenant boundary."""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

KEYS = Path("/var/lib/kube-inference-platform/tenant-keys.json")
MODELS = ("qwen3-8b", "qwen3-next-80b-a3b-instruct")


def status(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def streaming(base: str, model: str, token: str) -> tuple[int, float]:
    parsed = urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=90)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "user", "content": "Write forty numbered, short Kubernetes facts."}
            ],
            "temperature": 0,
            "max_tokens": 160,
            "stream": True,
        }
    )
    arrivals: list[float] = []
    done = False
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body,
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {token}",
                "X-Gateway-Base-Model-Name": model,
            },
        )
        response = connection.getresponse()
        if response.status != 200 or "text/event-stream" not in response.getheader(
            "Content-Type", ""
        ):
            raise ValueError(
                f"Gateway streaming returned HTTP {response.status} or wrong content type"
            )
        while line := response.readline():
            if not line.startswith(b"data: "):
                continue
            data = line[6:].strip()
            if data == b"[DONE]":
                done = True
                break
            event = json.loads(data)
            delta = event["choices"][0]["delta"].get("content")
            if isinstance(delta, str) and delta:
                arrivals.append(time.monotonic())
    finally:
        connection.close()
    if not done or len(arrivals) < 8 or arrivals[-1] - arrivals[0] < 0.1:
        raise ValueError("Gateway SSE was incomplete, too short, or buffered")
    return len(arrivals), round((arrivals[-1] - arrivals[0]) * 1000, 1)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: tenant-smoke.py PRIVATE_GATEWAY_URL", file=sys.stderr)
        return 2
    credentials = json.loads(KEYS.read_text(encoding="utf-8"))
    base = sys.argv[1].rstrip("/")
    for model in MODELS:
        url = f"{base}/v1/models"
        selector = {"X-Gateway-Base-Model-Name": model}
        unauth, _ = status(url, selector)
        if unauth != 403:
            print(f"FAIL {model}: unauthenticated HTTP {unauth}", file=sys.stderr)
            return 1
        for tenant, workload_class in (
            ("tenant-a", "chat"),
            ("tenant-b", "agentic"),
            ("tenant-c", "batch"),
        ):
            headers = {
                **selector,
                "Authorization": f"Bearer {credentials[tenant][workload_class]}",
            }
            code, body = status(url, headers)
            if code != 200 or [item["id"] for item in json.loads(body)["data"]] != [model]:
                print(
                    f"FAIL {model}: authorized {tenant}/{workload_class} HTTP {code}",
                    file=sys.stderr,
                )
                return 1
            spoof, _ = status(
                url,
                {**headers, "X-LLM-D-Inference-Objective": "kip-qwen3-8b-voice"},
            )
            if spoof != 403:
                print(f"FAIL {model}: spoofed objective HTTP {spoof}", file=sys.stderr)
                return 1
        print(f"PASS {model}: A/B/C authorized, missing and spoofed identity denied", flush=True)
        chunks, milliseconds = streaming(base, model, credentials["tenant-a"]["chat"])
        print(f"PASS {model}: Gateway SSE deltas={chunks} span_ms={milliseconds}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
