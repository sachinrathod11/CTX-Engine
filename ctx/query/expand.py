"""Query understanding: pull search terms out of a vague question and add related terms.

With Ollama: the local LLM proposes identifiers likely to appear in the relevant code.
Without it: a hand-written dictionary of common web/app development concepts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from ..llm import LLMUnavailable, Ollama
from ..textutil import STOPWORDS, query_terms, split_identifier

SYNONYMS = {
    "login": ["auth", "signin", "sign_in", "authenticate", "session", "password", "credentials", "token", "jwt", "user"],
    "log": ["login", "auth"],
    "signin": ["login", "auth"],
    "logout": ["auth", "session", "signout"],
    "signup": ["register", "registration", "auth", "user", "create_user"],
    "register": ["signup", "registration", "user"],
    "auth": ["login", "authenticate", "session", "token", "jwt", "permission"],
    "password": ["hash", "verify_password", "reset", "credentials"],
    "reset": ["password", "token", "forgot"],
    "forgot": ["reset", "password"],
    "token": ["jwt", "auth", "session"],
    "session": ["cookie", "auth", "login"],
    "permission": ["role", "admin", "authorize", "forbidden", "access"],
    "admin": ["role", "permission", "is_admin"],
    "button": ["onclick", "onsubmit", "click", "handler", "submit", "form"],
    "click": ["onclick", "handler", "button"],
    "form": ["submit", "onsubmit", "input", "validation"],
    "submit": ["onsubmit", "form", "post"],
    "page": ["component", "route", "view", "render"],
    "screen": ["component", "view", "page"],
    "api": ["fetch", "axios", "route", "endpoint", "request", "response"],
    "endpoint": ["route", "api", "handler"],
    "request": ["fetch", "axios", "api", "http"],
    "fetch": ["api", "request", "axios"],
    "database": ["db", "model", "query", "sql", "orm", "migration", "schema"],
    "db": ["database", "model", "query", "session"],
    "save": ["commit", "insert", "update", "store", "db"],
    "payment": ["checkout", "stripe", "order", "billing", "charge", "invoice"],
    "checkout": ["order", "cart", "payment"],
    "cart": ["checkout", "order", "items"],
    "order": ["checkout", "cart", "orders"],
    "product": ["products", "catalog", "item", "inventory"],
    "search": ["query", "filter", "find"],
    "upload": ["file", "multipart", "storage", "image"],
    "image": ["upload", "img", "media", "storage"],
    "email": ["mail", "smtp", "send_email", "notification"],
    "notification": ["notify", "email", "push", "message"],
    "error": ["exception", "catch", "raise", "throw", "handler"],
    "crash": ["exception", "error", "traceback"],
    "slow": ["performance", "cache", "query", "timeout"],
    "cache": ["redis", "memo", "ttl"],
    "style": ["css", "class", "theme", "layout"],
    "css": ["style", "classname", "theme"],
    "test": ["tests", "spec", "assert", "fixture", "mock"],
    "deploy": ["docker", "config", "env", "build", "ci"],
    "config": ["settings", "env", "environment"],
    "env": ["config", "environ", "settings"],
    "websocket": ["socket", "ws", "realtime"],
    "chat": ["message", "socket", "conversation"],
    "profile": ["user", "account", "settings"],
    "user": ["users", "account", "profile"],
    "state": ["usestate", "store", "redux", "context"],
    "redirect": ["navigate", "router", "route"],
    "route": ["router", "path", "navigate", "endpoint"],
    "cors": ["origin", "headers", "middleware"],
    "timeout": ["retry", "async", "await"],
}


@dataclass
class Expansion:
    terms: List[str]                      # from the question itself
    extra: List[str] = field(default_factory=list)   # related terms
    phrases: List[str] = field(default_factory=list) # quoted text / error messages
    mentioned_files: List[str] = field(default_factory=list)
    source: str = "dictionary"


_FILE_RX = re.compile(r"[\w./-]+\.(?:py|js|jsx|ts|tsx|java|go|rs|rb|php|cs|c|cpp|h|vue|svelte|kt|swift|html|css)\b")
_QUOTE_RX = re.compile(r"[\"'`]([^\"'`]{6,120})[\"'`]")


def heuristic_expand(question: str) -> Expansion:
    terms = query_terms(question)
    extra: List[str] = []
    for t in terms:
        for s in SYNONYMS.get(t, []) + SYNONYMS.get(t.rstrip("s"), []):
            if s not in terms and s not in extra:
                extra.append(s)
    phrases = [m.group(1).strip() for m in _QUOTE_RX.finditer(question)]
    m = re.search(r"(?:error|exception)[:\s]+(.{6,120})", question, re.I)
    if m and not phrases:
        phrases.append(m.group(1).strip().rstrip("."))
    files = _FILE_RX.findall(question)
    return Expansion(terms, extra[:20], phrases[:3], files)


SYSTEM = ("You help search a codebase. Given a developer's question and a project overview, list identifiers "
          "and words that are likely to appear in the code relevant to the question (function names, variable "
          "names, route paths, library names, UI handler names). Return JSON: {\"keywords\": [..up to 15 short strings..]}. "
          "Only output JSON.")


def llm_expand(llm: Ollama, question: str, overview: str) -> Expansion:
    base = heuristic_expand(question)
    try:
        data = llm.generate_json(f"Project overview:\n{overview}\n\nQuestion: {question}", system=SYSTEM, max_tokens=160)
    except LLMUnavailable:
        return base
    kws = data.get("keywords") if isinstance(data, dict) else None
    if not isinstance(kws, list):
        return base
    extra: List[str] = []
    for k in kws:
        if not isinstance(k, str):
            continue
        for w in [k.strip().lower()] + split_identifier(k.strip()):
            w = w.strip("/ ")
            if 2 <= len(w) <= 40 and w not in STOPWORDS and w not in base.terms and w not in extra:
                extra.append(w)
    # keep dictionary terms too; LLM terms first
    merged = extra + [e for e in base.extra if e not in extra]
    return Expansion(base.terms, merged[:24], base.phrases, base.mentioned_files, source="llm")
