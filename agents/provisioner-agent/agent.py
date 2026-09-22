#!/usr/bin/env python3
"""LangChain wrapper for the application-stack provisioner renderer."""
from __future__ import annotations

from pathlib import Path
import sys

from renderer import load_request, render_manifests


SYSTEM_PROMPT = """You are a provisioner agent for score-aws-template-catalog.
Render Kubernetes YAML for application-stack.aws by calling the provided tool.
Do not invent AWS credentials, run Terraform, run kubectl, plan, apply, or deploy.
Return only the rendered YAML plus short validation notes."""


def run_agent(request_path: Path) -> str:
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

    @tool
    def render_application_stack_manifests() -> str:
        """Render the Secret and Terraform CR for the request YAML."""
        return render_manifests(request)

    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "Render the provisioner manifests for this request: {request}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    tools = [render_application_stack_manifests]
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=False)
    result = executor.invoke({"request": request})
    return str(result["output"])


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: agent.py <request.yaml>", file=sys.stderr)
        return 2
    print(run_agent(Path(sys.argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
