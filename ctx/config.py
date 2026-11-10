"""Settings. Defaults live here; override them in ~/.ctx/config.json or with CTX_* env vars."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

HOME_DIR = Path(os.environ.get("CTX_HOME", Path.home() / ".ctx"))
INDEX_DIRNAME = ".ctx"


@dataclass
class Config:
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:4b"            # rewrite, rerank, summaries
    embed_model: str = "embeddinggemma"    # semantic search
    use_llm: bool = True                   # set False to run fully heuristic (no Ollama)
    llm_timeout: float = 120.0
    num_ctx: int = 8192

    budget: int = 3500                     # default prompt size in tokens
    candidates: int = 15                   # files passed from search to rerank
    max_files: int = 6                     # files that end up in the prompt
    max_file_bytes: int = 400_000          # skip bigger files (minified bundles, data dumps)
    summarize: bool = True                 # LLM file summaries during indexing
    extra_ignore: list = field(default_factory=list)

    server_host: str = "127.0.0.1"
    server_port: int = 7777

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = HOME_DIR / "config.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for f in fields(cls):
                    if f.name in data:
                        setattr(cfg, f.name, data[f.name])
            except (OSError, ValueError):
                pass
        for f in fields(cls):
            env = os.environ.get("CTX_" + f.name.upper())
            if env is None:
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                setattr(cfg, f.name, env.lower() in ("1", "true", "yes", "on"))
            elif isinstance(cur, int):
                setattr(cfg, f.name, int(env))
            elif isinstance(cur, float):
                setattr(cfg, f.name, float(env))
            elif isinstance(cur, list):
                setattr(cfg, f.name, [s for s in env.split(",") if s])
            else:
                setattr(cfg, f.name, env)
        return cfg

    def save(self) -> Path:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        path = HOME_DIR / "config.json"
        path.write_text(json.dumps(asdict(self), indent=2))
        return path
