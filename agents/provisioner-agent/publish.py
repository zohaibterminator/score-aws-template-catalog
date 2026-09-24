#!/usr/bin/env python3
"""Push a guarded Score provisioner to a review branch in score-gp-aws-rds."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

from harness import check


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def approval_code(provisioner: str) -> str:
    """Return the review code for the exact provisioner content."""
    return hashlib.sha256(provisioner.encode()).hexdigest()[:12]


def publish(target_repo: Path, approval: str) -> str:
    provisioner, policy = check()
    expected_approval = approval_code(provisioner)
    if approval != expected_approval:
        raise ValueError(
            "Human approval does not match the generated provisioner. "
            f"Review it again and approve code {expected_approval}."
        )
    repo = target_repo.resolve()
    if repo.name != policy["publish_repo"] or not repo.is_dir():
        raise ValueError(f"Target must be the {policy['publish_repo']} repository.")
    top = git(repo, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != repo:
        raise ValueError("Target must be the Git repository root.")
    remote = git(repo, "remote", "get-url", "origin")
    if not remote.replace("\\", "/").rstrip("/").removesuffix(".git").endswith("/" + policy["publish_repo"]):
        raise ValueError("Origin does not point to the expected repository.")

    branch = policy["publish_branch_prefix"] + expected_approval
    relative = Path(policy["publish_file"])
    if git(repo, "ls-remote", "--heads", "origin", branch):
        return branch
    with tempfile.TemporaryDirectory(prefix="score-provisioner-") as temp:
        worktree = Path(temp) / "checkout"
        remote = git(repo, "remote", "get-url", "origin")
        clone = subprocess.run(["git", "clone", "--no-checkout", remote, str(worktree)], text=True, capture_output=True)
        if clone.returncode:
            raise RuntimeError(f"git clone failed: {clone.stderr.strip()}")
        has_main = bool(git(worktree, "ls-remote", "--heads", "origin", "main"))
        if has_main:
            git(worktree, "switch", "-c", branch, "origin/main")
        else:
            git(worktree, "switch", "--orphan", branch)
        destination = worktree / relative
        if destination.exists() and not policy["allow_existing_provisioner_replacement"]:
            raise ValueError(f"{relative} already exists on origin/main; review changes manually.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(provisioner, encoding="utf-8", newline="\n")
        git(worktree, "add", "--", relative.as_posix())
        staged = git(worktree, "diff", "--cached", "--name-only")
        if staged.replace("\\", "/") != relative.as_posix():
            raise ValueError("Git staged files outside the reviewed provisioner path.")
        git(worktree, "-c", "user.name=score-provisioner-agent", "-c", "user.email=provisioner@score.local",
            "commit", "-m", "Add guarded application stack Score provisioner")
        git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
    return branch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3] / "score-gp-aws-rds")
    parser.add_argument("--approval", required=True, help="Approval code printed by the preview command")
    args = parser.parse_args()
    try:
        print(f"Pushed review branch: {publish(args.repo, args.approval)}")
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
