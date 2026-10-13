#!/usr/bin/env python3
"""Content-free OpenAI/SSE checkpoint through the private llm-d Gateway.

This is a correctness smoke test, not a load generator or latency benchmark.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SmokeError(Exception):
    """The private Gateway failed an inference contract check."""


def _json_request(url: str, body: dict[str, object] | None = None) -> dict[str, object]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    with urlopen(Request(url, data=encoded, headers=headers), timeout=90) as response:
        if response.status != 200:
            raise SmokeError(f"{url} returned HTTP {response.status}")
        value = json.load(response)
    if not isinstance(value, dict):
        raise SmokeError(f"{url} returned non-object JSON")
    return value


def _health_request(url: str) -> None:
    with urlopen(url, timeout=20) as response:
        if response.status != 200:
            raise SmokeError(f"{url} returned HTTP {response.status}")


def stream_once(url: str, model: str, run_index: int = 0) -> dict[str, object]:
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Synthetic variant {run_index:04d}: "
                    "Write the integers from 1 through 100 in order, comma separated."
                ),
            }
        ],
        "temperature": 0,
        "max_tokens": 96,
        "stream": True,
    }
    request = Request(
        f"{url}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    content_arrivals: list[float] = []
    done = False
    with urlopen(request, timeout=180) as response:
        if response.status != 200:
            raise SmokeError(f"stream returned HTTP {response.status}")
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise SmokeError("stream did not return text/event-stream")
        for raw in response:
            arrival = time.monotonic()
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            if not line.startswith("data: "):
                raise SmokeError("stream contained malformed SSE data")
            data = line[6:]
            if data == "[DONE]":
                done = True
                break
            event = json.loads(data)
            choices = event.get("choices") if isinstance(event, dict) else None
            if not isinstance(choices, list) or not choices:
                raise SmokeError("stream event omitted choices")
            delta = choices[0].get("delta", {})
            if isinstance(delta, dict) and delta.get("content"):
                content_arrivals.append(arrival)
    if not done:
        raise SmokeError("stream did not terminate with [DONE]")
    if len(content_arrivals) < 8:
        raise SmokeError(f"only {len(content_arrivals)} content deltas arrived")
    span = content_arrivals[-1] - content_arrivals[0]
    if span < 0.1:
        raise SmokeError(f"stream content was buffered ({span:.3f}s arrival span)")
    return {"contentDeltas": len(content_arrivals), "arrivalSpanSeconds": round(span, 3)}


def smoke(url: str, model: str, runs: int, parallel: int = 1) -> dict[str, object]:
    _health_request(f"{url}/health")
    models = _json_request(f"{url}/v1/models")
    listed = models.get("data")
    if (
        not isinstance(listed, list)
        or not all(isinstance(item, dict) for item in listed)
        or [item.get("id") for item in listed] != [model]
    ):
        raise SmokeError("Gateway model discovery does not match the pinned served name")
    completion = _json_request(
        f"{url}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the word ready."}],
            "temperature": 0,
            "max_tokens": 16,
        },
    )
    choices = completion.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0].get("message", {}).get("content"), str)
        or not choices[0]["message"]["content"].strip()
    ):
        raise SmokeError("non-streaming chat completion was empty")
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        streams = list(executor.map(lambda index: stream_once(url, model, index), range(runs)))
    return {"health": "PASS", "models": "PASS", "nonStreaming": "PASS", "streams": streams}


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke the private KIP llm-d Gateway")
    parser.add_argument("--url", required=True, help="Private Gateway URL")
    parser.add_argument("--model", default="qwen2.5-7b-instruct")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    args = parser.parse_args()
    if (
        not args.url.startswith("http://")
        or args.runs < 1
        or args.runs > 20
        or args.parallel < 1
        or args.parallel > min(args.runs, 8)
    ):
        parser.error("--url must be private HTTP, --runs 1..20, --parallel 1..min(runs,8)")
    try:
        print(
            json.dumps(
                smoke(args.url.rstrip("/"), args.model, args.runs, args.parallel),
                sort_keys=True,
            )
        )
    except (SmokeError, HTTPError, URLError, ValueError, KeyError) as error:
        print(f"llm-d smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
