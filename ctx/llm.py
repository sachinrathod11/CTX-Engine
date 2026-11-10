"""Tiny Ollama client (standard library only).

Every caller treats the LLM as optional: if Ollama isn't running or a model is
missing, they fall back to heuristics, so the tool always produces a prompt.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import List, Optional

from .config import Config


class LLMUnavailable(RuntimeError):
    pass


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class Ollama:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._checked_at = 0.0
        self._models: Optional[List[str]] = None

    # ---------- plumbing ----------
    def _post(self, path: str, payload: dict, timeout: Optional[float] = None) -> dict:
        req = urllib.request.Request(
            self.cfg.ollama_url.rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.cfg.llm_timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise LLMUnavailable(f"Ollama HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LLMUnavailable(f"Ollama not reachable at {self.cfg.ollama_url}: {e}") from e

    def models(self, refresh: bool = False) -> List[str]:
        if not self.cfg.use_llm:
            return []
        if self._models is not None and not refresh and time.time() - self._checked_at < 30:
            return self._models
        try:
            with urllib.request.urlopen(self.cfg.ollama_url.rstrip("/") + "/api/tags", timeout=2) as r:
                data = json.loads(r.read().decode())
            self._models = [m.get("name", "") for m in data.get("models", [])]
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            self._models = []
        self._checked_at = time.time()
        return self._models

    def has(self, model: str) -> bool:
        names = self.models()
        base = model if ":" in model else model + ":latest"
        return model in names or base in names

    @property
    def can_generate(self) -> bool:
        return self.has(self.cfg.llm_model)

    @property
    def can_embed(self) -> bool:
        return self.has(self.cfg.embed_model)

    def status(self) -> dict:
        names = self.models(refresh=True)
        return {
            "ollama_running": bool(names) or self._ping(),
            "llm_model": self.cfg.llm_model,
            "llm_ready": self.has(self.cfg.llm_model),
            "embed_model": self.cfg.embed_model,
            "embed_ready": self.has(self.cfg.embed_model),
            "use_llm": self.cfg.use_llm,
        }

    def _ping(self) -> bool:
        if not self.cfg.use_llm:
            return False
        try:
            urllib.request.urlopen(self.cfg.ollama_url.rstrip("/") + "/api/version", timeout=2)
            return True
        except Exception:
            return False

    # ---------- API ----------
    def generate(self, prompt: str, system: str = "", as_json: bool = False,
                 max_tokens: int = 400, temperature: float = 0.1) -> str:
        if not self.can_generate:
            raise LLMUnavailable(f"model {self.cfg.llm_model} not available")
        payload = {
            "model": self.cfg.llm_model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "think": False,  # Qwen3 thinking mode is slow and not needed here
            "options": {"temperature": temperature, "num_ctx": self.cfg.num_ctx,
                        "num_predict": max_tokens},
        }
        if as_json:
            payload["format"] = "json"
        try:
            data = self._post("/api/generate", payload)
        except LLMUnavailable as e:
            if "think" in str(e).lower():  # older Ollama / non-thinking model
                payload.pop("think", None)
                data = self._post("/api/generate", payload)
            else:
                raise
        return _THINK_RE.sub("", data.get("response", "")).strip()

    def generate_json(self, prompt: str, system: str = "", max_tokens: int = 400) -> dict:
        text = self.generate(prompt, system=system, as_json=True, max_tokens=max_tokens)
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
        raise LLMUnavailable("model did not return valid JSON")

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not self.can_embed:
            raise LLMUnavailable(f"model {self.cfg.embed_model} not available")
        out: List[List[float]] = []
        for i in range(0, len(texts), 32):
            data = self._post("/api/embed", {"model": self.cfg.embed_model,
                                              "input": texts[i:i + 32], "truncate": True})
            out.extend(data.get("embeddings", []))
        if len(out) != len(texts):
            raise LLMUnavailable("embedding count mismatch")
        return out
