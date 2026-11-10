"""Chooses which lines of each selected file go into the prompt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..index.indexer import ProjectIndex
from ..textutil import code_terms

SMALL_FILE_LINES = 90


@dataclass
class Snippet:
    start: int
    end: int
    name: str
    score: float
    text: str = ""


def _merge(ranges: List[Snippet], gap: int = 3) -> List[Snippet]:
    ranges = sorted(ranges, key=lambda r: r.start)
    out: List[Snippet] = []
    for r in ranges:
        if out and r.start <= out[-1].end + gap:
            out[-1].end = max(out[-1].end, r.end)
            out[-1].score = max(out[-1].score, r.score)
            if r.name not in out[-1].name:
                out[-1].name += ", " + r.name
        else:
            out.append(Snippet(r.start, r.end, r.name, r.score))
    return out


def file_snippets(index: ProjectIndex, file_id: int, path: str, chunk_scores: Dict[int, float],
                  query_words: set) -> Tuple[List[str], List[Snippet]]:
    """Return (file lines, ranked snippets). Small files come back as one whole-file snippet."""
    try:
        lines = (index.root / path).read_text(errors="replace").splitlines()
    except OSError:
        return [], []
    n = len(lines)
    if n <= SMALL_FILE_LINES:
        return lines, [Snippet(1, n, "whole file", 1.0)]
    chunks = index.store.chunks_of(file_id)
    scored: List[Snippet] = []
    for c in chunks:
        s = chunk_scores.get(c["id"], 0.0)
        words = set(code_terms(c["text"]))
        s += 0.002 * len(words & query_words)
        if c["kind"] == "module" and c["start"] == 1:
            s += 0.001  # imports at the top help the chatbot see dependencies
        if s > 0:
            scored.append(Snippet(c["start"], c["end"], c["name"], s))
    if not scored:
        scored = [Snippet(1, min(n, 60), "top of file", 0.001)]
    scored.sort(key=lambda x: -x.score)
    return lines, scored


def render(lines: List[str], sn: Snippet, max_lines: Optional[int] = None) -> str:
    end = sn.end if max_lines is None else min(sn.end, sn.start + max_lines - 1)
    body = "\n".join(lines[sn.start - 1:end])
    if end < sn.end:
        body += f"\n... ({sn.end - end} more lines)"
    return body


FENCE_LANG = {"javascript": "jsx", "typescript": "tsx", "python": "python", "java": "java", "go": "go",
              "rust": "rust", "ruby": "ruby", "php": "php", "csharp": "csharp", "c": "c", "cpp": "cpp",
              "kotlin": "kotlin", "swift": "swift", "html": "html", "css": "css", "sql": "sql",
              "shell": "bash", "json": "json", "yaml": "yaml", "vue": "vue", "svelte": "svelte",
              "markdown": "markdown", "dart": "dart"}


def fence_lang(path: str, lang: str) -> str:
    ext = Path(path).suffix.lstrip(".")
    if lang in ("javascript", "typescript") and ext in ("js", "ts", "mjs", "cjs"):
        return ext if ext in ("js", "ts") else "js"
    return FENCE_LANG.get(lang, "")
