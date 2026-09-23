#!/usr/bin/env python3
"""LangChain wrapper around the guarded Score provisioner workflow."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from harness import check
from publish import approval_code, publish


def run_agent(repo: Path, do_publish: bool = False, approval: str | None = None) -> str:
    rendered, _ = check()  # Deterministic gate runs before the model or Git.
    code = approval_code(rendered)
    if do_publish and approval != code:
        raise ValueError(
            "Human approval is required for the exact provisioner. "
            f"Review the preview and rerun with --publish --approval {code}."
        )
    try:
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise SystemExit("Install the dependencies with: python -m pip install -r requirements-agent.txt") from exc

    called = False

    @tool(return_direct=True)
    def preview_score_provisioner() -> str:
        """Preview the contract-checked Score application-stack provisioner YAML."""
        nonlocal called
        called = True
        return f"Approval code: {code}\n\n{rendered}"

    @tool(return_direct=True)
    def publish_score_provisioner() -> str:
        """Push the guarded Score provisioner to a review branch in application-stack."""
        nonlocal called
        called = True
        return f"Pushed review branch: {publish(repo, approval or '')}"

    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Call the one provided tool exactly once. Do not invent manifests, run shell commands, or access AWS or Kubernetes. Return the tool result."),
        ("human", "{action} the Score application-stack provisioner."),
        ("placeholder", "{agent_scratchpad}"),
    ])
    tool = publish_score_provisioner if do_publish else preview_score_provisioner
    executor = AgentExecutor(agent=create_tool_calling_agent(llm, [tool], prompt), tools=[tool], verbose=False)
    result = executor.invoke({"action": "Publish to a review branch" if do_publish else "Preview"})
    if not called:
        raise RuntimeError("The model did not call the guarded provisioner tool.")
    return str(result["output"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--approval", help="Approval code printed by the preview command")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3] / "application-stack")
    args = parser.parse_args()
    try:
        print(run_agent(args.repo, args.publish, args.approval))
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
