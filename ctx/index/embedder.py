"""Turns text into vectors for semantic search.

Primary: embeddinggemma through Ollama. Fallback (no Ollama): a hashed bag of code
terms, so search still works offline - just less "semantic"."""
from __future__ import annotations

import hashlib
from typing import List

import numpy as np

from ..llm import LLMUnavailable, Ollama
from ..textutil import code_terms

HASH_DIM = 512
HASH_MODEL = "hash-512"


def _hash_vec(text: str) -> np.ndarray:
    v = np.zeros(HASH_DIM, dtype=np.float32)
    terms = [t for t in code_terms(text) if len(t) > 1]
    feats = terms + [a + "_" + b for a, b in zip(terms, terms[1:])]
    for f in feats:
        h = int.from_bytes(hashlib.md5(f.encode()).digest()[:4], "little")
        v[h % HASH_DIM] += 1.0 if (h >> 31) & 1 else -1.0
    v = np.sign(v) * np.log1p(np.abs(v))
    n = np.linalg.norm(v)
    return v / n if n else v


class Embedder:
    def __init__(self, llm: Ollama, model_name: str):
        self.llm = llm
        self.model = model_name  # an Ollama model or HASH_MODEL

    @classmethod
    def best_available(cls, llm: Ollama) -> "Embedder":
        return cls(llm, llm.cfg.embed_model if llm.can_embed else HASH_MODEL)

    def _prefix(self, text: str, is_query: bool) -> str:
        if "embeddinggemma" in self.model:
            return ("task: code retrieval | query: " if is_query else "title: none | text: ") + text
        return text

    def embed_docs(self, texts: List[str]) -> List[np.ndarray]:
        if self.model == HASH_MODEL:
            return [_hash_vec(t) for t in texts]
        vecs = self.llm.embed([self._prefix(t[:6000], False) for t in texts])
        return [np.asarray(v, dtype=np.float32) for v in vecs]

    def embed_query(self, text: str) -> np.ndarray:
        if self.model == HASH_MODEL:
            return _hash_vec(text)
        try:
            return np.asarray(self.llm.embed([self._prefix(text, True)])[0], dtype=np.float32)
        except LLMUnavailable:
            raise
