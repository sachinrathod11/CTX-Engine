"""Token counting. Uses tiktoken when installed and its encoding is cached; otherwise
a character-based estimate (~4 chars per token for code), which is close enough
for budgeting."""
from __future__ import annotations

import re
from functools import lru_cache

_WORDISH = re.compile(r"\w+|[^\w\s]")


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken  # optional
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def count(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    # Blend of char and word estimates; tracks BPE counts on code within ~10-15%.
    return max(1, int(0.5 * len(text) / 4 + 0.5 * len(_WORDISH.findall(text)) * 0.75))


def method() -> str:
    return "tiktoken o200k_base" if _encoder() is not None else "estimate"
