"""Extracts symbols (functions, classes, methods), imports and HTTP routes from source files.

Python is parsed with the standard-library `ast` module (exact). Other languages use
targeted regexes plus brace matching, which needs no native grammars and works offline.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "all", "route")


@dataclass
class Symbol:
    name: str
    kind: str           # function | method | class | component | type
    start: int          # 1-based, inclusive
    end: int
    signature: str = ""
    doc: str = ""
    parent: str = ""


@dataclass
class Route:
    method: str          # GET/POST/... or ANY
    path: str            # normalised, e.g. /api/login
    line: int
    role: str            # "defines" (server handler) or "calls" (client request)


@dataclass
class ParsedFile:
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[Tuple[str, int]] = field(default_factory=list)  # (raw spec, python relative level)
    routes: List[Route] = field(default_factory=list)
    doc: str = ""


# ---------------------------------------------------------------- helpers
def normalise_route(path: str) -> str:
    path = path.strip()
    path = re.sub(r"^[a-z]+://[^/]+", "", path)          # drop scheme + host
    path = path.split("?")[0].split("#")[0]
    path = re.sub(r"\$\{[^}]*\}", "", path)               # ${BASE_URL}/api/x -> /api/x
    path = re.sub(r"<[^>]+>|:\w+|\{[^}]+\}", "*", path)   # params -> *
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        return ""
    return path.rstrip("/") or "/"


def _block_end(lines: List[str], start_idx: int, max_scan: int = 2000) -> int:
    """Return 0-based index of the line closing the brace block that opens at/after start_idx."""
    depth, opened = 0, False
    in_str: Optional[str] = None
    in_block_comment = False
    for i in range(start_idx, min(len(lines), start_idx + max_scan)):
        line = lines[i]
        j = 0
        while j < len(line):
            ch = line[j]
            nxt = line[j + 1] if j + 1 < len(line) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    j += 1
            elif in_str:
                if ch == "\\":
                    j += 1
                elif ch == in_str:
                    in_str = None
            elif ch == "/" and nxt == "/":
                break
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                j += 1
            elif ch in "\"'`":
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return i
            j += 1
        if in_str in ("'", '"'):
            in_str = None  # unterminated simple string: reset at end of line
        # the body counts as opened only once a brace stays open past a line end;
        # this skips balanced pairs in signatures like `function F({ a, b }) {`
        if depth > 0:
            opened = True
        elif not opened and i - start_idx > 3:
            return start_idx  # no block found (declaration only / one-liner)
    return min(len(lines) - 1, start_idx + 40) if opened else start_idx


def _leading_comment(lines: List[str]) -> str:
    out = []
    for raw in lines[:30]:
        s = raw.strip()
        if not s and not out:
            continue
        if s.startswith(("#!", "'use strict'", '"use strict"', "package ")):
            continue
        if s.startswith(("//", "#", "*", "/*", "--", '"""', "'''")):
            s = s.lstrip("/#*-\"' ").rstrip("*/\"' ")
            if s:
                out.append(s)
            if len(out) >= 4:
                break
        else:
            break
    return " ".join(out)[:300]


