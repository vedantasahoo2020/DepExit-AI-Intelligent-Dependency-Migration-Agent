"""Exercise the real google-genai SDK call path against a local stand-in server (no network, no key needed)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from depexit import gemma


class _Fake(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.seen.append(sorted(body))
        if "systemInstruction" in body:  # behave like a model without system-instruction support
            return self._send(400, {"error": {"code": 400, "message": "Developer instruction is not enabled",
                                              "status": "INVALID_ARGUMENT"}})
        text = '```json\n{"action": "upgrade", "add_requirements": ["Jinja2>=3.1"], "code_changes": []}\n```'
        self._send(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def fake_api(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GEMINI_BASE_URL", "http://127.0.0.1:%d" % server.server_address[1])
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    yield
    server.shutdown()


def test_propose_through_sdk_with_fallback(fake_api):
    g = gemma.Gemma(model="gemma-4-26b-a4b-it")
    out = gemma.normalise(g.propose({"dependency": {"name": "jinja2"}}))
    assert out["action"] == "upgrade"
    assert out["add_requirements"] == [{"spec": "Jinja2>=3.1", "group": "runtime"}]
    assert g._plain is True  # fell back after the 400
    assert ["contents", "generationConfig"] in _Fake.seen
    assert g.calls and g.calls[-1]["model"] == "gemma-4-26b-a4b-it"
