"""SQLite storage for one project's index (lives in <project>/.ctx/index.db)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT, size INTEGER, mtime REAL,
    hash TEXT, n_lines INTEGER, doc TEXT, summary TEXT, summary_src TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT, kind TEXT, start INTEGER, "end" INTEGER,
    signature TEXT, doc TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT, kind TEXT, start INTEGER, "end" INTEGER,
    text TEXT, vec BLOB
);
CREATE TABLE IF NOT EXISTS imports (file_id INTEGER, spec TEXT, level INTEGER);
CREATE TABLE IF NOT EXISTS routes (file_id INTEGER, method TEXT, path TEXT, line INTEGER, role TEXT);
CREATE TABLE IF NOT EXISTS edges (src INTEGER, dst INTEGER, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_sym_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS ix_chunk_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS ix_imp_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS ix_route_file ON routes(file_id);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edges(dst);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(path, name, body, tokenize='porter unicode61');
"""


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._vec_cache: Optional[tuple] = None

    # -------- meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
            self.db.commit()

    # -------- files
    def file_hashes(self) -> Dict[str, str]:
        return {r["path"]: r["hash"] for r in self.db.execute("SELECT path, hash FROM files")}

    def file_by_path(self, path: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def file_by_id(self, fid: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()

    def all_files(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files ORDER BY path").fetchall()

    def remove_file(self, path: str) -> None:
        with self._lock:
            row = self.file_by_path(path)
            if not row:
                return
            fid = row["id"]
            ids = [r["id"] for r in self.db.execute("SELECT id FROM chunks WHERE file_id=?", (fid,))]
            self.db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
            for t in ("symbols", "chunks", "imports", "routes"):
                self.db.execute(f"DELETE FROM {t} WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM edges WHERE src=? OR dst=?", (fid, fid))
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))
            self._vec_cache = None

    def put_file(self, rec: dict, symbols: Iterable[dict], chunks: Iterable[dict],
                 imports: Iterable[tuple], routes: Iterable[dict]) -> int:
        """Replace everything stored for one file. Caller commits."""
        with self._lock:
            old = self.file_by_path(rec["path"])
            summary, src = None, None
            if old is not None:
                self.remove_file(rec["path"])
            cur = self.db.execute(
                "INSERT INTO files(path, lang, size, mtime, hash, n_lines, doc, summary, summary_src) VALUES(?,?,?,?,?,?,?,?,?)",
                (rec["path"], rec["lang"], rec["size"], rec["mtime"], rec["hash"], rec["n_lines"], rec["doc"], summary, src))
            fid = cur.lastrowid
            self.db.executemany(
                'INSERT INTO symbols(file_id, name, kind, start, "end", signature, doc) VALUES(?,?,?,?,?,?,?)',
                [(fid, s["name"], s["kind"], s["start"], s["end"], s["signature"], s["doc"]) for s in symbols])
            for c in chunks:
                cid = self.db.execute(
                    'INSERT INTO chunks(file_id, name, kind, start, "end", text) VALUES(?,?,?,?,?,?)',
                    (fid, c["name"], c["kind"], c["start"], c["end"], c["text"])).lastrowid
                self.db.execute("INSERT INTO chunks_fts(rowid, path, name, body) VALUES(?,?,?,?)",
                                (cid, c["fts_path"], c["fts_name"], c["fts_body"]))
            self.db.executemany("INSERT INTO imports(file_id, spec, level) VALUES(?,?,?)",
                                [(fid, s, lv) for s, lv in imports])
            self.db.executemany("INSERT INTO routes(file_id, method, path, line, role) VALUES(?,?,?,?,?)",
                                [(fid, r["method"], r["path"], r["line"], r["role"]) for r in routes])
            self._vec_cache = None
            return fid

    def set_summary(self, fid: int, summary: str, src: str) -> None:
        with self._lock:
            self.db.execute("UPDATE files SET summary=?, summary_src=? WHERE id=?", (summary, src, fid))

    def commit(self) -> None:
        with self._lock:
            self.db.commit()

    # -------- symbols / chunks
    def symbols_of(self, fid: int) -> List[sqlite3.Row]:
        return self.db.execute('SELECT * FROM symbols WHERE file_id=? ORDER BY start', (fid,)).fetchall()

    def chunks_of(self, fid: int) -> List[sqlite3.Row]:
        return self.db.execute('SELECT id, file_id, name, kind, start, "end", text FROM chunks WHERE file_id=? ORDER BY start', (fid,)).fetchall()

    def chunks_missing_vec(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT c.id, c.text, c.name, f.path FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.vec IS NULL").fetchall()

    def set_vecs(self, items: List[tuple]) -> None:
        with self._lock:
            self.db.executemany("UPDATE chunks SET vec=? WHERE id=?",
                                [(np.asarray(v, dtype=np.float32).tobytes(), cid) for cid, v in items])
            self.db.commit()
            self._vec_cache = None

    def clear_vecs(self) -> None:
        with self._lock:
            self.db.execute("UPDATE chunks SET vec=NULL")
            self.db.commit()
            self._vec_cache = None

    def vec_matrix(self):
        """(chunk_ids, file_ids, matrix) for all embedded chunks, cached until the index changes
        (also when another connection - e.g. the background indexer - changed it)."""
        sig = tuple(self.db.execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(id), 0) FROM chunks WHERE vec IS NOT NULL").fetchone())
        if self._vec_cache is not None and getattr(self, "_vec_sig", None) != sig:
            self._vec_cache = None
        self._vec_sig = sig
        if self._vec_cache is None:
            rows = self.db.execute("SELECT id, file_id, vec FROM chunks WHERE vec IS NOT NULL").fetchall()
            if not rows:
                self._vec_cache = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32))
            else:
                mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0] = 1
                self._vec_cache = (np.array([r["id"] for r in rows]), np.array([r["file_id"] for r in rows]), mat / norms)
        return self._vec_cache

    def fts_search(self, match: str, limit: int = 200) -> List[sqlite3.Row]:
        try:
            return self.db.execute(
                "SELECT rowid AS chunk_id, bm25(chunks_fts, 4.0, 3.0, 1.0) AS score FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []

    def chunk(self, cid: int) -> Optional[sqlite3.Row]:
        return self.db.execute('SELECT id, file_id, name, kind, start, "end", text FROM chunks WHERE id=?', (cid,)).fetchone()

    # -------- graph
    def all_imports(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT file_id, spec, level FROM imports").fetchall()

    def all_routes(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT r.*, f.path AS file FROM routes r JOIN files f ON f.id=r.file_id").fetchall()

    def replace_edges(self, edges: List[tuple]) -> None:
        with self._lock:
            self.db.execute("DELETE FROM edges")
            self.db.executemany("INSERT INTO edges(src, dst, kind, detail) VALUES(?,?,?,?)", edges)
            self.db.commit()

    def edges_from(self, fid: int) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM edges WHERE src=?", (fid,)).fetchall()

    def edges_to(self, fid: int) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM edges WHERE dst=?", (fid,)).fetchall()

    def in_degree(self) -> Dict[int, int]:
        return {r["dst"]: r["n"] for r in self.db.execute(
            "SELECT dst, COUNT(DISTINCT src) AS n FROM edges GROUP BY dst")}

    def close(self) -> None:
        self.db.close()
