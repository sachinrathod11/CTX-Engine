"""Builds the file graph: import edges (file -> file it imports) and HTTP edges
(frontend file that calls /api/x -> backend file that defines /api/x)."""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .store import Store

JS_EXTS = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".json"]
JS_INDEX = ["/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.vue"]


class Resolver:
    def __init__(self, paths: List[str], root: Optional[Path] = None):
        self.paths = set(paths)
        self.by_suffix: Dict[str, List[str]] = defaultdict(list)
        for p in paths:
            parts = p.split("/")
            for i in range(len(parts)):
                self.by_suffix["/".join(parts[i:])].append(p)
        self.go_module = ""
        if root and (root / "go.mod").exists():
            m = re.search(r"^module\s+(\S+)", (root / "go.mod").read_text(errors="ignore"), re.M)
            self.go_module = m.group(1) if m else ""
        self.go_dirs: Dict[str, List[str]] = defaultdict(list)
        for p in paths:
            if p.endswith(".go"):
                self.go_dirs[posixpath.dirname(p)].append(p)

    def _suffix(self, suffix: str, near: str) -> Optional[str]:
        hits = self.by_suffix.get(suffix)
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        # prefer the candidate sharing the longest directory prefix with the importer
        def shared(p):
            a, b = p.split("/"), near.split("/")
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            return n
        return max(hits, key=shared)

    def resolve(self, src: str, spec: str, level: int, lang: str) -> List[str]:
        d = posixpath.dirname(src)
        if lang == "python":
            mod_path = spec.replace(".", "/")
            if level:
                base = d
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                cands = [posixpath.normpath(posixpath.join(base, mod_path)) if mod_path else base]
                for c in cands:
                    for t in (c + ".py", c + "/__init__.py"):
                        if t in self.paths:
                            return [t]
                return []
            for t in (mod_path + ".py", mod_path + "/__init__.py"):
                hit = self._suffix(t, src)
                if hit:
                    return [hit]
            return []
        if lang in ("javascript", "typescript", "vue", "svelte", "html"):
            if spec.startswith((".", "/")):
                base = posixpath.normpath(posixpath.join(d, spec)) if spec.startswith(".") else spec.lstrip("/")
            elif spec.startswith(("@/", "~/")):
                base = "src/" + spec[2:]
            else:
                return []  # npm package
            for ext in JS_EXTS + JS_INDEX:
                t = base + ext
                if t in self.paths:
                    return [t]
                if spec.startswith(("@/", "~/")):
                    hit = self._suffix(spec[2:] + ext, src)
                    if hit:
                        return [hit]
            return []
        if lang in ("java", "kotlin", "scala"):
            rel = spec.replace(".", "/")
            for ext in (".java", ".kt", ".scala"):
                hit = self._suffix(rel + ext, src)
                if hit:
                    return [hit]
            return []
        if lang == "go":
            if self.go_module and spec.startswith(self.go_module):
                sub = spec[len(self.go_module):].strip("/")
                return self.go_dirs.get(sub, [])[:6]
            return []
        if lang in ("c", "cpp"):
            t = posixpath.normpath(posixpath.join(d, spec))
            if t in self.paths:
                return [t]
            hit = self._suffix(spec, src)
            return [hit] if hit else []
        if lang == "rust":
            if "::" not in spec:  # mod x;
                for t in (posixpath.join(d, spec + ".rs"), posixpath.join(d, spec, "mod.rs")):
                    if t in self.paths:
                        return [t]
                return []
            parts = spec.split("::")
            if parts[0] in ("crate", "super", "self"):
                parts = parts[1:]
            for n in range(len(parts), 0, -1):
                hit = self._suffix("/".join(parts[:n]) + ".rs", src) or self._suffix("/".join(parts[:n]) + "/mod.rs", src)
                if hit:
                    return [hit]
            return []
        if lang in ("ruby", "php", "dart"):
            for t in (posixpath.normpath(posixpath.join(d, spec)), posixpath.normpath(posixpath.join(d, spec)) + ".rb"):
                if t in self.paths:
                    return [t]
            hit = self._suffix(spec.lstrip("./"), src) or self._suffix(spec.lstrip("./") + ".rb", src)
            return [hit] if hit else []
        return []


def _route_match(a: str, b: str) -> bool:
    pa, pb = a.strip("/").split("/"), b.strip("/").split("/")
    if len(pa) != len(pb):
        return False
    return all(x == y or x == "*" or y == "*" for x, y in zip(pa, pb))


def rebuild_edges(store: Store, root: Optional[Path] = None) -> int:
    files = store.all_files()
    by_id = {f["id"]: f for f in files}
    by_path = {f["path"]: f["id"] for f in files}
    res = Resolver(list(by_path), root)
    edges: Set[Tuple[int, int, str, str]] = set()
    for imp in store.all_imports():
        src = by_id.get(imp["file_id"])
        if src is None:
            continue
        for target in res.resolve(src["path"], imp["spec"], imp["level"], src["lang"]):
            tid = by_path.get(target)
            if tid and tid != src["id"]:
                edges.add((src["id"], tid, "import", imp["spec"]))
    routes = store.all_routes()
    defines = [r for r in routes if r["role"] == "defines"]
    def method_ok(c, d):
        return c["method"] == d["method"] or "ANY" in (c["method"], d["method"])

    for call in (r for r in routes if r["role"] == "calls"):
        exact = [d for d in defines if d["file_id"] != call["file_id"]
                 and _route_match(call["path"], d["path"]) and method_ok(call, d)]
        if not exact:
            # router mounted under a prefix elsewhere (app.use('/api', router)): match on the tail
            exact = [d for d in defines if d["file_id"] != call["file_id"] and d["path"].count("/") >= 1
                     and d["path"] != "/" and call["path"].endswith(d["path"]) and method_ok(call, d)]
            if len(exact) > 2:
                exact = []
        for d in exact:
            edges.add((call["file_id"], d["file_id"], "http", f"{call['method']} {d['path']}"))
    store.replace_edges(sorted(edges))
    return len(edges)
