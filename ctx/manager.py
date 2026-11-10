"""Keeps track of the user's projects for the web app: registry, background indexing
jobs with progress, file watchers and prompt history."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import HOME_DIR, Config
from .index.indexer import ProjectIndex
from .index.watcher import IndexWatcher
from .llm import Ollama
from .prompt.builder import PromptBuilder, PromptResult


def project_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:10]


class Job:
    def __init__(self):
        self.state = "idle"     # idle | running | done | error
        self.stage = ""
        self.done = 0
        self.total = 0
        self.message = ""
        self.started = 0.0
        self.stats: Optional[dict] = None

    def as_dict(self) -> dict:
        return dict(state=self.state, stage=self.stage, done=self.done, total=self.total,
                    message=self.message, started=self.started, stats=self.stats)


class ProjectManager:
    def __init__(self, cfg: Optional[Config] = None, watch: bool = True):
        self.cfg = cfg or Config.load()
        self.llm = Ollama(self.cfg)
        self.watch = watch
        self.registry_path = HOME_DIR / "projects.json"
        self._writers: Dict[str, ProjectIndex] = {}   # used by indexing + watcher
        self._readers: Dict[str, ProjectIndex] = {}   # separate SQLite connection for prompts
        self._watchers: Dict[str, IndexWatcher] = {}
        self.jobs: Dict[str, Job] = {}
        self._lock = threading.RLock()
        self.projects: Dict[str, dict] = {}
        self._load()

    # ---------------------------------------------------------------- registry
    def _load(self) -> None:
        try:
            for p in json.loads(self.registry_path.read_text()):
                if Path(p["path"]).is_dir():
                    self.projects[p["id"]] = p
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(list(self.projects.values()), indent=2))

    def add(self, path: str) -> dict:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Folder not found: {root}")
        pid = project_id(root)
        with self._lock:
            if pid not in self.projects:
                self.projects[pid] = {"id": pid, "path": str(root), "name": root.name, "added": time.time()}
                self._save()
        if not self.reader(pid).is_built:
            self.start_index(pid)
        else:
            self._ensure_watch(pid)
        return self.projects[pid]

    def remove(self, pid: str) -> None:
        with self._lock:
            w = self._watchers.pop(pid, None)
            if w:
                w.stop()
            self._writers.pop(pid, None)
            self._readers.pop(pid, None)
            self.projects.pop(pid, None)
            self._save()

    def writer(self, pid: str) -> ProjectIndex:
        with self._lock:
            if pid not in self._writers:
                self._writers[pid] = ProjectIndex(Path(self.projects[pid]["path"]), self.cfg, self.llm)
            return self._writers[pid]

    def reader(self, pid: str) -> ProjectIndex:
        with self._lock:
            if pid not in self.projects:
                raise KeyError(pid)
            if pid not in self._readers:
                self._readers[pid] = ProjectIndex(Path(self.projects[pid]["path"]), self.cfg, self.llm)
            return self._readers[pid]

    # ---------------------------------------------------------------- indexing
    def start_index(self, pid: str, summarize: Optional[bool] = None) -> Job:
        job = self.jobs.setdefault(pid, Job())
        if job.state == "running":
            return job
        job.__init__()
        job.state, job.started = "running", time.time()

        def progress(stage, done, total, msg):
            job.stage, job.done, job.total, job.message = stage, done, total, msg

        def run():
            try:
                job.stats = self.writer(pid).update(progress=progress, summarize=summarize)
                job.state = "done"
                self._ensure_watch(pid)
            except Exception as e:
                job.state, job.message = "error", str(e)

        threading.Thread(target=run, daemon=True, name=f"index-{pid}").start()
        return job

    def _ensure_watch(self, pid: str) -> None:
        if not self.watch or pid in self._watchers:
            return
        try:
            self._watchers[pid] = IndexWatcher(self.writer(pid)).start()
        except Exception:
            pass  # watching is a convenience; the index still refreshes before each prompt

    def status(self, pid: str) -> dict:
        idx = self.reader(pid)
        s = idx.status()
        s.update(self.projects[pid])
        s["job"] = self.jobs.get(pid, Job()).as_dict()
        s["watching"] = pid in self._watchers
        return s

    def list(self) -> List[dict]:
        out = []
        for pid in list(self.projects):
            try:
                st = self.reader(pid).status()
                out.append({**self.projects[pid], "files": st["files"], "indexed_at": st["indexed_at"],
                            "job": self.jobs.get(pid, Job()).as_dict()})
            except Exception as e:
                out.append({**self.projects[pid], "error": str(e)})
        return sorted(out, key=lambda p: p["name"].lower())

    # ---------------------------------------------------------------- prompts
    def build_prompt(self, pid: str, **kw) -> PromptResult:
        idx = self.reader(pid)
        if not idx.is_built:
            raise RuntimeError("This project is still being indexed. Try again in a moment.")
        res = PromptBuilder(idx).build(**kw)
        self._append_history(idx, res)
        return res

    def _append_history(self, idx: ProjectIndex, res: PromptResult) -> None:
        path = idx.root / ".ctx" / "history.jsonl"
        rec = {"time": time.time(), "question": res.question, "rewritten": res.rewritten, "tokens": res.tokens,
               "files": [f.path for f in res.files if f.selected]}
        try:
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def history(self, pid: str, limit: int = 30) -> List[dict]:
        path = self.reader(pid).root / ".ctx" / "history.jsonl"
        try:
            lines = path.read_text().splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for l in reversed(lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out

    def shutdown(self) -> None:
        for w in self._watchers.values():
            w.stop()
        for idx in list(self._writers.values()) + list(self._readers.values()):
            idx.stop_flag.set()
