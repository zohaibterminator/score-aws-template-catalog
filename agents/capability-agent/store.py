"""Crawls the Git repos with the LLM agent when asked, and caches the result per commit."""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

import report as report_builder
from git_source import checkout, remote_commit

log = logging.getLogger(__name__)
Describer = Callable[[dict[str, Any], dict[str, Path]], dict[str, Any]]

# Files in the Score repo that define capabilities. Anything else there (state.yaml, workloads/,
# generated/, manifests) changes on every provisioning request and does not change what can be deployed.
CAPABILITY_FILE = re.compile(r"(^|/)\.score-k8s/[^/]+\.provisioners\.ya?ml$|(^|/)README[^/]*$", re.I)


def _changed_files(repo: Path, old: str, new: str) -> list[str] | None:
    """Paths changed between two commits of a checkout, or None if Git cannot tell."""
    result = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", old, new],
                            capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else None


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

    def _reuse(self, modules, score) -> dict[str, Any] | None:
        """The previous report, when only non-capability files changed in the Score repo since it was built.

        score-api commits request state to the Score repo on every provisioning request; re-crawling with
        the LLM for those would cost minutes and tokens each time without changing any capability.
        """
        previous = self._snapshot
        if previous is None or score is None or previous.commits[0] != modules.commit:
            return None
        changed = _changed_files(score.path, previous.commits[1], score.commit) if previous.commits[1] else None
        if changed is None or any(CAPABILITY_FILE.search(path) for path in changed):
            return None
        log.info("Score repo moved to %s with no capability changes (%d files); reusing the crawl",
                 score.commit, len(changed))
        report = copy.deepcopy(previous.report)
        report["sources"]["score_workloads"] = score.describe()
        return report

    def _crawl(self) -> Snapshot:
        self._builds += 1
        target = self.workdir / f"build-{self._builds}"
        try:
            modules = checkout(self.module_repo, self.module_ref, target / "modules")
            score = checkout(self.score_repo, self.score_ref, target / "score") if self.score_repo else None
            roots = {"modules": modules.path} | ({"score": score.path} if score else {})
            log.info("Agent crawling %s@%s", self.module_repo, modules.commit)
            report = self._reuse(modules, score) or self.describe(report_builder.build(modules, score), roots)
        except Exception:
            # Nothing is cached, so the next request crawls again.
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._dirs.append(target)
        while len(self._dirs) > 2:  # keep the previous checkout for requests still reading it
            shutil.rmtree(self._dirs.popleft(), ignore_errors=True)
        log.info("Agent found %d capabilities at %s", len(report["capabilities"]), modules.commit)
        return Snapshot(report, roots, (modules.commit, score.commit if score else None))
