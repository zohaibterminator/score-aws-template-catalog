#!/usr/bin/env python3
"""Commit and push one generated Terraform CR into score-api/.score-k8s."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from renderer import load_request, render_manifests


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def publish(request_path: Path, score_api_repo: Path) -> Path:
    request = load_request(request_path)
    manifest = render_manifests(request)
    if request["resource_guid"] == "00000000-0000-4000-8000-000000000001":
        raise ValueError("Replace the example resource_guid before publishing.")
    repo = score_api_repo.resolve()
    if not repo.is_dir() or git(repo, "rev-parse", "--show-toplevel").casefold() != str(repo).replace("\\", "/").casefold():
        raise ValueError("score_api_repo must be the root of a Git repository.")
    if git(repo, "branch", "--show-current") != "main":
        raise ValueError("score-api must be on main before publishing.")
    if git(repo, "diff", "--cached", "--name-only"):
        raise ValueError("score-api has staged changes; publish them separately first.")
    if not git(repo, "remote", "get-url", "origin"):
        raise ValueError("score-api has no origin remote.")

    relative = Path(".score-k8s") / "provisioners" / f"stack-{request['resource_guid']}.yaml"
    target = repo / relative
    if git(repo, "status", "--porcelain", "--", relative.as_posix()):
        raise ValueError(f"{target} already has local changes; review them before publishing.")
    if target.exists() and target.read_text(encoding="utf-8") == manifest:
        git(repo, "push", "origin", "HEAD:main")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifest, encoding="utf-8", newline="\n")
    git(repo, "add", "--", relative.as_posix())
    git(repo, "-c", "user.name=score-provisioner-agent", "-c", "user.email=provisioner@score.local",
        "commit", "-m", f"Add application stack {request['resource_guid']}", "--", relative.as_posix())
    git(repo, "push", "origin", "HEAD:main")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--score-api-repo", type=Path, default=Path(__file__).resolve().parents[3] / "score-api")
    args = parser.parse_args()
    try:
        print(publish(args.request, args.score_api_repo))
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
