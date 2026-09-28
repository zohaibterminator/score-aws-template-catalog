#!/usr/bin/env python3
"""Claude agent that crawls Git repositories to find what Score workloads can deploy through Terraform."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import tempfile
from typing import Annotated, Any, Callable

from pydantic import BaseModel, BeforeValidator, Field

import report as report_builder
from git_source import checkout

log = logging.getLogger(__name__)
DEFAULT_MODULE_REPO = "https://github.com/zohaibterminator/score-tf-modules.git"
DEFAULT_MODEL = "claude-opus-5"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
MAX_FILE_BYTES = 60_000
# score-k8s's generic built-in provisioners (added by `score-k8s init`). They describe no platform capability
# and are large (~28k tokens between two projects), so the crawl never reads them.
SKIPPED_FILES = ("zz-default.provisioners.yaml",)


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


def _decode_capabilities(value: Any) -> Any:
    # Some models send the list as a JSON string, sometimes wrapped in {"capabilities": [...]}.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, dict) and "capabilities" in value:
        value = value["capabilities"]
    return value


Capabilities = Annotated[list[CapabilityDraft], BeforeValidator(_decode_capabilities)]


SYSTEM_PROMPT = """You catalogue what a platform can deploy. Terraform modules are run by tofu-controller \
when a Score workload requests a resource. You have read-only tools over Git checkouts of the repositories.

Crawl the repositories before answering:
1. list_files for every repository.
2. read_file every Terraform file (*.tf) and every Score provisioner (.score-k8s/*.provisioners.yaml), \
plus READMEs that explain them. Pay attention to comments: they often state intent and policy. Read each file once;
skip score-k8s's built-in zz-default.provisioners.yaml files, which are not platform capabilities.
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
            and p.name not in SKIPPED_FILES
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
        if target.name in SKIPPED_FILES:
            return f"Skipped: {path} holds score-k8s's built-in default provisioners, not platform capabilities."
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]

    return [list_files, read_file]


def crawl_tools(roots: dict[str, Path], report: dict[str, Any], captured: dict[str, Any]) -> list[Callable[..., str]]:
    def get_facts() -> str:
        """Return the parsed facts for every capability: Terraform variables, resources, conditions and Score bindings."""
        return json.dumps(report["capabilities"], default=str)

    def submit_capabilities(capabilities: Capabilities) -> str:
        """Submit the final capability descriptions. Call exactly once, after crawling.

        Args:
            capabilities: One entry per capability id in the facts.
        """
        captured["draft"] = {"capabilities": [c.model_dump() for c in capabilities]}
        return "Submitted."

    return [*file_tools(roots), get_facts, submit_capabilities]


def run_claude(system: str, prompt: str, tools: list[Callable[..., str]], model: str, max_iterations: int,
               label: str = "claude"):
    """Run Claude's tool loop to completion and return its final message. Logs the tokens it used."""
    import anthropic
    from anthropic import beta_tool

    # If a safety classifier declines, the API retries on a fallback model instead of stopping.
    # Only the models with those classifiers take the parameter; lighter models are sent without it.
    fallback = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                if model.startswith(FALLBACK_MODELS) else {})
    runner = anthropic.Anthropic().beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=system,
        tools=[beta_tool(fn) for fn in tools],
        messages=[{"role": "user", "content": prompt}],
        max_iterations=max_iterations,
        # Automatic prompt caching: each round re-sends the whole conversation (every file already read),
        # so the cache point moves forward and earlier rounds are billed at the cache-read rate.
        cache_control={"type": "ephemeral"},
        **fallback,
    )
    final, rounds = None, 0
    used = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
    try:
        for message in runner:
            final, rounds = message, rounds + 1
            for key in used:
                used[key] += getattr(message.usage, key, None) or 0
    finally:
        log.info("Claude %s (%s): %d rounds, input %d, cache read %d, cache write %d, output %d tokens",
                 label, model, rounds, used["input_tokens"], used["cache_read_input_tokens"],
                 used["cache_creation_input_tokens"], used["output_tokens"])
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
        label="crawl",
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
