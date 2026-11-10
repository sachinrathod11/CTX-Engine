"""Local API + web app. Listens on 127.0.0.1 only; your code never leaves the laptop
unless you paste the generated prompt somewhere."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, tokens
from .config import Config
from .manager import ProjectManager

WEB_DIR = Path(__file__).parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


class AddProject(BaseModel):
    path: str


class PromptRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    budget: Optional[int] = Field(None, ge=500, le=64000)
    include: List[str] = []
    exclude: List[str] = []
    use_llm: bool = True
    rewrite: bool = True


class IndexRequest(BaseModel):
    summarize: Optional[bool] = None


def create_app(cfg: Optional[Config] = None, manager: Optional[ProjectManager] = None) -> FastAPI:
    cfg = cfg or Config.load()
    mgr = manager or ProjectManager(cfg)
    @asynccontextmanager
    async def lifespan(_app):
        yield
        mgr.shutdown()

    app = FastAPI(title="ctx engine", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.state.manager = mgr

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # blocks DNS-rebinding: a web page on another site can't talk to this server
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "ctx only accepts requests to localhost"}, status_code=403)
        return await call_next(request)

    def get(pid: str):
        if pid not in mgr.projects:
            raise HTTPException(404, "Unknown project")
        return pid

    @app.get("/api/health")
    def health():
        return {"version": __version__, "llm": mgr.llm.status(), "tokenizer": tokens.method(),
                "budget": cfg.budget, "max_files": cfg.max_files}

    @app.get("/api/projects")
    def projects():
        return mgr.list()

    @app.post("/api/projects")
    def add_project(body: AddProject):
        try:
            return mgr.add(body.path)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/projects/{pid}")
    def remove_project(pid: str):
        mgr.remove(get(pid))
        return {"ok": True}

    @app.get("/api/projects/{pid}")
    def project_status(pid: str):
        return mgr.status(get(pid))

    @app.post("/api/projects/{pid}/index")
    def reindex(pid: str, body: IndexRequest = IndexRequest()):
        return mgr.start_index(get(pid), summarize=body.summarize).as_dict()

    @app.get("/api/projects/{pid}/overview")
    def overview(pid: str):
        idx = mgr.reader(get(pid))
        return {"text": idx.overview(), "data": idx.store.get_meta("overview")}

    @app.get("/api/projects/{pid}/files")
    def files(pid: str, q: str = ""):
        rows = mgr.reader(get(pid)).store.all_files()
        q = q.lower()
        return [r["path"] for r in rows if q in r["path"].lower()][:50]

    @app.post("/api/projects/{pid}/prompt")
    def prompt(pid: str, body: PromptRequest):
        try:
            res = mgr.build_prompt(get(pid), question=body.question, budget=body.budget, include=body.include,
                                   exclude=body.exclude, use_llm=body.use_llm, rewrite=body.rewrite)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e))
        return res.to_dict()

    @app.get("/api/projects/{pid}/history")
    def history(pid: str):
        return mgr.history(get(pid))

    @app.get("/")
    def index_page():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app
