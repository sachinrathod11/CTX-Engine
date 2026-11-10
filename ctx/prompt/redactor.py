"""Removes secrets before a prompt leaves the laptop (it may be pasted into a cloud chatbot)."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Tuple

# (label, regex, group to replace: 0 = whole match)
PATTERNS: List[Tuple[str, re.Pattern, int]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("aws-secret", re.compile(r"(?i)aws.{0,20}secret.{0,20}?['\"]([A-Za-z0-9/+=]{40})['\"]"), 1),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"), 0),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), 0),
    ("webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{10,}"), 0),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), 0),
    ("sendgrid-key", re.compile(r"\bSG\.[\w\-]{16,}\.[\w\-]{20,}"), 0),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), 0),
    ("url-password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:([^@\s/]{3,})@"), 1),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_\-.=]{20,})"), 1),
]

# key = "literal" where the key name says it's a secret
ASSIGN = re.compile(
    r"""(?i)\b([\w.\-]*(?:secret|token|passw(?:or)?d|pwd|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth[_\-]?key|credential)[\w.\-]*)"""
    r"""\s*(?:=|:|=>)\s*(['"])([^'"\n]{6,})\2""")

PLACEHOLDER = re.compile(r"(?i)^(?:x+|\*+|\.+|changeme|change[_\-]?me|your[_\-].*|<.*>|\$\{.*\}|example.*|test|dev|password|secret|none|null|todo)$")


@dataclass
class Finding:
    kind: str
    where: str
    line: int


def _entropy(s: str) -> float:
    c = Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def redact(text: str, where: str = "", line_offset: int = 0) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def mark(kind: str, start: int) -> None:
        findings.append(Finding(kind, where, line_offset + text.count("\n", 0, start) + 1))

    spans: List[Tuple[int, int, str]] = []
    for kind, rx, grp in PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span(grp)
            spans.append((s, e, kind))
    for m in ASSIGN.finditer(text):
        key, val = m.group(1).lower(), m.group(3).strip()
        if "[REDACTED" in val or PLACEHOLDER.match(val) or (" " in val and re.fullmatch(r"[A-Za-z ,.'!?()\-]+", val)):
            continue  # already redacted, placeholders, plain-English messages ("Invalid token")
        password_key = re.search(r"passw|pwd", key) is not None
        if password_key or (len(val) >= 8 and _entropy(val) >= 3.0):
            spans.append((m.start(3), m.end(3), "secret-value"))
    if not spans:
        return text, findings
    # merge overlapping spans; a specific label (stripe-key) beats the generic one
    spans.sort(key=lambda x: (x[0], x[2] == "secret-value", -x[1]))
    merged: List[List] = []
    for s, e, k in spans:
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    out, pos = [], 0
    for s, e, k in merged:
        out.append(text[pos:s])
        out.append(f"[REDACTED {k}]")
        mark(k, s)
        pos = e
    out.append(text[pos:])
    return "".join(out), findings
