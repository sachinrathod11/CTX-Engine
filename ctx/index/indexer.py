"""ProjectIndex: builds and incrementally updates one project's index."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from ..config import INDEX_DIRNAME, Config
from ..llm import LLMUnavailable, Ollama
from . import graph
from .chunker import build_chunks
from .embedder import Embedder
from .overview import build_overview, overview_text
from .parser import parse
from .scanner import Scanner
from .store import Store
from .summarizer import SUMMARIZABLE, heuristic_summary, llm_summary

Progress = Callable[[str, int, int, str], None]


def _noop(stage: str, done: int, total: int, msg: str) -> None:
    pass


class ProjectIndex:
    def __init__(self, root: Path, cfg: Optional[Config] = None, llm: Optional[Ollama] = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Not a folder: {self.root}")
        self.cfg = cfg or Config.load()
        self.llm = llm or Ollama(self.cfg)
        ctx_dir = self.root / INDEX_DIRNAME
        ctx_dir.mkdir(exist_ok=True)
        gi = ctx_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
        self.store = Store(ctx_dir / "index.db")
        self.scanner = Scanner(self.root, self.cfg.max_file_bytes, self.cfg.extra_ignore)
        self.lock = threading.RLock()
        self.stop_flag = threading.Event()

    # ------------------------------------------------------------ public
    @property
    def is_built(self) -> bool:
        return self.store.get_meta("indexed_at") is not None

    def update(self, progress: Progress = _noop, summarize: Optional[bool] = None,
               paths: Optional[Iterable[Path]] = None) -> Dict:
        """Full incremental refresh (paths=None) or refresh of specific changed paths (watcher)."""
        with self.lock:
            t0 = time.time()
            self.stop_flag.clear()
            changed, removed = self._sync_files(progress, paths)
            if changed or removed or not self.is_built:
                progress("graph", 0, 1, "Linking imports and API calls")
                n_edges = graph.rebuild_edges(self.store, self.root)
            else:
                n_edges = None
            do_sum = self.cfg.summarize if summarize is None else summarize
            n_llm = self._llm_summaries(progress) if do_sum else 0
            n_vec = self._embed(progress)
            ov = build_overview(self.store, self.root)
            self.store.set_meta("overview", ov)
            if do_sum and (changed or not self.store.get_meta("project_description")):
                self._describe_project(ov)
            self.store.set_meta("indexed_at", time.time())
            stats = {"changed": len(changed), "removed": len(removed), "edges": n_edges,
                     "llm_summaries": n_llm, "embedded_chunks": n_vec,
                     "seconds": round(time.time() - t0, 2), "files": ov["files"]}
            self.store.set_meta("last_stats", stats)
            progress("done", 1, 1, "Index up to date")
            return stats

    def overview(self) -> str:
        ov = self.store.get_meta("overview") or build_overview(self.store, self.root)
        return overview_text(ov, self.store.get_meta("project_description", ""))

    def status(self) -> Dict:
        db = self.store.db
        n_files = db.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        n_llm = db.execute("SELECT COUNT(*) n FROM files WHERE summary_src='llm'").fetchone()["n"]
        n_chunks = db.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        n_vec = db.execute("SELECT COUNT(*) n FROM chunks WHERE vec IS NOT NULL").fetchone()["n"]
        n_http = db.execute("SELECT COUNT(*) n FROM edges WHERE kind='http'").fetchone()["n"]
        n_imp = db.execute("SELECT COUNT(*) n FROM edges WHERE kind='import'").fetchone()["n"]
        return {"root": str(self.root), "files": n_files, "chunks": n_chunks, "embedded_chunks": n_vec,
                "llm_summaries": n_llm, "import_edges": n_imp, "http_edges": n_http,
                "embed_model": self.store.get_meta("embed_model"), "indexed_at": self.store.get_meta("indexed_at"),
                "last_stats": self.store.get_meta("last_stats")}

    # ------------------------------------------------------------ steps
    def _sync_files(self, progress: Progress, paths: Optional[Iterable[Path]]):
        known = self.store.file_hashes()
        if paths is None:
            progress("scan", 0, 1, "Scanning folder")
            found = {sf.rel: sf for sf in self.scanner.scan()}
            removed = [p for p in known if p not in found]
            candidates = list(found.values())
        else:
            candidates, removed = [], []
            for p in paths:
                p = Path(p)
                try:
                    rel = p.resolve().relative_to(self.root).as_posix()
                except ValueError:
                    continue
                sf = self.scanner.accept(p) if p.exists() else None
                if sf:
                    candidates.append(sf)
                elif rel in known:
                    removed.append(rel)
        for rel in removed:
            self.store.remove_file(rel)
        changed: List[str] = []
        total = len(candidates)
        for i, sf in enumerate(candidates):
            if self.stop_flag.is_set():
                break
            try:
                raw = sf.abs.read_bytes()
            except OSError:
                continue
            h = hashlib.sha1(raw).hexdigest()
            if known.get(sf.rel) == h:
                continue
            text = raw.decode("utf-8", errors="replace")
            self._index_file(sf, text, h)
            changed.append(sf.rel)
            if i % 25 == 0:
                progress("parse", i, total, sf.rel)
        self.store.commit()
        progress("parse", total, total, f"{len(changed)} changed, {len(removed)} removed")
        return changed, removed

    def _index_file(self, sf, text: str, h: str) -> None:
        pf = parse(text, sf.lang)
        n_lines = text.count("\n") + 1
        symbols = [{"name": s.name, "kind": s.kind, "start": s.start, "end": min(s.end, n_lines),
                    "signature": s.signature, "doc": s.doc} for s in pf.symbols]
        routes = [{"method": r.method, "path": r.path, "line": r.line, "role": r.role} for r in pf.routes]
        chunks = build_chunks(sf.rel, text, pf.symbols)
        rec = {"path": sf.rel, "lang": sf.lang, "size": sf.size, "mtime": sf.mtime, "hash": h,
               "n_lines": n_lines, "doc": pf.doc}
        fid = self.store.put_file(rec, symbols, chunks, pf.imports, routes)
        imports = [s.split("/")[-1].split(".")[-1] if not s.startswith(".") else s.lstrip("./") for s, _ in pf.imports]
        self.store.set_summary(fid, heuristic_summary(sf.rel, sf.lang, pf.doc, symbols, routes, imports), "heuristic")

    def _llm_summaries(self, progress: Progress) -> int:
        if not self.llm.can_generate:
            return 0
        todo = [f for f in self.store.all_files() if f["summary_src"] != "llm" and f["lang"] in SUMMARIZABLE]
        done = 0
        for i, f in enumerate(todo):
            if self.stop_flag.is_set():
                break
            progress("summarize", i, len(todo), f["path"])
            try:
                text = (self.root / f["path"]).read_text(errors="replace")
                s = llm_summary(self.llm, f["path"], text, f["summary"] or "")
            except (OSError, LLMUnavailable):
                continue
            self.store.set_summary(f["id"], s, "llm")
            done += 1
            if done % 10 == 0:
                self.store.commit()
        self.store.commit()
        return done

    def _embed(self, progress: Progress) -> int:
        emb = Embedder.best_available(self.llm)
        if self.store.get_meta("embed_model") != emb.model:
            self.store.clear_vecs()
            self.store.set_meta("embed_model", emb.model)
        todo = self.store.chunks_missing_vec()
        done = 0
        batch = 32
        for i in range(0, len(todo), batch):
            if self.stop_flag.is_set():
                break
            part = todo[i:i + batch]
            progress("embed", i, len(todo), f"{emb.model}")
            texts = [f"{r['path']} :: {r['name']}\n{r['text']}" for r in part]
            try:
                vecs = emb.embed_docs(texts)
            except LLMUnavailable:
                # Ollama went away mid-run: switch the whole index to the offline embedder
                self.store.clear_vecs()
                self.store.set_meta("embed_model", "hash-512")
                emb = Embedder(self.llm, "hash-512")
                return self._embed(progress)
            self.store.set_vecs([(r["id"], v) for r, v in zip(part, vecs)])
            done += len(part)
        return done

    def _describe_project(self, ov: Dict) -> None:
        if not self.llm.can_generate:
            return
        prompt = (overview_text(ov) + "\n\nIn ONE sentence (max 35 words), say what this project is and what it does. "
                  "Plain text, no preamble.")
        try:
            desc = " ".join(self.llm.generate(prompt, max_tokens=80).split())
            if len(desc) > 15:
                self.store.set_meta("project_description", desc[:300])
        except LLMUnavailable:
            pass
