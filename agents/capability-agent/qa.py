"""Claude agent that answers other agents' questions, using the crawled capabilities as its tools."""
from __future__ import annotations

import json
from typing import Any, Callable

import skills
from capability_agent import DEFAULT_MODEL, file_tools, run_claude
from store import Snapshot

SYSTEM_PROMPT = """You are the capability agent of an internal developer platform. Other agents ask you what \
infrastructure a Score workload can deploy through Terraform, and whether a specific request will work.

Rules:
- For "what can you do / what capabilities do you have" questions, call list_capabilities and list each \
capability: what it creates, how to request it in Score, and its parameters. Mention rules, limits or caveats \
only when asked; list_capabilities(detail="detailed") has them.
- Get facts only from your tools. For "can I get X" or "will this work" questions, call check_request with the \
Score params and the resource attributes the caller cares about (e.g. multi_az, backup_retention_period).
- Never invent parameters, limits or values. If the tools do not show a limit, say it is not enforced by the \
Terraform module rather than guessing.
- Answer concisely for another agent: the verdict first, then the parameter values to use or what to change, \
then file paths as evidence where relevant."""


def qa_tools(snapshot: Snapshot, evidence: list[dict[str, Any]]) -> list[Callable[..., str]]:
    def list_capabilities(detail: str = "summary") -> str:
        """List every deployable capability.

        Args:
            detail: "summary" for what can be deployed, how to request it and its params; "detailed" to add
                fixed settings, conditional rules and caveats.
        """
        result = skills.list_capabilities(snapshot.report, detail if detail in ("summary", "detailed") else "summary")
        evidence.append({"skill": "list_capabilities", "result": result})
        return json.dumps(result, default=str)

    def check_request(score_type: str | None = None, score_class: str | None = None,
                      capability_id: str | None = None, params: dict[str, Any] | None = None,
                      expect: dict[str, Any] | None = None) -> str:
        """Check whether a Score resource request is valid and what the deployed resource would look like.

        Args:
            score_type: Score resource type, e.g. postgres.
            score_class: Score resource class, e.g. aws-terraform.
            capability_id: Capability id, when there is no Score type.
            params: Score resource params the caller wants to set.
            expect: Resource attributes and the values the caller needs, e.g. {"multi_az": true}.
        """
        request = {"score_type": score_type, "score_class": score_class, "capability_id": capability_id,
                   "params": params or {}, "expect": expect or {}}
        result = skills.check_request(snapshot.report, request)
        evidence.append({"skill": "check_request", "request": request, "result": result})
        return json.dumps(result, default=str)

    return [list_capabilities, check_request, *file_tools(snapshot.roots)]


def answer(snapshot: Snapshot, question: str, model: str = DEFAULT_MODEL) -> tuple[str, list[dict[str, Any]]]:
    """Return Claude's answer and the deterministic tool results it was based on."""
    evidence: list[dict[str, Any]] = []
    final = run_claude(SYSTEM_PROMPT, question, qa_tools(snapshot, evidence), model, max_iterations=15)
    text = "\n".join(block.text for block in final.content if block.type == "text").strip()
    return text, evidence
