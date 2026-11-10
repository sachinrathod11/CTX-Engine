import time

from fastapi.testclient import TestClient

from ctx.config import Config
from ctx.index.indexer import ProjectIndex
from ctx.llm import Ollama
from ctx.manager import ProjectManager
from ctx.prompt.builder import PromptBuilder
from ctx.server import create_app


def test_llm_path_with_fake_ollama(demo, fake_ollama):
    url, calls = fake_ollama
    cfg = Config()
    cfg.ollama_url = url
    idx = ProjectIndex(demo, cfg, Ollama(cfg))
    stats = idx.update()
    assert stats["llm_summaries"] > 5
    assert idx.store.get_meta("embed_model") == "embeddinggemma"
    s = idx.store.file_by_path("server/auth/routes.py")
    assert s["summary_src"] == "llm" and "<think>" not in s["summary"]
    assert idx.store.get_meta("project_description").startswith("A demo online shop")
    gen = [c for p, c in calls if p == "/api/generate"]
    assert all(c["think"] is False for c in gen)

    res = PromptBuilder(idx).build("why is the login button not working")
    assert res.modes["expand"] == "llm" and res.modes["rerank"] == "llm"
    assert res.modes["vector"] == "embeddinggemma"
    assert res.rewritten.startswith("The Log in button")
    assert "web/src/components/LoginForm.jsx" in [f.path for f in res.files if f.selected]
    assert '(My original question: "why is the login button not working")' in res.prompt


def test_falls_back_when_ollama_disappears(demo, fake_ollama, tmp_path):
    url, _ = fake_ollama
    cfg = Config()
    cfg.ollama_url = url
    idx = ProjectIndex(demo, cfg, Ollama(cfg))
    idx.update(summarize=False)
    cfg.ollama_url = "http://127.0.0.1:9"  # nothing listens here
    idx.llm._models = None
    res = PromptBuilder(idx).build("why is the login button not working")
    assert res.modes["rerank"] == "heuristic" and res.modes["vector"] == "off"
    assert "web/src/api/auth.js" in [f.path for f in res.files if f.selected]


def test_server_flow(demo, offline_cfg):
    mgr = ProjectManager(offline_cfg, watch=False)
    client = TestClient(create_app(offline_cfg, mgr))
    assert client.get("/api/health").json()["llm"]["use_llm"] is False
    assert client.post("/api/projects", json={"path": str(demo / "nope")}).status_code == 400
    p = client.post("/api/projects", json={"path": str(demo)}).json()
    for _ in range(100):
        st = client.get(f"/api/projects/{p['id']}").json()
        if st["job"]["state"] == "done":
            break
        time.sleep(0.05)
    assert st["files"] >= 18 and st["http_edges"] >= 4
    r = client.post(f"/api/projects/{p['id']}/prompt", json={"question": "why is the login button not working", "budget": 3000})
    assert r.status_code == 200
    body = r.json()
    assert body["tokens"] <= 3150 and "## Code" in body["prompt"]
    assert client.post(f"/api/projects/{p['id']}/prompt", json={"question": ""}).status_code == 422
    hist = client.get(f"/api/projects/{p['id']}/history").json()
    assert hist[0]["question"] == "why is the login button not working"
    assert "server/auth/routes.py" in client.get(f"/api/projects/{p['id']}/files?q=auth").json()
    assert client.get("/").status_code == 200 and "ctx" in client.get("/").text
    # requests that don't target localhost are refused (DNS-rebinding protection)
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
    assert client.delete(f"/api/projects/{p['id']}").json()["ok"]
    assert client.get("/api/projects").json() == []
