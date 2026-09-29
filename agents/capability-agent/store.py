"""Reads the Git repos and caches the result per commit.

Two views, with different costs:
  current(): the LLM crawl (Claude describes the capabilities). Used only for list_capabilities.
  facts():   deterministic Terraform and Score facts from Git, no LLM. Used for everything else, including every
             score-api call, check_request and start-up. It reuses the LLM crawl when that is for the same commits.
"""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass, field
import json
import logging
import os
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


class CrawlUnavailable(RuntimeError):
    """No crawl is attempted right now: backing off after a failure, or over the hourly crawl budget."""


def _changed_files(repo: Path, old: str, new: str) -> list[str] | None:
    """Paths changed between two commits of a checkout, or None if Git cannot tell."""
    result = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", old, new],
                            capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else None


def overlay_descriptions(facts: dict[str, Any], described: dict[str, Any]) -> dict[str, Any]:
    """The facts report with the LLM prose of an older crawl, for capabilities and variables that still exist."""
    report = copy.deepcopy(facts)
    old = {c["id"]: c for c in described.get("capabilities", [])}
    for capability in report["capabilities"]:
        previous = old.get(capability["id"])
        if not previous:
            continue
        for key in ("summary", "aws_service"):
            if previous.get(key) and not capability.get(key):
                capability[key] = previous[key]
        effects = {p["terraform_variable"]: p.get("effect") for p in previous.get("parameters", [])}
        for parameter in capability["parameters"]:
            if effects.get(parameter["terraform_variable"]) and not parameter.get("effect"):
                parameter["effect"] = effects[parameter["terraform_variable"]]
    return report


@dataclass(frozen=True)
class Snapshot:
    report: dict[str, Any]
    roots: dict[str, Path]
    commits: tuple[str, str | None]
    loaded_at: float = field(default_factory=time.time)