# ---------------------------------------------------------------- python
def _parse_python(text: str) -> ParsedFile:
    pf = ParsedFile()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _parse_generic(text, "python")
    pf.doc = (ast.get_docstring(tree) or "").strip().split("\n\n")[0][:300]
    lines = text.splitlines()

    def sig(node) -> str:
        line = lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else ""
        return line[:160]

    def add_routes(node):
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            attr = dec.func.attr.lower()
            if attr not in HTTP_METHODS and attr not in ("api_route", "websocket"):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str):
                continue
            raw_path = dec.args[0].value
            path = normalise_route(raw_path) if raw_path else "/"
            if not path:
                continue
            methods = [attr.upper()] if attr in HTTP_METHODS[:-2] else []
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value.upper() for e in kw.value.elts
                               if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            for m in methods or ["ANY"]:
                pf.routes.append(Route(m, path, node.lineno, "defines"))

    def visit(body, parent: str = ""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if parent else "function")
                name = f"{parent}.{node.name}" if parent else node.name
                doc = (ast.get_docstring(node) or "").strip().split("\n")[0][:200]
                pf.symbols.append(Symbol(name, kind, start, end, sig(node), doc, parent))
                if not isinstance(node, ast.ClassDef):
                    add_routes(node)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, node.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    pf.imports.append((a.name, 0))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                pf.imports.append((mod, node.level))
                for a in node.names:  # "from . import views" -> module ".views"
                    if a.name != "*":
                        pf.imports.append(((mod + "." if mod else "") + a.name, node.level))
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                visit(getattr(node, "body", []), parent)
    visit(tree.body)
    # Flask Blueprint(url_prefix=...) / FastAPI APIRouter(prefix=...)
    pm = re.search(r"(?:Blueprint|APIRouter)\([^)]*?\b(?:url_)?prefix\s*=\s*['\"]([^'\"]+)['\"]", text, re.S)
    if pm:
        prefix = normalise_route(pm.group(1))
        if prefix and prefix != "/":
            for r in pf.routes:
                if r.role == "defines" and not r.path.startswith(prefix + "/") and r.path != prefix:
                    r.path = normalise_route(prefix + "/" + r.path.lstrip("/"))
    # client-side calls in Python (requests/httpx)
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"\b(?:requests|httpx|client|session)\.(get|post|put|patch|delete)\(\s*[fr]?['\"]([^'\"]+)['\"]", line):
            p = normalise_route(m.group(2))
            if p:
                pf.routes.append(Route(m.group(1).upper(), p, i, "calls"))
    return pf


