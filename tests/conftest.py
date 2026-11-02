"""Общие фикстуры: синтетический стенд в tmp, индекс Qdrant в памяти, эмбеддинги без скачивания моделей."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from triage.config import ROOT, get_settings
from triage.embedder import HashingEmbedder
from triage.logparse import build_chunks, load_logs
from triage.store import Index, load_runbooks
from triage.synth import generate
from triage.tools import LogTools


@pytest.fixture(scope="session")
def stand(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("stand")
    generate(data_dir, seed=7)
    settings = get_settings(data_dir=data_dir, runbooks_dir=ROOT / "runbooks", embedder="hashing", qdrant_url=None)
    index = Index(HashingEmbedder(), location=":memory:")
    index.build(build_chunks(load_logs(settings.logs_dir)), load_runbooks(settings.runbooks_dir))
    yield LogTools(settings, index)
    index.close()


@pytest.fixture(scope="session")
def incidents(stand):
    return json.loads(stand.settings.incidents_path.read_text(encoding="utf-8"))


class FakeLLM:
    """Отдаёт заранее заданные ответы: dict — для json(), список ответов — для chat_tools()."""

    model = "fake"

    def __init__(self, analysis=None, tool_replies=None, error=None):
        self.analysis = analysis
        self.tool_replies = list(tool_replies or [])
        self.error = error
        self.calls = []

    def json(self, messages, schema, name):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.analysis

    def chat_tools(self, messages, tools):
        self.calls.append(messages)
        return self.tool_replies.pop(0)


@pytest.fixture
def fake_llm():
    return FakeLLM


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        status, payload = self.server.responder(body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def completion(content: str) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def openai_server():
    """Фейковый OpenAI-совместимый сервер: responder(body) → (status, json)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.requests = []
    server.responder = lambda body: (200, completion("{}"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield server
    server.shutdown()
    server.server_close()