class CapabilityStore:
    def __init__(self, module_repo: str, module_ref: str | None, score_repo: str | None, score_ref: str | None,
                 workdir: Path, describe: Describer, check_interval: float = 60, max_crawls_per_hour: int = 6,
                 cache_dir: Path | None = None):
        self.module_repo, self.module_ref = module_repo, module_ref
        self.score_repo, self.score_ref = score_repo, score_ref
        self.workdir, self.describe, self.check_interval = workdir, describe, check_interval
        self._snapshot: Snapshot | None = None
        self._checked_at = 0.0
        self._facts: Snapshot | None = None
        self._facts_checked_at = 0.0
        self._lock = threading.Lock()
        self._dirs: deque[Path] = deque()
        self._builds = 0
        # Cost guards: every LLM crawl is a paid Claude run, so failures back off (1, 2, 4 ... 30 minutes)
        # and at most max_crawls_per_hour crawls run in any hour. A failing crawl can no longer loop.
        self.max_crawls_per_hour = max_crawls_per_hour
        self._llm_crawls: deque[float] = deque()
        self._failures = 0
        self._retry_at = 0.0
        self._last_error = ""
        # The last LLM crawl, saved to a persistent volume so a pod restart does not pay for a new crawl of the same
        # commits. It is served only when its commits still match the refs.
        self._cache_file = Path(cache_dir) / "crawl.json" if cache_dir else None
        self._snapshot = self._load_saved()

    def _load_saved(self) -> Snapshot | None:
        if not self._cache_file or not self._cache_file.exists():
            return None
        try:
            saved = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if saved.get("repos") != [self.module_repo, self.module_ref, self.score_repo, self.score_ref]:
                log.info("Saved crawl is for other repositories or refs; ignoring it")
                return None
            commits = tuple(saved["commits"])
            log.info("Loaded the saved crawl for commits %s (no LLM)", commits)
            return Snapshot(saved["report"], {}, (commits[0], commits[1]))
        except Exception as exc:
            log.warning("Could not read the saved crawl %s: %s", self._cache_file, exc)
            return None

    def _save(self, snapshot: Snapshot) -> None:
        if not self._cache_file:
            return
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "repos": [self.module_repo, self.module_ref, self.score_repo, self.score_ref],
                "commits": list(snapshot.commits), "report": snapshot.report}), encoding="utf-8")
            os.replace(tmp, self._cache_file)
        except Exception as exc:  # the crawl itself succeeded; only the restart cache is missing
            log.warning("Could not save the crawl to %s: %s", self._cache_file, exc)

    def retry_in(self) -> float:
        """Seconds until the next crawl may be attempted (0 when one may run now)."""
        return max(0.0, self._retry_at - time.time())

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    @property
    def facts_snapshot(self) -> Snapshot | None:
        return self._facts

    def _wanted(self) -> tuple[str, str | None]:
        return (remote_commit(self.module_repo, self.module_ref),
                remote_commit(self.score_repo, self.score_ref) if self.score_repo else None)

    def facts(self) -> Snapshot:
        """Deterministic facts for the commits the refs point to now. Never calls the LLM."""
        with self._lock:
            if self._facts and time.time() - self._facts_checked_at < self.check_interval:
                return self._facts
            wanted = self._wanted()
            if self._snapshot and self._snapshot.commits == wanted:
                self._facts = self._snapshot          # the LLM crawl of these commits is a superset of the facts
            elif not self._facts or self._facts.commits != wanted:
                self._facts = self._build(use_llm=False)
            self._facts_checked_at = time.time()
            return self._facts

    def current(self) -> Snapshot:
        """LLM crawl for the commits the refs point to now; crawls with Claude if they moved. list_capabilities only."""
        with self._lock:
            if self._snapshot and time.time() - self._checked_at < self.check_interval:
                return self._snapshot
            if self.retry_in() > 0:
                # Backing off after a failed crawl: serve the last good result, or say when we will retry.
                if self._snapshot:
                    return self._snapshot
                raise CrawlUnavailable(f"{self._last_error}; next attempt in {int(self.retry_in())}s")
            wanted = self._wanted()
            if not self._snapshot or self._snapshot.commits != wanted:
                try:
                    self._snapshot = self._build(use_llm=True)
                    self._save(self._snapshot)
                except Exception as exc:
                    self._failures += 1
                    delay = min(60 * 2 ** (self._failures - 1), 1800)
                    self._retry_at = time.time() + delay
                    self._last_error = f"crawl failed: {type(exc).__name__}: {exc}"
                    log.error("%s; failure %d, next attempt in %ds", self._last_error, self._failures, delay)
                    if self._snapshot:
                        log.warning("Serving the previous crawl (%s) until a new one succeeds", self._snapshot.commits)
                        return self._snapshot
                    raise
                self._failures, self._retry_at, self._last_error = 0, 0.0, ""
            self._checked_at = time.time()
            return self._snapshot

    def described(self) -> Snapshot:
        """list_capabilities: the current commits' facts (params, types, defaults, required) with the LLM's prose.

        When the LLM crawl is older than the refs (crawl budget used up, or a crawl failed), its descriptions are laid
        over the current facts instead of serving its stale parameters: a variable removed from the modules must
        disappear from the tools at once, without waiting for (or paying for) a new crawl.
        """
        llm = self.current()
        facts = self.facts()
        if llm.commits == facts.commits:
            return llm
        log.info("Serving facts at %s with descriptions from the crawl of %s", facts.commits, llm.commits)
        return Snapshot(overlay_descriptions(facts.report, llm.report), facts.roots, facts.commits)

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

    def _spend_crawl_budget(self) -> None:
        now = time.time()
        while self._llm_crawls and now - self._llm_crawls[0] > 3600:
            self._llm_crawls.popleft()
        if len(self._llm_crawls) >= self.max_crawls_per_hour:
            raise CrawlUnavailable(f"crawl budget of {self.max_crawls_per_hour} per hour used up")
        self._llm_crawls.append(now)

    def _build(self, use_llm: bool) -> Snapshot:
        self._builds += 1
        target = self.workdir / f"build-{self._builds}"
        try:
            modules = checkout(self.module_repo, self.module_ref, target / "modules")
            score = checkout(self.score_repo, self.score_ref, target / "score") if self.score_repo else None
            roots = {"modules": modules.path} | ({"score": score.path} if score else {})
            if not use_llm:
                report = report_builder.build(modules, score)
                log.info("Read deterministic facts at %s (no LLM)", modules.commit)
            else:
                report = self._reuse(modules, score)
            if report is None:
                self._spend_crawl_budget()
                log.info("Agent crawling %s@%s with Claude", self.module_repo, modules.commit)
                report = self.describe(report_builder.build(modules, score), roots)
        except Exception:
            # Nothing is cached, so the next request crawls again.
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._dirs.append(target)
        while len(self._dirs) > 3:  # keep recent checkouts (LLM and facts) for requests still reading them
            shutil.rmtree(self._dirs.popleft(), ignore_errors=True)
        log.info("Agent found %d capabilities at %s", len(report["capabilities"]), modules.commit)
        return Snapshot(report, roots, (modules.commit, score.commit if score else None))