# ---------------------------------------------------------------- JS / TS
_JS_IMPORT = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+[\w*{}\s,$]+\s+from\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]""")
_JS_SYMS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
    (re.compile(r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$"), "method"),
]
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "constructor_", "else", "do", "try"}
_JS_SERVER_DEF = re.compile(r"\b(?:app|router|server|api|routes?|fastify)\.(get|post|put|patch|delete|all|use)\(\s*['\"`]([^'\"`]+)['\"`]")
_JS_FETCH = re.compile(r"\bfetch\(\s*['\"`]([^'\"`]+)['\"`](?:\s*,\s*\{[^}]*?method\s*:\s*['\"](\w+)['\"])?")
_JS_CLIENT = re.compile(r"\b(?:axios|api|http|client|instance|\$http|request)\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]")


def _parse_js(text: str) -> ParsedFile:
    pf = ParsedFile()
    lines = text.splitlines()
    pf.doc = _leading_comment(lines)
    is_server = bool(re.search(r"require\(['\"]express['\"]\)|from ['\"](express|fastify|koa|hono)['\"]|express\(\)|Router\(\)", text))
    for m in _JS_IMPORT.finditer(text):
        pf.imports.append((m.group(1), 0))
    for i, line in enumerate(lines):
        for rx, kind in _JS_SYMS:
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in _JS_KEYWORDS:
                break
            end = _block_end(lines, i) if kind != "type" or "{" in line else i
            if kind == "type" and end == i and "{" in line:
                end = _block_end(lines, i)
            if kind == "function" and name[:1].isupper() and re.search(r"return\s*\(?\s*<|<\w", "\n".join(lines[i:end + 1])):
                kind = "component"
            pf.symbols.append(Symbol(name, kind, i + 1, end + 1, line.strip()[:160]))
            break
        if is_server:
            for m in _JS_SERVER_DEF.finditer(line):
                if m.group(1) == "use":
                    continue
                p = normalise_route(m.group(2))
                if p:
                    pf.routes.append(Route(m.group(1).upper().replace("ALL", "ANY"), p, i + 1, "defines"))
        for m in _JS_FETCH.finditer(line):
            p = normalise_route(m.group(1))
            if p:
                pf.routes.append(Route((m.group(2) or "GET").upper(), p, i + 1, "calls"))
        if not is_server:
            for m in _JS_CLIENT.finditer(line):
                p = normalise_route(m.group(2))
                if p:
                    pf.routes.append(Route(m.group(1).upper(), p, i + 1, "calls"))
    # fetch with method on following lines: fetch('/x', {\n method: 'POST'
    for r in pf.routes:
        if r.role == "calls" and r.method == "GET":
            window = "\n".join(lines[r.line - 1:r.line + 4])
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
            if mm:
                r.method = mm.group(1).upper()
    _attach_parents(pf.symbols)
    return pf


# ---------------------------------------------------------------- brace languages
_LANG_RULES = {
    "java": dict(
        imports=[r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;"],
        syms=[(r"^\s*(?:public|private|protected|abstract|final|static|\s)*(?:class|interface|enum|record)\s+(\w+)", "class"),
              (r"^\s*(?:@\w+\s+)*(?:public|private|protected|static|final|synchronized|abstract|\s)+[\w<>\[\],.? ]+\s+(\w+)\s*\([^;]*$", "method")],
        routes=[r"@(Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]+)\""]),
    "kotlin": dict(
        imports=[r"^\s*import\s+([\w.]+)"],
        syms=[(r"^\s*(?:\w+\s+)*(?:class|interface|object)\s+(\w+)", "class"),
              (r"^\s*(?:\w+\s+)*fun\s+(?:<[^>]+>\s*)?(?:\w+\.)?(\w+)\s*\(", "function")],
        routes=[r"@(Get|Post|Put|Patch|Delete|Request)Mapping\(\s*\"([^\"]+)\""]),
    "go": dict(
        imports=[r"^\s*import\s+\"([^\"]+)\"", r"^\s+(?:\w+\s+)?\"([^\"]+)\"\s*$"],
        syms=[(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", "function"),
              (r"^type\s+(\w+)\s+(?:struct|interface)", "type")],
        routes=[r"\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE)\(\s*\"([^\"]+)\""]),
    "rust": dict(
        imports=[r"^\s*use\s+([\w:]+)", r"^\s*mod\s+(\w+)\s*;"],
        syms=[(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)", "function"),
              (r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl(?:<[^>]*>)?)\s+(\w+)", "type")],
        routes=[r"#\[(get|post|put|patch|delete)\(\"([^\"]+)\""]),
    "csharp": dict(
        imports=[r"^\s*using\s+([\w.]+)\s*;"],
        syms=[(r"^\s*(?:\w+\s+)*(?:class|interface|struct|enum|record)\s+(\w+)", "class"),
              (r"^\s*(?:public|private|protected|internal|static|async|override|virtual|\s)+[\w<>\[\],.? ]+\s+(\w+)\s*\([^;]*$", "method")],
        routes=[r"\[Http(Get|Post|Put|Patch|Delete)\(\"([^\"]+)\""]),
    "php": dict(
        imports=[r"^\s*(?:require|include)(?:_once)?\s*\(?\s*['\"]([^'\"]+)['\"]", r"^\s*use\s+([\w\\]+)"],
        syms=[(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", "class"),
              (r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)", "function")],
        routes=[r"Route::(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]"]),
    "c": dict(
        imports=[r"^\s*#\s*include\s+\"([^\"]+)\""],
        syms=[(r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;]*\)\s*\{?\s*$", "function"),
              (r"^\s*(?:typedef\s+)?struct\s+(\w+)\s*\{", "type")],
        routes=[]),
    "cpp": dict(
        imports=[r"^\s*#\s*include\s+\"([^\"]+)\""],
        syms=[(r"^\s*(?:class|struct)\s+(\w+)[^;]*$", "class"),
              (r"^(?:[\w:<>,\*&~\s]+?)\b([\w:~]+)\s*\([^;]*\)\s*(?:const)?\s*(?:override)?\s*\{?\s*$", "function")],
        routes=[]),
    "swift": dict(
        imports=[r"^\s*import\s+(\w+)"],
        syms=[(r"^\s*(?:\w+\s+)*(?:class|struct|enum|protocol|extension)\s+(\w+)", "class"),
              (r"^\s*(?:\w+\s+)*func\s+(\w+)", "function")],
        routes=[]),
    "dart": dict(
        imports=[r"^\s*import\s+'([^']+)'"],
        syms=[(r"^\s*(?:abstract\s+)?class\s+(\w+)", "class"),
              (r"^\s*(?:[\w<>?]+\s+)+(\w+)\s*\([^;]*\)\s*(?:async\s*)?\{\s*$", "function")],
        routes=[]),
}
_C_NOT_FUNC = {"if", "for", "while", "switch", "return", "sizeof", "else", "catch", "new", "delete"}


def _parse_brace_lang(text: str, lang: str) -> ParsedFile:
    rules = _LANG_RULES[lang]
    pf = ParsedFile()
    lines = text.splitlines()
    pf.doc = _leading_comment(lines)
    imp_rx = [re.compile(r) for r in rules["imports"]]
    sym_rx = [(re.compile(r), k) for r, k in rules["syms"]]
    route_rx = [re.compile(r, re.I) for r in rules["routes"]]
    in_go_import = False
    for i, line in enumerate(lines):
        if lang == "go":
            if re.match(r"^\s*import\s*\($", line):
                in_go_import = True
                continue
            if in_go_import:
                if line.strip() == ")":
                    in_go_import = False
                    continue
                m = re.search(r"\"([^\"]+)\"", line)
                if m:
                    pf.imports.append((m.group(1), 0))
                continue
        for rx in imp_rx[:1] if lang == "go" else imp_rx:
            m = rx.match(line)
            if m:
                pf.imports.append((m.group(1), 0))
        for rx in route_rx:
            for m in rx.finditer(line):
                p = normalise_route(m.group(2))
                if p:
                    meth = m.group(1).upper()
                    meth = "ANY" if meth in ("REQUEST", "HANDLEFUNC", "HANDLE") else meth
                    pf.routes.append(Route(meth, p, i + 1, "defines"))
        for rx, kind in sym_rx:
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in _C_NOT_FUNC or line.rstrip().endswith(";"):
                break
            end = _block_end(lines, i)
            if end == i and kind in ("function", "method") and lang in ("c", "cpp", "java", "csharp", "dart"):
                break  # prototype / call, not a definition
            pf.symbols.append(Symbol(name, kind, i + 1, end + 1, line.strip()[:160]))
            break
    _attach_parents(pf.symbols)
    return pf


# ---------------------------------------------------------------- everything else
_GENERIC_SYM = re.compile(r"^\s*(?:export\s+|public\s+|private\s+|local\s+)*(?:def|function|fn|func|class|module|sub|proc)\s+([A-Za-z_][\w.:!?]*)")
_MD_HEAD = re.compile(r"^(#{1,3})\s+(.+)")


def _parse_generic(text: str, lang: str) -> ParsedFile:
    pf = ParsedFile()
    lines = text.splitlines()
    if lang == "markdown":
        heads = [(i, m.group(2).strip()) for i, l in enumerate(lines) if (m := _MD_HEAD.match(l))]
        for k, (i, title) in enumerate(heads):
            end = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(lines) - 1
            pf.symbols.append(Symbol(title[:80], "section", i + 1, end + 1, lines[i].strip()))
        first_para = next((l.strip() for l in lines if l.strip() and not l.startswith(("#", "!", "[", "<"))), "")
        pf.doc = first_para[:300]
        return pf
    pf.doc = _leading_comment(lines)
    if lang in ("ruby", "lua", "shell", "r", "python"):
        starts = [(i, m.group(1)) for i, l in enumerate(lines) if (m := _GENERIC_SYM.match(l))]
        for k, (i, name) in enumerate(starts):
            end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines) - 1
            pf.symbols.append(Symbol(name, "function", i + 1, end + 1, lines[i].strip()[:160]))
        if lang == "ruby":
            for m in re.finditer(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]", text, re.M):
                pf.imports.append((m.group(1), 0))
            for i, l in enumerate(lines, 1):
                for m in re.finditer(r"^\s*(get|post|put|patch|delete)\s+['\"]([^'\"]+)['\"]", l):
                    p = normalise_route(m.group(2))
                    if p:
                        pf.routes.append(Route(m.group(1).upper(), p, i, "defines"))
    if lang in ("html", "vue", "svelte"):
        for m in re.finditer(r"<script[^>]+src=['\"]([^'\"]+)['\"]", text):
            pf.imports.append((m.group(1), 0))
        if lang in ("vue", "svelte"):
            js = _parse_js(text)
            pf.imports += js.imports
            pf.routes += js.routes
            pf.symbols += js.symbols
    return pf


def _attach_parents(symbols: List[Symbol]) -> None:
    classes = [s for s in symbols if s.kind in ("class", "type")]
    for s in symbols:
        if s.kind in ("method", "function"):
            for c in classes:
                if c is not s and c.start < s.start and s.end <= c.end:
                    s.parent = c.name
                    if s.kind == "function" and c.kind == "class":
                        s.kind = "method"
                    s.name = f"{c.name}.{s.name}" if not s.name.startswith(c.name + ".") else s.name


def parse(text: str, lang: str) -> ParsedFile:
    if lang == "python":
        return _parse_python(text)
    if lang in ("javascript", "typescript"):
        return _parse_js(text)
    if lang in _LANG_RULES:
        return _parse_brace_lang(text, lang)
    return _parse_generic(text, lang)
