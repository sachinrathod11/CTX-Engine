"""Hybrid retrieval: BM25 keyword search + embedding search + path matching, merged with
normalised score fusion (CombSUM plus a reciprocal-rank term), then expanded along the file graph."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from ..index.embedder import HASH_MODEL, Embedder
from ..index.indexer import ProjectIndex
from ..llm import LLMUnavailable
from ..textutil import split_identifier
from .expand import Expansion

RRF_K = 60


@dataclass
class Candidate:
    file_id: int
    path: str
    score: float
    reasons: List[str] = field(default_factory=list)
    via_graph: bool = False


@dataclass
class SearchResult:
    files: List[Candidate]
    chunk_scores: Dict[int, float]
    signals: Dict[str, int]


def _fts_expr(terms: List[str], prefix: bool = False) -> str:
    clean = []
    for t in terms:
        t = re.sub(r"[^\w]", " ", t).strip()
        if not t:
            continue
        clean.append('"' + t.replace('"', "") + '"')
        if prefix and len(t) >= 7 and " " not in t:
            clean.append('"' + t[:6] + '"*')  # configured -> config*, payments -> paymen*
    return " OR ".join(clean)


def _file_scores(pairs: List[tuple], chunk_file: Dict[int, int]) -> Dict[int, float]:
    """(chunk_id, raw score >= 0) -> best normalised score per file in [0, 1]."""
    best: Dict[int, float] = {}
    for cid, sc in pairs:
        fid = chunk_file.get(cid)
        if fid is not None and sc > best.get(fid, 0.0):
            best[fid] = sc
    top = max(best.values()) if best else 0.0
    return {f: v / top for f, v in best.items()} if top > 0 else {}


def search(index: ProjectIndex, question: str, exp: Expansion, limit: int = 15,
           use_vector: bool = True) -> SearchResult:
    """Each signal gives every file a normalised score; scores are combined as a weighted sum
    (CombSUM) plus a small reciprocal-rank term, so a strong keyword match on a rare word
    outweighs many weak matches."""
    store = index.store
    db = store.db
    chunk_file = {r["id"]: r["file_id"] for r in db.execute("SELECT id, file_id FROM chunks")}
    files = {f["id"]: f for f in store.all_files()}
    lists: List[tuple] = []          # (weight, {file_id: norm score}, name)
    chunk_fused: Dict[int, float] = defaultdict(float)
    signals = {}

    def add_chunk_list(weight: float, pairs: List[tuple], name: str) -> None:
        if not pairs:
            return
        top = max(sc for _, sc in pairs) or 1.0
        for cid, sc in pairs:
            chunk_fused[cid] += weight * sc / top
        lists.append((weight, _file_scores(pairs, chunk_file), name))
        signals[name] = len(pairs)

    # 1) BM25 over chunks (sqlite bm25() is negative: smaller = better)
    for weight, terms, name, pre in ((1.0, exp.terms, "bm25", True), (0.5, exp.extra, "bm25_expanded", False)):
        if terms:
            rows = store.fts_search(_fts_expr(terms, prefix=pre), limit=300)
            add_chunk_list(weight, [(r["chunk_id"], -r["score"]) for r in rows], name)

    # exact phrases (error messages, quoted strings) are strong evidence
    for ph in exp.phrases:
        rows = store.fts_search('"' + re.sub(r"[^\w\s]", " ", ph).strip() + '"', limit=50)
        add_chunk_list(1.5, [(r["chunk_id"], -r["score"]) for r in rows], "phrase")

    # 2) embedding search over chunks
    model = store.get_meta("embed_model") if use_vector else None
    if model:
        emb = Embedder(index.llm, model)
        weight = 0.4 if model == HASH_MODEL else 1.0  # offline hash vectors are a weak signal
        try:
            q = emb.embed_query(question + "\n" + " ".join(exp.terms + exp.extra[:10]))
            ids, fids, mat = store.vec_matrix()
            if len(ids):
                qn = q / (np.linalg.norm(q) or 1)
                sims = mat @ qn
                order = np.argsort(-sims)[:200]
                floor = float(np.median(sims)) if len(sims) > 4 else 0.0
                pairs = [(int(ids[i]), float(sims[i]) - floor) for i in order if sims[i] - floor > 0]
                add_chunk_list(weight, pairs, "vector")
        except LLMUnavailable:
            signals["vector"] = -1  # embedding model offline; keyword search still runs

    # 3) path / file-name match (LoginForm.jsx for "login form", config.py for "configured")
    main, extra = set(exp.terms), set(exp.extra[:8])

    def word_hit(words: set, terms: set) -> int:
        return sum(1 for t in terms if any(w == t or (len(w) >= 4 and len(t) >= 4 and (t.startswith(w) or w.startswith(t)))
                                           for w in words))
    path_scores: Dict[int, float] = {}
    for fid, f in files.items():
        words = set(w for part in f["path"].replace(".", "/").split("/") for w in split_identifier(part)) - {"src", "py", "js", "jsx", "ts", "tsx"}
        hit = 2 * word_hit(words, main) + word_hit(words, extra)
        if any(f["path"].endswith(m) for m in exp.mentioned_files):
            hit += 10
        if hit:
            path_scores[fid] = hit
    if path_scores:
        top = max(path_scores.values())
        lists.append((0.7, {f: v / top for f, v in path_scores.items()}, "path"))
        signals["path"] = len(path_scores)

    # fuse
    fused: Dict[int, float] = defaultdict(float)
    why: Dict[int, List[str]] = defaultdict(list)
    for weight, scores, name in lists:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        for rank, (fid, sc) in enumerate(ranked[:150]):
            fused[fid] += weight * (0.75 * sc + 0.25 * RRF_K / (RRF_K + rank))
            if rank < 10 and sc > 0.2:
                why[fid].append(name)

    # light priors: docs/tests/examples rarely hold the logic being asked about
    qtext = question.lower()
    has_core = any(not re.search(r"(^|/)(examples?|samples?|docs?|tests?)/", f["path"]) and f["lang"] not in ("markdown", "text")
                   for f in files.values())
    for fid in list(fused):
        f = files[fid]
        if f["lang"] in ("markdown", "text", "json", "yaml", "toml", "ini", "xml") and not any(
                k in qtext for k in ("readme", "config", "setting", "doc", "json", "yaml", "env", "depend", "version", "install", "package")):
            fused[fid] *= 0.5
        if re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|test_|\.test\.|\.spec\.", f["path"]) and "test" not in qtext:
            fused[fid] *= 0.6
        if has_core and re.search(r"(^|/)(examples?|samples?|docs?|benchmarks?)/", f["path"]) and not any(
                k in qtext for k in ("example", "sample", "doc", "tutorial", "benchmark")):
            fused[fid] *= 0.75

    ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
    cands = [Candidate(fid, files[fid]["path"], s, sorted(set(why[fid]))) for fid, s in ranked]
    return SearchResult(cands, dict(chunk_fused), signals)


def expand_graph(index: ProjectIndex, res: SearchResult, limit: int = 15, seeds: int = 6) -> List[Candidate]:
    """Add files directly connected to the top hits (imports both ways, frontend<->backend API links).

    Boosts are damped for hub files that many files import (`__init__.py`, `typing.py`, a shared
    `utils`), and each file is boosted once by its strongest neighbour, not once per neighbour."""
    store = index.store
    indeg = store.in_degree()
    have = {c.file_id: c for c in res.files}
    boost: Dict[int, float] = {}
    reasons: Dict[int, List[str]] = defaultdict(list)
    for c in res.files[:seeds]:
        edges = [(e, True) for e in store.edges_from(c.file_id)] + [(e, False) for e in store.edges_to(c.file_id)]
        for e, outgoing in edges:
            fid = e["dst"] if outgoing else e["src"]
            if fid == c.file_id:
                continue
            if e["kind"] == "http":
                w = 0.75
                why = (f"handles {e['detail']} called from {c.path}" if outgoing
                       else f"calls {e['detail']} handled in {c.path}")
            else:
                w = (0.5 if outgoing else 0.35) / math.log2(2 + indeg.get(fid, 0))
                why = f"imported by {c.path}" if outgoing else f"imports {c.path}"
            b = c.score * w
            if b > boost.get(fid, 0.0):
                boost[fid] = b
            if why not in reasons[fid]:
                reasons[fid].append(why)
    extra: List[Candidate] = []
    for fid, b in boost.items():
        if fid in have:
            have[fid].score += 0.3 * b
            have[fid].reasons += [r for r in reasons[fid] if r not in have[fid].reasons][:2]
            continue
        f = store.file_by_id(fid)
        if f is None:
            continue
        name = f["path"].rsplit("/", 1)[-1]
        if name in HUB_NAMES or name.endswith(".d.ts"):
            b *= 0.5  # re-export / type-only modules rarely hold the logic
        extra.append(Candidate(fid, f["path"], b, reasons[fid][:4], via_graph=True))
    merged = sorted(list(res.files) + extra, key=lambda x: -x.score)
    return merged[:limit]


HUB_NAMES = {"__init__.py", "typing.py", "types.py", "index.js", "index.ts", "index.tsx", "types.ts", "constants.py",
             "constants.js", "constants.ts", "globals.py", "utils.py", "utils.js", "utils.ts"}
