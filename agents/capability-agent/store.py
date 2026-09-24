"""Crawls the Git repos with the LLM agent when asked, and caches the result per commit."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Callable

import report as report_builder
from git_source import checkout, remote_commit

log = logging.getLogger(__name__)
Describer = Callable[[dict[str, Any], dict[str, Path]], dict[str, Any]]


@dataclass(frozen=True)
class Snapshot:
    report: dict[str, Any]
    roots: dict[str, Path]
    commits: tuple[str, str | None]
    loaded_at: float = field(default_factory=time.time)


class CapabilityStore:
    def __init__(self, module_repo: str, module_ref: str | None, score_repo: str | None, score_ref: str | None,
                 workdir: Path, describe: Describer, check_interval: float = 60):
        self.module_repo, self.module_ref = module_repo, module_ref
        self.score_repo, self.score_ref = score_repo, score_ref
        self.workdir, self.describe, self.check_interval = workdir, describe, check_interval
        self._snapshot: Snapshot | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self._dirs: deque[Path] = deque()
        self._builds = 0

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    def current(self) -> Snapshot:
        """Snapshot for the commits the refs point to now; crawls the repos with the agent if they moved."""
        with self._lock:
            if self._snapshot and time.time() - self._checked_at < self.check_interval:
                return self._snapshot
            wanted = (remote_commit(self.module_repo, self.module_ref),
                      remote_commit(self.score_repo, self.score_ref) if self.score_repo else None)
            if not self._snapshot or self._snapshot.commits != wanted:
                self._snapshot = self._crawl()
            self._checked_at = time.time()
            return self._snapshot

    def _crawl(self) -> Snapshot:
        self._builds += 1
        target = self.workdir / f"build-{self._builds}"
        try:
            modules = checkout(self.module_repo, self.module_ref, target / "modules")
            score = checkout(self.score_repo, self.score_ref, target / "score") if self.score_repo else None
            roots = {"modules": modules.path} | ({"score": score.path} if score else {})
            log.info("Agent crawling %s@%s", self.module_repo, modules.commit)
            report = self.describe(report_builder.build(modules, score), roots)
        except Exception:
            # Nothing is cached, so the next request crawls again.
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._dirs.append(target)
        while len(self._dirs) > 2:  # keep the previous checkout for requests still reading it
            shutil.rmtree(self._dirs.popleft(), ignore_errors=True)
        log.info("Agent found %d capabilities at %s", len(report["capabilities"]), modules.commit)
        return Snapshot(report, roots, (modules.commit, score.commit if score else None))
