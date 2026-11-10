"""Small text helpers shared by indexing and search."""
from __future__ import annotations

import re
from typing import List

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

STOPWORDS = set("""
a an the and or but if then else of to in on at by for with from as is are was were be been being
it its this that these those there here what which who whom why how when where do does did done
not no can could should would will shall may might must i me my we our you your he she they them
his her their have has had get gets got make makes made use uses using used work works working
issue problem error bug help please want need fix fixed fixing happen happens happening any some
all into about after before just also only than so very more most out up down over under again
working doesnt dont isnt cant wont not does give gives show shows tell way thing things
""".split())


def split_identifier(word: str) -> List[str]:
    parts: List[str] = []
    for piece in word.split("_"):
        if piece:
            parts.extend(p.lower() for p in _CAMEL.findall(piece))
    return parts


def code_terms(text: str) -> List[str]:
    """Identifiers in `text` split into lower-case words: getUserById -> get, user, by, id."""
    out: List[str] = []
    for ident in _IDENT.findall(text):
        sub = split_identifier(ident)
        out.extend(sub)
        if len(sub) > 1:
            out.append(ident.lower())
    return out


def query_terms(text: str) -> List[str]:
    seen, out = set(), []
    for t in code_terms(text):
        if len(t) < 2 or t in STOPWORDS or t.isdigit():
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
