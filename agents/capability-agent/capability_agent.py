#!/usr/bin/env python3
"""Claude agent that crawls Git repositories to find what Score workloads can deploy through Terraform."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any, Callable

from pydantic import BaseModel, Field

import report as report_builder
from git_source import checkout

DEFAULT_MODULE_REPO = "https://github.com/zohaibterminator/score-tf-modules.git"
DEFAULT_MODEL = "claude-opus-5"
MAX_FILE_BYTES = 60_000


class ParameterNote(BaseModel):
    terraform_variable: str
    effect: str = Field(description="What changes in the deployed infrastructure when this is set")


class BehaviourNote(BaseModel):
    resource: str = Field(description="Resource address exactly as in the facts, e.g. aws_db_instance.this")
    attribute: str
    description: str = Field(description="Plain-language rule, e.g. 'Multi-AZ only when environment is prod'")


class Constraint(BaseModel):
    text: str
    evidence: str = Field(description="'modules:<path>' or 'score:<path>' of the file that shows this")


class CapabilityDraft(BaseModel):
    id: str = Field(description="Capability id exactly as in the facts")
    summary: str = Field(description="One sentence: what a Score workload can get from this capability")
    aws_service: str
    parameters: list[ParameterNote]
    behaviours: list[BehaviourNote]
    constraints: list[Constraint]


SYSTEM_PROMPT = """You catalogue what a platform can deploy. Terraform modules are run by tofu-controller \
when a Score workload requests a resource. You have read-only tools over Git checkouts of the repositories.

Crawl the repositories before answering:
1. list_files for every repository.
2. read_file every Terraform file (*.tf) and every Score provisioner (.score-k8s/*.provisioners.yaml), \
plus READMEs that explain them. Pay attention to comments: they often state intent and policy.
3. get_facts to check your reading against the parsed variables, resources, conditions and Score bindings.
Then call submit_capabilities exactly once, describing every capability id from the facts:
- summary and aws_service in plain language;
- an effect for each parameter a developer or platform can influence;
- a description for every conditional behaviour listed in the facts;
- constraints and caveats (limits, security posture, deletion or backup policy), each with the file that shows it.
Use only ids, variables, resources and attributes that appear in the facts. Do not guess values that are not in the files."""


def file_tools(roots: dict[str, Path]) -> list[Callable[..., str]]:
    """Read-only, sandboxed access to the Git checkouts."""

    def resolve(repo: str, path: str = "") -> Path:
        if repo not in roots:
            raise ValueError(f"repo must be one of {sorted(roots)}")
        root = roots[repo].resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
            raise ValueError("path is outside the repository")
        return target

    def list_files(repo: str) -> str:
        """List every file in a repository checkout.

        Args:
            repo: Which repository: 'modules' (Terraform) or 'score' (Score provisioners).
        """
        root = resolve(repo)
        return "\n".join(
            p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
            if p.is_file() and not {".git", ".terraform"}.intersection(p.relative_to(root).parts)
        )

    def read_file(repo: str, path: str) -> str:
        """Read a text file from a repository checkout.

        Args:
            repo: Which repository: 'modules' (Terraform) or 'score' (Score provisioners).
            path: File path relative to the repository root, as printed by list_files.
        """
        target = resolve(repo, path)
        if not target.is_file():
            return f"No such file: {path}"
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]

    return [list_files, read_file]


def crawl_tools(roots: dict[str, Path], report: dict[str, Any], captured: dict[str, Any]) -> list[Callable[..., str]]:
    def get_facts() -> str:
        """Return the parsed facts for every capability: Terraform variables, resources, conditions and Score bindings."""
        return json.dumps(report["capabilities"], default=str)

    def submit_capabilities(capabilities: list[CapabilityDraft]) -> str:
        """Submit the final capability descriptions. Call exactly once, after crawling.

        Args:
            capabilities: One entry per capability id in the facts.
        """
        captured["draft"] = {"capabilities": [c.model_dump() for c in capabilities]}
        return "Submitted."

    return [*file_tools(roots), get_facts, submit_capabilities]


def run_claude(system: str, prompt: str, tools: list[Callable[..., str]], model: str, max_iterations: int):
    """Run Claude's tool loop to completion and return its final message."""
    import anthropic
    from anthropic import beta_tool

    runner = anthropic.Anthropic().beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=system,
        tools=[beta_tool(fn) for fn in tools],
        messages=[{"role": "user", "content": prompt}],
        max_iterations=max_iterations,
        # If a safety classifier declines, the API retries on a fallback model instead of stopping.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    final = None
    for message in runner:
        final = message
    if final is None:
        raise RuntimeError("Claude returned no response.")
    if final.stop_reason == "refusal":
        category = final.stop_details.category if final.stop_details else None
        raise RuntimeError(f"Claude declined the request (category: {category}).")
    if final.stop_reason == "max_tokens":
        raise RuntimeError("Claude's response hit max_tokens before finishing.")
    return final


def describe_with_llm(report: dict[str, Any], roots: dict[str, Path], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    run_claude(
        SYSTEM_PROMPT,
        f"Repositories available: {', '.join(sorted(roots))}. "
        f"Capability ids: {', '.join(c['id'] for c in report['capabilities'])}. Crawl them and submit the capabilities.",
        crawl_tools(roots, report, captured),
        model,
        max_iterations=40,
    )
    if "draft" not in captured:
        raise RuntimeError("Claude did not submit capabilities.")
    return report_builder.merge_llm(report, captured["draft"], roots, model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-repo", default=DEFAULT_MODULE_REPO, help="Git URL or local repo path of Terraform modules")
    parser.add_argument("--ref", help="Branch, tag or commit of the module repo (default: its default branch)")
    parser.add_argument("--score-repo", help="Optional Git URL or local path of a Score workload repo with .score-k8s provisioners")
    parser.add_argument("--score-ref", help="Branch, tag or commit of the Score repo")
    parser.add_argument("--no-llm", action="store_true", help="Return the parsed facts only, without calling Claude")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, help="Write the JSON report here instead of stdout")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="capability-agent-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        modules = checkout(args.module_repo, args.ref, workdir / "modules")
        score = checkout(args.score_repo, args.score_ref, workdir / "score") if args.score_repo else None
        result = report_builder.build(modules, score)
        if not args.no_llm:
            roots = {"modules": modules.path} | ({"score": score.path} if score else {})
            result = describe_with_llm(result, roots, args.model)

    text = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
