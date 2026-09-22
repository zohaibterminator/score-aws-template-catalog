#!/usr/bin/env python3
"""LangChain agent for previewing or publishing application-stack manifests."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from renderer import load_request, render_manifests
from publish import publish


SYSTEM_PROMPT = """You are a provisioner agent for score-aws-template-catalog.
Use the provided tool exactly once for the requested action. The deterministic renderer
selects child templates and owns validation. Never invent credentials or change YAML.
Return the tool result."""


def run_agent(request_path: Path, score_api_repo: Path, do_publish: bool = False) -> str:
    try:
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise SystemExit(
            "LangChain dependencies are missing. Install them with: "
            "pip install -r requirements-agent.txt"
        ) from exc

    request = load_request(request_path)
    render_manifests(request)  # Validate locally before any model call or Git write.
    called = False

    @tool(return_direct=True)
    def render_application_stack_manifests() -> str:
        """Preview the Terraform CR for the selected application-stack components."""
        nonlocal called
        called = True
        return render_manifests(request)

    @tool(return_direct=True)
    def publish_application_stack_manifests() -> str:
        """Commit and push the Terraform CR to score-api/.score-k8s/provisioners."""
        nonlocal called
        called = True
        return f"Published {publish(request_path, score_api_repo)}"

    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "{action} the application-stack provisioner for these components: {components}."),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    tools = [publish_application_stack_manifests] if do_publish else [render_application_stack_manifests]
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=False)
    result = executor.invoke({"action": "Publish" if do_publish else "Preview", "components": request.get("components", "request flags")})
    if not called:
        raise RuntimeError("The model did not call the provisioner tool.")
    return str(result["output"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--score-api-repo", type=Path, default=Path(__file__).resolve().parents[3] / "score-api")
    args = parser.parse_args()
    try:
        print(run_agent(args.request, args.score_api_repo, args.publish))
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
