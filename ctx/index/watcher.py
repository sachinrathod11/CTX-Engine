"""Keeps an index fresh: watches the folder and re-indexes only changed files."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional, Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .indexer import ProjectIndex
from .scanner import DEFAULT_IGNORE_DIRS


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "IndexWatcher"):
        self.w = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        for attr in ("src_path", "dest_path"):
            p = getattr(event, attr, None)
            if p:
                self.w.queue(Path(p if isinstance(p, str) else p.decode()), event.is_directory)


class IndexWatcher:
    """Batches file events for `debounce` seconds, then runs one incremental update."""

    def __init__(self, index: ProjectIndex, debounce: float = 1.5,
                 on_update: Optional[Callable[[dict], None]] = None):
        self.index = index
        self.debounce = debounce
        self.on_update = on_update
        self._pending: Set[Path] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._observer: Optional[Observer] = None

    def queue(self, path: Path, is_dir: bool) -> None:
        try:
            rel_parts = path.resolve().relative_to(self.index.root).parts
        except (ValueError, OSError):
            return
        if any(p in DEFAULT_IGNORE_DIRS for p in rel_parts):
            return
        with self._lock:
            if is_dir and not path.exists():
                # folder deleted or renamed away: every indexed file under it is gone
                prefix = "/".join(rel_parts) + "/"
                for known in self.index.store.file_hashes():
                    if known.startswith(prefix):
                        self._pending.add(self.index.root / known)
            elif is_dir:
                self._pending.update(p for p in path.rglob("*") if p.is_file())
            else:
                self._pending.add(path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths, self._pending = self._pending, set()
        if not paths:
            return
        try:
            stats = self.index.update(paths=paths)
        except Exception as e:  # keep watching even if one update fails
            stats = {"error": str(e)}
        if self.on_update:
            self.on_update(stats)

    def start(self) -> "IndexWatcher":
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.index.root), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        return self

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
