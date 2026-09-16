"""Protocol tests for the dependency-free runtime smoke client."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

ROOT = Path(__file__).parents[1]
CLIENT_PATH = ROOT / "deploy" / "runtime" / "vllm" / "smoke" / "client.py"
MODEL = "qwen2.5-7b-instruct"


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kip_runtime_smoke_client", CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLIENT = load_client()


class RuntimeHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "success"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def send_json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(503 if self.mode == "http-error" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/v1/models":
            if self.mode == "malformed-json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{not-json")
                return
            model = "wrong-model" if self.mode == "wrong-model" else MODEL
            self.send_json({"data": [{"id": model}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.path == "/v1/completions":
            text = "" if self.mode == "empty-completion" else " reliable and measurable"
            self.send_json({"choices": [{"text": text}]})
            return
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if not payload.get("stream"):
            self.send_json(
                {
                    "choices": [
                        {
                            "message": {"content": "" if self.mode == "empty-chat" else "KIP_OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 2},
                }
            )
            return

        self.send_response(200)
        content_type = (
            "application/json" if self.mode == "wrong-content-type" else "text/event-stream"
        )
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if self.mode == "malformed-sse":
            self.wfile.write(b"event: token\n\n")
            self.wfile.flush()
            return
        if self.mode == "malformed-sse-json":
            self.wfile.write(b"data: {not-json\n\n")
            self.wfile.flush()
            return
        delta_count = 4 if self.mode == "too-few-deltas" else 8
        for index in range(delta_count):
            event = {"choices": [{"delta": {"content": f"token-{index} "}}]}
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            if self.mode != "buffered":
                time.sleep(0.02)
        if self.mode != "missing-done":
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()


@pytest.fixture
def runtime_server() -> tuple[str, type[RuntimeHandler]]:
    handler = type("ConfiguredRuntimeHandler", (RuntimeHandler,), {"mode": "success"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url, handler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_smoke_client_accepts_healthy_incremental_runtime(
    runtime_server: tuple[str, type[RuntimeHandler]],
) -> None:
    base_url, _ = runtime_server

    CLIENT.check_health(base_url)
    CLIENT.check_models(base_url, MODEL)
    CLIENT.check_chat_completion(base_url, MODEL)
    CLIENT.check_legacy_completion(base_url, MODEL)
    CLIENT.check_streaming(base_url, MODEL)


@pytest.mark.parametrize(
    ("mode", "function", "message"),
    [
        ("wrong-model", "models", "expected"),
        ("malformed-json", "models", "valid JSON"),
        ("http-error", "health", "health request failed"),
        ("empty-chat", "chat", "empty or unfinished"),
        ("empty-completion", "completion", "empty text"),
        ("wrong-content-type", "stream", "Content-Type"),
        ("malformed-sse", "stream", "invalid SSE line"),
        ("malformed-sse-json", "stream", "malformed JSON data"),
        ("too-few-deltas", "stream", "only 4 non-empty deltas"),
        ("missing-done", "stream", r"without data: \[DONE\]"),
        ("buffered", "stream", "appears buffered"),
    ],
)
def test_smoke_client_rejects_invalid_or_buffered_responses(
    runtime_server: tuple[str, type[RuntimeHandler]],
    mode: str,
    function: str,
    message: str,
) -> None:
    base_url, handler = runtime_server
    handler.mode = mode

    with pytest.raises(CLIENT.SmokeFailure, match=message):
        if function == "models":
            CLIENT.check_models(base_url, MODEL)
        elif function == "health":
            CLIENT.check_health(base_url)
        elif function == "chat":
            CLIENT.check_chat_completion(base_url, MODEL)
        elif function == "completion":
            CLIENT.check_legacy_completion(base_url, MODEL)
        else:
            CLIENT.check_streaming(base_url, MODEL)
