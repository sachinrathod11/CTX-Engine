"""Walks a project folder and yields the source files worth indexing."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pathspec

from ..config import INDEX_DIRNAME

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", INDEX_DIRNAME, "node_modules", "bower_components", "vendor",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    "venv", ".venv", "env", ".env", "virtualenv", "site-packages",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit", ".angular",
    "coverage", ".gradle", ".idea", ".vscode", ".terraform", ".cache", "Pods",
    "DerivedData", ".dart_tool", "migrations_backup",
}

# Never read these: they usually hold secrets, and the prompt goes to a cloud chatbot.
SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
    "*.kdbx", "credentials*.json", "service-account*.json", "secrets.*", "*.secret*",
]

SKIP_FILE_PATTERNS = [
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "pnpm-lock.yaml",
    "yarn.lock", "poetry.lock", "Cargo.lock", "*.pyc", "*.class", "*.o", "*.so", "*.dll",
    "*.exe", "*.bin", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg", "*.webp",
    "*.pdf", "*.zip", "*.tar", "*.gz", "*.mp4", "*.mp3", "*.woff", "*.woff2", "*.ttf",
    "*.sqlite", "*.db", "*.parquet", "*.pkl", "*.pt", "*.onnx", "*.ipynb_checkpoints",
]

LANG_BY_EXT: Dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".swift": "swift", ".dart": "dart", ".lua": "lua", ".r": "r", ".m": "objc",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".html": "html", ".htm": "html", ".vue": "vue", ".svelte": "svelte",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql", ".proto": "proto",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".xml": "xml", ".gradle": "gradle",
    ".md": "markdown", ".rst": "text", ".txt": "text",
}
LANG_BY_NAME = {
    "Dockerfile": "docker", "Makefile": "make", "CMakeLists.txt": "cmake",
    "requirements.txt": "text", "Gemfile": "ruby", "Procfile": "text",
}


@dataclass
class ScannedFile:
    rel: str          # forward-slash path relative to the project root
    abs: Path
    lang: str
    size: int
    mtime: float


def language_of(path: Path) -> Optional[str]:
    if path.name in LANG_BY_NAME:
        return LANG_BY_NAME[path.name]
    return LANG_BY_EXT.get(path.suffix.lower())


def _match_any(name: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def is_sensitive(name: str) -> bool:
    return _match_any(name, SENSITIVE_PATTERNS) and not name.endswith((".example", ".sample", ".template"))


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


class Scanner:
    def __init__(self, root: Path, max_file_bytes: int = 400_000, extra_ignore: Optional[List[str]] = None):
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        self._specs: Dict[str, Optional[pathspec.GitIgnoreSpec]] = {}
        lines = list(extra_ignore or [])
        ctxignore = self.root / ".ctxignore"
        if ctxignore.exists():
            lines += ctxignore.read_text(errors="ignore").splitlines()
        self._extra = pathspec.GitIgnoreSpec.from_lines(lines)

    def _spec_for(self, dirpath: Path) -> Optional[pathspec.GitIgnoreSpec]:
        key = str(dirpath)
        if key not in self._specs:
            gi = dirpath / ".gitignore"
            self._specs[key] = (pathspec.GitIgnoreSpec.from_lines(gi.read_text(errors="ignore").splitlines())
                                if gi.exists() else None)
        return self._specs[key]

    def _ignored(self, path: Path, is_dir: bool) -> bool:
        rel = path.relative_to(self.root).as_posix() + ("/" if is_dir else "")
        if self._extra.match_file(rel):
            return True
        # check every .gitignore from the root down to the file's parent
        cur = self.root
        parts = path.relative_to(self.root).parts
        for i in range(len(parts)):
            spec = self._spec_for(cur)
            if spec is not None:
                sub = "/".join(parts[i:]) + ("/" if is_dir else "")
                if spec.match_file(sub):
                    return True
            cur = cur / parts[i]
        return False

    def accept(self, path: Path) -> Optional[ScannedFile]:
        """Decide about a single file (also used by the watcher)."""
        try:
            path = path.resolve()
            rel_parts = path.relative_to(self.root).parts
        except (ValueError, OSError):
            return None
        if any(p in DEFAULT_IGNORE_DIRS for p in rel_parts[:-1]):
            return None
        name = path.name
        if is_sensitive(name) or _match_any(name, SKIP_FILE_PATTERNS):
            return None
        lang = language_of(path)
        if lang is None or not path.is_file():
            return None
        st = path.stat()
        if st.st_size == 0 or st.st_size > self.max_file_bytes:
            return None
        if self._ignored(path, False) or _looks_binary(path):
            return None
        return ScannedFile(path.relative_to(self.root).as_posix(), path, lang, st.st_size, st.st_mtime)

    def scan(self) -> Iterator[ScannedFile]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            d = Path(dirpath)
            keep = []
            for dn in sorted(dirnames):
                if dn in DEFAULT_IGNORE_DIRS or dn.endswith(".egg-info"):
                    continue
                if self._ignored(d / dn, True):
                    continue
                keep.append(dn)
            dirnames[:] = keep
            for fn in sorted(filenames):
                sf = self.accept(d / fn)
                if sf:
                    yield sf
