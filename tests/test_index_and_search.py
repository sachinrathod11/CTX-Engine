import time

import pytest

from ctx import tokens
from ctx.index.indexer import ProjectIndex
from ctx.index.watcher import IndexWatcher
from ctx.prompt.builder import PromptBuilder, whole_repo_tokens


def selected(res):
    return [f.path for f in res.files if f.selected]


def test_scanner_skips_secrets_and_ignored(demo, offline_cfg):
    (demo / "node_modules" / "x").mkdir(parents=True)
    (demo / "node_modules" / "x" / "index.js").write_text("module.exports = 1")
    (demo / ".gitignore").write_text("build_output/\n")
    (demo / "build_output").mkdir()
    (demo / "build_output" / "a.py").write_text("x = 1")
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    paths = set(idx.store.file_hashes())
    assert ".env" not in paths
    assert not any(p.startswith(("node_modules", "build_output", ".ctx")) for p in paths)
    assert "server/auth/routes.py" in paths


def test_graph_links_frontend_calls_to_backend_routes(demo, offline_cfg):
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    rows = idx.store.db.execute(
        "SELECT a.path s, b.path d, e.detail FROM edges e JOIN files a ON a.id=e.src JOIN files b ON b.id=e.dst WHERE kind='http'").fetchall()
    links = {(r["s"], r["d"], r["detail"]) for r in rows}
    assert ("web/src/api/auth.js", "server/auth/routes.py", "POST /api/auth/login") in links
    assert ("web/src/api/products.js", "server/products/routes.py", "GET /api/products") in links
    imports = {(r["s"], r["d"]) for r in idx.store.db.execute(
        "SELECT a.path s, b.path d FROM edges e JOIN files a ON a.id=e.src JOIN files b ON b.id=e.dst WHERE kind='import'")}
    assert ("server/auth/routes.py", "server/utils/security.py") in imports       # python relative import
    assert ("web/src/components/LoginForm.jsx", "web/src/api/auth.js") in imports  # js relative import


@pytest.mark.parametrize("question,must_have", [
    ("why is the login button not working", ["web/src/components/LoginForm.jsx", "web/src/api/auth.js", "server/auth/routes.py"]),
    ("password reset email never arrives", ["server/auth/reset.py", "server/utils/email.py"]),
    ("where is the stripe payment key configured?", ["server/config.py"]),
    ("how are products searched?", ["server/products/routes.py", "web/src/api/products.js"]),
    ("checkout says please log in even after logging in", ["web/src/pages/CartPage.jsx"]),
])
def test_retrieval_offline(demo, offline_cfg, question, must_have):
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    res = PromptBuilder(idx).build(question)
    for p in must_have:
        assert p in selected(res), f"{p} missing for: {question}"


def test_prompt_respects_budget_and_redacts(demo, offline_cfg):
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    b = PromptBuilder(idx)
    for budget in (1000, 2000, 6000):
        res = b.build("where is the stripe payment key configured?", budget=budget)
        assert res.tokens <= budget * 1.05, (budget, res.tokens)
    res = b.build("where is the stripe payment key configured?", budget=6000)
    assert "sk_test_FAKE" not in res.prompt and "whsec_FAKE" not in res.prompt
    assert "[REDACTED stripe-key]" in res.prompt
    assert res.tokens < whole_repo_tokens(idx)


def test_include_and_exclude(demo, offline_cfg):
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    b = PromptBuilder(idx)
    res = b.build("why is the login button not working", include=["server/orders/routes.py"],
                  exclude=["server/auth/reset.py"])
    sel = selected(res)
    assert sel[0] == "server/orders/routes.py"
    assert "server/auth/reset.py" not in sel
    assert "### server/orders/routes.py" in res.prompt


def test_incremental_update_and_delete(demo, offline_cfg):
    idx = ProjectIndex(demo, offline_cfg)
    first = idx.update()
    assert first["changed"] == first["files"]
    again = idx.update()
    assert again["changed"] == 0 and again["removed"] == 0
    f = demo / "server" / "utils" / "security.py"
    f.write_text(f.read_text() + "\n\ndef rotate_pepper():\n    return 'x'\n")
    (demo / "server" / "utils" / "email.py").unlink()
    stats = idx.update()
    assert stats["changed"] == 1 and stats["removed"] == 1
    syms = [s["name"] for s in idx.store.symbols_of(idx.store.file_by_path("server/utils/security.py")["id"])]
    assert "rotate_pepper" in syms
    assert idx.store.file_by_path("server/utils/email.py") is None


def test_watcher_picks_up_new_file(demo, offline_cfg):
    idx = ProjectIndex(demo, offline_cfg)
    idx.update()
    done = []
    w = IndexWatcher(idx, debounce=0.3, on_update=done.append).start()
    try:
        (demo / "server" / "coupons.py").write_text("def apply_coupon(code):\n    return code.upper()\n")
        for _ in range(50):
            if idx.store.file_by_path("server/coupons.py"):
                break
            time.sleep(0.1)
    finally:
        w.stop()
    assert idx.store.file_by_path("server/coupons.py") is not None


def test_token_counter_is_sane():
    assert tokens.count("") == 0
    n = tokens.count("def hello_world():\n    return 'hi'\n" * 10)
    assert 60 <= n <= 200
