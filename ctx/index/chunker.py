"""Splits a file into search chunks that follow code structure (one function or small
class per chunk) instead of fixed-size windows."""
from __future__ import annotations

from typing import List

from ..textutil import code_terms, split_identifier
from .parser import Symbol

MAX_CHUNK_LINES = 80
WINDOW = 60


def _windows(start: int, end: int, name: str, kind: str) -> List[dict]:
    out = []
    s = start
    while s <= end:
        e = min(end, s + WINDOW - 1)
        out.append({"name": name if s == start else f"{name} (cont.)", "kind": kind, "start": s, "end": e})
        s = e + 1
    return out


def plan_chunks(symbols: List[Symbol], n_lines: int) -> List[dict]:
    """Choose non-overlapping line ranges: small symbols whole, big ones split by children/windows,
    leftover module-level code grouped into windows."""
    syms = sorted(symbols, key=lambda s: (s.start, -(s.end - s.start)))
    chosen: List[dict] = []

    def children(parent: Symbol) -> List[Symbol]:
        inner = [s for s in syms if s is not parent and parent.start <= s.start and s.end <= parent.end
                 and (s.start, s.end) != (parent.start, parent.end)]
        return [s for s in inner if not any(o is not s and o.start <= s.start and s.end <= o.end
                                            and (o.start, o.end) != (s.start, s.end) for o in inner)]

    def take(sym: Symbol) -> None:
        length = sym.end - sym.start + 1
        if length <= MAX_CHUNK_LINES:
            chosen.append({"name": sym.name, "kind": sym.kind, "start": sym.start, "end": sym.end})
            return
        kids = children(sym)
        if not kids:
            chosen.extend(_windows(sym.start, sym.end, sym.name, sym.kind))
            return
        header_end = kids[0].start - 1
        if header_end >= sym.start:
            chosen.append({"name": sym.name, "kind": sym.kind, "start": sym.start, "end": min(header_end, sym.start + WINDOW - 1)})
        for k in kids:
            take(k)

    tops = [s for s in syms if not any(o is not s and o.start <= s.start and s.end <= o.end
                                       and (o.start, o.end) != (s.start, s.end) for o in syms)]
    seen = set()
    for s in tops:
        if (s.start, s.end) in seen:
            continue
        seen.add((s.start, s.end))
        take(s)

    # module-level leftovers (imports, config, top-level calls)
    covered = [False] * (n_lines + 2)
    for c in chosen:
        for i in range(c["start"], min(c["end"], n_lines) + 1):
            covered[i] = True
    i = 1
    while i <= n_lines:
        if covered[i]:
            i += 1
            continue
        j = i
        while j <= n_lines and not covered[j]:
            j += 1
        chosen.extend(_windows(i, j - 1, "(module)", "module"))
        i = j
    return sorted(chosen, key=lambda c: c["start"])


def build_chunks(path: str, text: str, symbols: List[Symbol]) -> List[dict]:
    lines = text.splitlines()
    n = len(lines)
    out = []
    path_words = " ".join(w for part in path.replace(".", "/").split("/") for w in split_identifier(part))
    for c in plan_chunks(symbols, n):
        body = "\n".join(lines[c["start"] - 1:c["end"]])
        if c["kind"] == "module" and len([l for l in body.splitlines() if l.strip()]) < 2:
            continue
        name_words = " ".join(split_identifier(c["name"].replace(".", "_")))
        c.update({
            "text": body,
            "fts_path": f"{path} {path_words}",
            "fts_name": f"{c['name']} {name_words}",
            "fts_body": body + "\n" + " ".join(code_terms(body)),
        })
        out.append(c)
    return out
