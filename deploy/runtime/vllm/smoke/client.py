"""Dependency-free OpenAI-compatible runtime checkpoint client."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class SmokeFailure(RuntimeError):
    """Raised when an inference checkpoint does not meet its contract."""


def request_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            if response.status != 200:
                raise SmokeFailure(f"{url} returned HTTP {response.status}")
            body = response.read()
    except (OSError, urllib.error.URLError) as error:
        raise SmokeFailure(f"request failed for {url}: {error}") from error
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeFailure(f"{url} did not return valid JSON") from error


def check_health(base_url: str) -> None:
    request = urllib.request.Request(f"{base_url}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status != 200:
                raise SmokeFailure(f"/health returned HTTP {response.status}")
            response.read()
    except (OSError, urllib.error.URLError) as error:
        raise SmokeFailure(f"/health request failed: {error}") from error
    print("PASS health HTTP 200", flush=True)


def check_models(base_url: str, model: str) -> None:
    payload = request_json(f"{base_url}/v1/models")
    try:
        model_ids = [item["id"] for item in payload["data"]]
    except (KeyError, TypeError) as error:
        raise SmokeFailure("/v1/models response has an invalid shape") from error
    if model_ids != [model]:
        raise SmokeFailure(f"/v1/models returned {model_ids!r}; expected [{model!r}]")
    print(f"PASS models {model}", flush=True)


def check_chat_completion(base_url: str, model: str) -> None:
    payload = request_json(
        f"{base_url}/v1/chat/completions",
        method="POST",
        payload={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly KIP_OK"}],
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
        },
    )
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice["finish_reason"]
        usage = payload["usage"]
        prompt_tokens = int(usage["prompt_tokens"])
        completion_tokens = int(usage["completion_tokens"])
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise SmokeFailure("non-streaming chat response has an invalid shape") from error
    if not isinstance(content, str) or not content.strip() or not finish_reason:
        raise SmokeFailure("non-streaming chat response is empty or unfinished")
    if prompt_tokens <= 0 or completion_tokens <= 0:
        raise SmokeFailure("non-streaming chat response has invalid token usage")
    print(
        f"PASS chat completion prompt_tokens={prompt_tokens} completion_tokens={completion_tokens}",
        flush=True,
    )


def check_legacy_completion(base_url: str, model: str) -> None:
    payload = request_json(
        f"{base_url}/v1/completions",
        method="POST",
        payload={
            "model": model,
            "prompt": "Kubernetes inference is",
            "temperature": 0,
            "max_tokens": 24,
            "stream": False,
        },
    )
    try:
        text = payload["choices"][0]["text"]
    except (IndexError, KeyError, TypeError) as error:
        raise SmokeFailure("legacy completion response has an invalid shape") from error
    if not isinstance(text, str) or not text.strip():
        raise SmokeFailure("legacy completion returned empty text")
    print("PASS legacy completion", flush=True)


def check_streaming(base_url: str, model: str) -> None:
    parsed = urlsplit(base_url)
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname, parsed.port, timeout=180)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Write exactly sixty words explaining why incremental token streaming "
                        "helps an interactive Kubernetes inference client."
                    ),
                }
            ],
            "temperature": 0,
            "max_tokens": 128,
            "stream": True,
        }
    )
    path = f"{parsed.path.rstrip('/')}/v1/chat/completions"
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise SmokeFailure(f"streaming chat returned HTTP {response.status}")
        content_type = response.getheader("Content-Type", "").split(";", maxsplit=1)[0]
        if content_type.strip().lower() != "text/event-stream":
            raise SmokeFailure(f"streaming chat returned Content-Type {content_type!r}")

        arrivals: list[float] = []
        done = False
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                raise SmokeFailure(f"invalid SSE line: {line!r}")
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                event = json.loads(data)
                delta = event["choices"][0]["delta"].get("content")
            except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise SmokeFailure("streaming chat returned malformed JSON data") from error
            if isinstance(delta, str) and delta:
                arrivals.append(time.monotonic())
    except OSError as error:
        raise SmokeFailure(f"streaming request failed: {error}") from error
    finally:
        connection.close()

    if not done:
        raise SmokeFailure("streaming chat ended without data: [DONE]")
    if len(arrivals) < 8:
        raise SmokeFailure(f"streaming chat produced only {len(arrivals)} non-empty deltas")
    span_seconds = arrivals[-1] - arrivals[0]
    if span_seconds < 0.1:
        raise SmokeFailure(
            "streaming deltas arrived within "
            f"{span_seconds * 1000:.1f} ms; response appears buffered"
        )
    print(
        f"PASS streaming deltas={len(arrivals)} span_ms={span_seconds * 1000:.1f}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the KIP OpenAI-compatible runtime")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    try:
        check_health(base_url)
        check_models(base_url, args.model)
        check_chat_completion(base_url, args.model)
        check_legacy_completion(base_url, args.model)
        check_streaming(base_url, args.model)
    except SmokeFailure as error:
        print(f"FAIL {error}", file=sys.stderr, flush=True)
        return 1
    print("RUNTIME_SMOKE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
