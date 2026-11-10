import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-shop"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "ctxhome"
    home.mkdir()
    monkeypatch.setenv("CTX_HOME", str(home))
    import ctx.config as c
    monkeypatch.setattr(c, "HOME_DIR", home)
    import ctx.manager as m
    monkeypatch.setattr(m, "HOME_DIR", home)
    yield home


@pytest.fixture
def demo(tmp_path):
    dst = tmp_path / "demo-shop"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns(".ctx"))
    return dst


@pytest.fixture
def offline_cfg():
    from ctx.config import Config
    cfg = Config()
    cfg.use_llm = False
    return cfg


class _FakeOllama(BaseHTTPRequestHandler):
    """Speaks the subset of the Ollama HTTP API that ctx uses."""
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3:4b"}, {"name": "embeddinggemma:latest"}]})
        else:
            self._send({"version": "fake"})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOllama.calls.append((self.path, data))
        if self.path == "/api/embed":
            from ctx.index.embedder import _hash_vec
            vecs = [(_hash_vec(t)[:256] * 1.0).tolist() for t in data["input"]]
            self._send({"embeddings": vecs})
            return
        prompt, system = data.get("prompt", ""), data.get("system", "")
        if "Candidate files" in prompt:
            # pick the candidates whose line mentions login/LoginForm, else the first two
            nums = [i for i, line in enumerate([l for l in prompt.splitlines() if l[:1].isdigit()], 1)
                    if "login" in line.lower() or "auth" in line.lower()][:4] or [1, 2]
            out = json.dumps({"files": nums, "question": "The Log in button in web/src/components/LoginForm.jsx does nothing. Check its click/submit wiring and the login() call to POST /api/auth/login."})
        elif "keywords" in system:
            out = json.dumps({"keywords": ["handleSubmit", "onSubmit", "LoginForm", "/api/auth/login"]})
        elif "summarize source files" in system:
            out = "<think>hmm</think>Handles part of the shop app. Key functions are listed in the outline."
        else:
            out = "A demo online shop with a Flask API and a React frontend."
        self._send({"response": out})


@pytest.fixture
def fake_ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _FakeOllama.calls = []
    yield f"http://127.0.0.1:{srv.server_address[1]}", _FakeOllama.calls
    srv.shutdown()
