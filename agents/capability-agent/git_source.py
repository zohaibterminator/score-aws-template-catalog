"""Check out a repository at a ref so capabilities reflect committed Git state only."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Checkout:
    source: str
    ref: str
    commit: str
    path: Path

    def describe(self) -> dict[str, str]:
        return {"repository": self.source, "ref": self.ref, "commit": self.commit}


def _auth_env() -> dict[str, str]:
    """Pass GIT_TOKEN (or the file named by GIT_TOKEN_FILE) as an HTTP header via env, never on the command line."""
    token = os.environ.get("GIT_TOKEN")
    if not token and os.environ.get("GIT_TOKEN_FILE"):
        token = Path(os.environ["GIT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        user = os.environ.get("GIT_USER", "x-access-token")
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        host = os.environ.get("GIT_TOKEN_HOST", "https://github.com/")
        env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{host}.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
    return env


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_auth_env(), timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _normalise(source: str) -> str:
    return Path(source).resolve().as_posix() if Path(source).is_dir() else source


def _redact(source: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", source)


def _git_for(source: str, *args: str, cwd: Path | None = None) -> str:
    try:
        return _git(*args, cwd=cwd)
    except RuntimeError as exc:
        hint = " (private repo? set GIT_TOKEN or GIT_TOKEN_FILE)" if "could not read Username" in str(exc) else ""
        raise RuntimeError(f"{_redact(source)}: {exc}{hint}") from None


def remote_commit(source: str, ref: str | None) -> str:
    """Commit a ref currently points to, without cloning. Annotated tags resolve to their commit."""
    if ref and SHA.match(ref):
        return ref
    lines = _git_for(source, "ls-remote", _normalise(source), ref or "HEAD").splitlines()
    peeled = [line.split()[0] for line in lines if line.endswith("^{}")]
    exact = [line.split()[0] for line in lines if line.split()[1] in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}", "HEAD")]
    found = peeled or exact or [line.split()[0] for line in lines]
    if not found:
        raise RuntimeError(f"ref {ref!r} not found in {source}")
    return found[0]


def checkout(source: str, ref: str | None, workdir: Path) -> Checkout:
    """Clone `source` (URL or local repo path) into `workdir` and check out `ref`."""
    source = _normalise(source)
    dest = workdir / Path(source.rstrip("/")).stem
    _git_for(source, "clone", "--quiet", "--no-checkout", source, str(dest))
    target = ref or "HEAD"
    # --detach so tags, branches (via origin/) and SHAs all resolve the same way.
    try:
        _git("checkout", "--quiet", "--detach", target, cwd=dest)
    except RuntimeError:
        _git("checkout", "--quiet", "--detach", f"origin/{target}", cwd=dest)
    return Checkout(
        source=source,
        ref=ref or _git("rev-parse", "--abbrev-ref", "origin/HEAD", cwd=dest).removeprefix("origin/"),
        commit=_git("rev-parse", "HEAD", cwd=dest),
        path=dest,
    )
