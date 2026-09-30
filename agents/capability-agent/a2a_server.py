#!/usr/bin/env python3
"""A2A server for the capability agent (JSON-RPC, A2A protocol 1.0).

When another agent asks, the LLM agent crawls the Git repositories (cached per commit) and answers.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from a2a.helpers.proto_helpers import (
    get_data_parts, get_message_text, new_data_part, new_message, new_task, new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill, HTTPAuthSecurityScheme,
    SecurityRequirement, SecurityScheme, StringList, TaskState,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from a2a.utils.errors import UnsupportedOperationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import skills
from capability_agent import DEFAULT_MODEL, DEFAULT_MODULE_REPO, describe_with_llm
from score_api import ACCESS_KEY_ID, DELETE_CONFIRMATION, SECRET_ACCESS_KEY, ScoreApi, ScoreApiError
from store import CapabilityStore, Snapshot

log = logging.getLogger("capability-agent")
OPEN_PATHS = {AGENT_CARD_WELL_KNOWN_PATH, "/healthz", "/readyz"}
USAGE = ('Send a data part: {"skill": "list_capabilities"}, {"skill": "check_request", "request": '
         '{"score_type": ..., "params": {...}, "expect": {...}}} or '
         '{"skill": "call_tool", "tool": "<tool id from the manifest>", "arguments": {...}}.')
Answerer = Callable[[Snapshot, str], "tuple[str, list[dict[str, Any]]]"]


def secret(name: str) -> str | None:
    """Read a secret from NAME, or from the file named by NAME_FILE (e.g. a Vault Agent injected file)."""
    if os.environ.get(name):
        return os.environ[name]
    if path := os.environ.get(f"{name}_FILE"):
        return Path(path).read_text(encoding="utf-8").strip()
    return None


def _whole_numbers(value: Any) -> Any:
    # A2A data parts are protobuf Values, so every number arrives as a float.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(v) for v in value]
    return value


def _as_request(value: Any) -> dict[str, Any] | None:
    """A skill request, whether it arrived as a JSON object or as a string holding one."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return _whole_numbers(value) if isinstance(value, dict) and "skill" in value else None


SKILLS = {"list_capabilities", "check_request", "call_tool"}
ARGUMENT_KEYS = ("arguments", "input", "params", "parameters")


def as_tool_call(request: dict[str, Any], agent: str) -> dict[str, Any]:
    """ValueOps invokes a tool by sending its id as the skill: {"skill": "<tool id>", <arguments>}. Rewrite that as
    {"skill": "call_tool", "tool": ..., "arguments": ...}; arguments come from a nested object or the other fields."""
    skill = request.get("skill")
    if skill in SKILLS or not isinstance(skill, str) or not skill.startswith(f"{agent}."):
        return request
    nested = next((request[k] for k in ARGUMENT_KEYS if isinstance(request.get(k), dict)), None)
    arguments = nested if nested is not None else {k: v for k, v in request.items() if k not in ("skill", "tool")}
    return {"skill": "call_tool", "tool": skill, "arguments": arguments}


SECRET_FIELD_HINTS = ("secret", "password", "access_key", "token", "credential")


def masked(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments as received, with anything that looks like a credential replaced by ***, safe to log."""
    return {k: "***" if any(h in k.lower() for h in SECRET_FIELD_HINTS) else v for k, v in arguments.items()}


def structured_requests(message) -> list[dict[str, Any]]:
    """Skill requests in a message. Callers should send a data part, but a data part holding a JSON string, or a
    text part that is entirely a JSON object with "skill", is accepted too, so it is executed instead of being
    ignored as free text."""
    requests = [r for r in map(_as_request, get_data_parts(message.parts)) if r]
    if not requests and (request := _as_request(get_message_text(message).strip())):
        requests = [request]
    return requests


def agent_card(public_url: str, auth: bool, actions: bool = False) -> AgentCard:
    card = AgentCard(
        name="Score capability agent",
        description="An LLM agent that crawls the platform's Git repositories to work out what Score workloads can "
                    "deploy through Terraform, returns those capabilities, and checks Score resource requests "
                    "before they are submitted.",
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=public_url, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="list_capabilities", name="List capabilities",
                description="The agent crawls the Terraform and Score repositories in Git (re-crawling when a new "
                            "commit lands) and returns every deployable capability as a tool manifest "
                            "({provider, discoveryMode, manifestDigest, tools[]}, each tool with id, description, "
                            'inputSchema and annotations). Send {"skill": "list_capabilities"}; "detail": '
                            '"summary" | "detailed" | "full" return other views.',
                tags=["score", "terraform", "catalog"], input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
                examples=["What capabilities do you have?", '{"skill": "list_capabilities"}'],
            ),
            AgentSkill(
                id="check_request", name="Check a Score resource request",
                description='Validates params against the crawled capabilities and resolves the resulting resource '
                            'attributes. Input: {"skill": "check_request", "request": {"score_type", "score_class", '
                            '"params": {...}, "expect": {attribute: value}}}. Returns verdict '
                            "accepted/rejected/unsupported with issues.",
                tags=["score", "validation"], input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
                examples=["Can I get a PostgreSQL database with automated backups in dev?",
                          '{"skill": "check_request", "request": {"score_type": "postgres", '
                          '"params": {"environment": "dev"}, "expect": {"multi_az": true}}}'],
            ),
        ],
    )
    if actions:
        card.skills.append(AgentSkill(
            id="call_tool", name="Call a tool",
            description='Executes a tool from the manifest through score-api. Input: {"skill": "call_tool", '
                        '"tool": "<tool id>", "arguments": {...matching its inputSchema}}. Provisioning and '
                        "delete-all run as tasks (send configuration.returnImmediately and poll GetTask for "
                        "delete-all, which takes 5-20 minutes); requests that fail validation are rejected "
                        "before score-api is called.",
            tags=["score", "provisioning"], input_modes=["application/json"], output_modes=["application/json"],
            # Generic on purpose: the card is public, so it names no real tool; list_capabilities (authenticated) does.
            examples=['{"skill": "call_tool", "tool": "<tool id from list_capabilities>", "arguments": {...}}'],
        ))
    if auth:
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")))
        card.security_requirements.append(SecurityRequirement(schemes={"bearer": StringList()}))
    return card


def _outcome(result: dict[str, Any]) -> str:
    """One line describing a score-api response."""
    status = result["status"]
    if status == "error":
        return f"score-api failed at {result.get('stage', '?')}: {result.get('msg', '')}"
    if status == "dry_run":
        eks = str(result.get("would_delete_eks_workloads") or "").strip()
        net = str(result.get("would_delete_network_access_workloads") or "").strip()
        return (f"Dry run: would delete workloads [{result.get('would_delete_workloads', '')}]"
                + (f" and EKS clusters [{eks}]" if eks else "")
                + (f" and network-access bastions [{net}]" if net else "") + ". "
                f'Send arguments {{"confirm": "{DELETE_CONFIRMATION}"}} to delete them.')
    if "wiped_workloads" in result:
        return _delete_all_outcome(result)
    if str(result.get("terraform_cr", "")).startswith("eks-"):
        line = (f"score-api published {result['terraform_cr']} in namespace {result.get('namespace')}; Flux and "
                "tofu-controller now create or update the VPC, EKS cluster and node group (about 15-20 minutes). "
                f"Outputs will be in Secret {result.get('output_secret')}.")
        if result.get("network_access_cr"):
            line += (f" Network access comes with it: {result['network_access_cr']} starts once the cluster is ready "
                     "and creates the bastion, the load balancer controller, NGINX and the NLB (about 10 more "
                     f"minutes); its outputs (ssm_start_session_command, ssh_command, ingress_load_balancer_hostname) "
                     f"will be in Secret {result.get('network_access_output_secret')}.")
    elif str(result.get("terraform_cr", "")).startswith("network-access-"):
        line = (f"score-api published {result['terraform_cr']} in namespace {result.get('namespace')}; Flux and "
                "tofu-controller now create or update the bastion (a few minutes). The Session Manager command "
                f"will be in Secret {result.get('output_secret')} (key ssm_start_session_command).")
    elif "run_id" in result:
        line = (f"score-api pushed run {result['run_id']} (guid {result.get('guid')}); Flux and tofu-controller "
                "now create or update the RDS instance.")
    else:
        line = result.get("msg") or f"score-api finished: {status}."
        if result.get("reconciled"):
            line += f" Reconciled: {result['reconciled'].strip()}."
    msg = result.get("msg")
    return line + (f" Warning: {msg}" if status == "partial" and msg and msg not in line else "")


def _status_summary(result: dict[str, Any], filters: dict[str, Any]) -> str:
    """One line for a /cgi-bin/status answer: the live state, not a snapshot of an earlier call."""
    if result.get("status") != "ok":
        return f"score-api failed at {result.get('stage', '?')}: {result.get('msg', '')}"
    when = result.get("checked_at", "now")
    scope = ", ".join(f"{k}={v}" for k, v in filters.items()) or "all resources"
    if overall := result.get("overall"):
        return f"{overall.get('summary')} (as of {when}; {scope})"
    items = result.get("resources") or []
    if not items:
        return f"Nothing exists for {scope} as of {when}" + (" (it is gone)." if filters.get("terraform_cr") else ".")
    lines = "; ".join(f"{r['terraform_cr']} ({r.get('workload') or '?'}): {r['state']}"
                      + (f" - {r['message']}" if r["state"] in ("failed", "in_progress") and r.get("message") else "")
                      for r in items)
    return f"{len(items)} resource(s) for {scope} as of {when}: {lines}."


def _delete_all_outcome(result: dict[str, Any]) -> str:
    """Summary of a confirmed delete-all. It describes the moment the call ended: EKS destroys that were
    still running then keep going in the background, so it must not read as the current state."""
    def names(key: str) -> str:
        return ", ".join(str(result.get(key) or "").split())
    parts = []
    if names("wiped_workloads") or names("wiped_eks_workloads") or names("wiped_network_access_workloads"):
        parts.append(f"Wiped RDS workloads [{names('wiped_workloads')}], EKS workloads "
                     f"[{names('wiped_eks_workloads')}] and network-access workloads "
                     f"[{names('wiped_network_access_workloads')}].")
    done = ", ".join(filter(None, (names("destroyed_by_terraform"), names("eks_destroyed_by_terraform"),
                                   names("network_access_destroyed_by_terraform"))))
    parts.append(f"Destroyed by Terraform before this task finished: [{done}]." if done
                 else "Nothing had finished destroying when this task finished.")
    running = ", ".join(filter(None, (names("eks_destroy_in_progress"), names("network_access_destroy_in_progress"))))
    if running:
        parts.append(f"Still being destroyed when this task finished (continues in the background, EKS ~15 minutes): "
                     f"[{running}]. This result is not updated later; check the current "
                     "state with `kubectl get terraform -n default`.")
    if names("left_for_retry"):
        parts.append(f"Stuck and left for retry: [{names('left_for_retry')}].")
    if names("manual_cleanup_required"):
        parts.append(f"May still exist in AWS, check by hand: [{names('manual_cleanup_required')}].")
    return " ".join(parts)


class CapabilityExecutor(AgentExecutor):
    def __init__(self, store: CapabilityStore, manifest_provider: str, manifest_agent: str,
                 score_api: ScoreApi | None = None, manifest_plane: str = "resource"):
        self.store, self.score_api = store, score_api
        self.manifest_provider, self.manifest_agent = manifest_provider, manifest_agent
        self.manifest_plane = manifest_plane

    @staticmethod
    def _parts(text: str, data: Any | None = None) -> list:
        return [new_text_part(text)] + ([new_data_part(data)] if data is not None else [])

    async def handle(self, context: RequestContext) -> list:
        # Claude is used for exactly one thing: describing the capabilities for list_capabilities. Everything else
        # (check_request, free text, unknown skills) is answered by code, without the LLM.
        requests = structured_requests(context.message)
        if not requests:
            return self._parts("Free-text questions are not answered. " + USAGE)
        request = requests[0]
        skill = request.get("skill")
        if skill not in ("list_capabilities", "check_request"):
            return self._parts(f"Unknown skill {skill!r}. {USAGE}")
        try:
            snapshot = await asyncio.to_thread(self.store.described if skill == "list_capabilities" else self.store.facts)
        except Exception as exc:
            log.exception("Reading the repositories failed")
            return self._parts(f"Could not read the repositories: {type(exc).__name__}: {exc}")
        source = snapshot.report["sources"]["terraform_modules"]

        if skill == "list_capabilities":
            manifest = skills.tool_manifest(snapshot.report, self.manifest_provider, self.manifest_agent,
                                            actions=self.score_api is not None, plane=self.manifest_plane)
            detail = request.get("detail", "manifest")
            result = manifest if detail == "manifest" else skills.list_capabilities(snapshot.report, detail)
            lines = "\n".join(f"- {tool['id']}: {tool['description']}" for tool in manifest["tools"])
            header = f"Capabilities in {source['repository']}@{source['commit'][:12]}:"
            return self._parts(f"{header}\n{lines}", result)
        if skill == "check_request":
            result = skills.check_request(snapshot.report, request.get("request") or {})
            issues = "; ".join(f"{i['field']}: {i['problem']}" for i in result["issues"])
            return self._parts(result["verdict"] + (f" ({issues})" if issues else ""), result)

    async def call_tool(self, context: RequestContext, event_queue: EventQueue, request: dict[str, Any]) -> list | None:
        """Run a manifest tool through score-api. Returns reply parts, or None when it ran as a task."""
        if self.score_api is None:
            return self._parts("This agent is not connected to score-api (SCORE_API_SECRET is not set), so it can "
                               "list capabilities but not execute them.")
        tool, arguments = request.get("tool") or "", request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._parts("arguments must be a JSON object.")
        prefix = f"{self.manifest_agent}.score_api."

        if tool == prefix + "update_aws_credentials":
            # Answered directly, not as a task, so the credentials are never kept in the task store.
            akid, secret_key = str(arguments.get("access_key_id", "")), str(arguments.get("secret_access_key", ""))
            region = arguments.get("region", "us-east-1")
            issues = [problem for ok, problem in (
                (ACCESS_KEY_ID.match(akid), "access_key_id does not look like an AWS access key ID"),
                (SECRET_ACCESS_KEY.match(secret_key), "secret_access_key does not look like an AWS secret key"),
                (region in skills.REGIONS, f"region must be one of {skills.REGIONS}"),
            ) if not ok]
            if issues:
                return self._parts("rejected: " + "; ".join(issues), {"verdict": "rejected", "issues": issues})
            try:
                result = await self.score_api.update_aws_creds(akid, secret_key, region)
            except ScoreApiError as exc:
                return self._parts(str(exc))
            return self._parts(_outcome(result), result)

        if tool == prefix + "resource_status":
            # Read-only and quick: answered directly, not as a task.
            keys = ("capability", "workload", "terraform_cr", "guid", "terraform_crs", "operation")
            filters = {k: arguments[k] for k in keys if arguments.get(k)}
            unknown = sorted(set(arguments) - set(keys))
            problems = [f"unknown argument {k!r}" for k in unknown]
            if filters.get("capability") not in (None, *skills.CAPABILITIES):
                problems.append(f"capability must be one of {skills.CAPABILITIES}")
            if problems:
                return self._parts("rejected: " + "; ".join(problems), {"verdict": "rejected", "issues": problems})
            try:
                result = await self.score_api.status(filters)
            except ScoreApiError as exc:
                return self._parts(str(exc))
            return self._parts(_status_summary(result, filters), result)

        if tool == prefix + "delete_all_resources":
            confirm = arguments.get("confirm")
            if confirm not in (None, DELETE_CONFIRMATION):
                return self._parts(f'confirm must be "{DELETE_CONFIRMATION}", or left out for a dry run.')
            summary = ("Deleting every RDS database, EKS cluster and network-access bastion" if confirm
                       else "Checking what delete-all would remove")
            await self._run_task(context, event_queue, summary, self.score_api.delete_all(confirm))
            return None

        # Provisioning is validated and sent by code from the deterministic facts; it never triggers the LLM.
        try:
            snapshot = await asyncio.to_thread(self.store.facts)
        except Exception as exc:
            log.exception("Reading the repositories failed")
            return self._parts(f"Could not read the repositories: {type(exc).__name__}: {exc}")
        plan = skills.prepare_provision(snapshot.report, tool, arguments, self.manifest_agent)
        if plan["verdict"] != "accepted":
            issues = "; ".join(f"{i['field']}: {i['problem']}" for i in plan["issues"])
            return self._parts(f"{plan['verdict']}, nothing was provisioned ({issues})", plan)
        summary = f"Submitting {tool} for workload {plan['workload']} with {plan['params']}"
        log.info(summary)
        if plan["endpoint"] == "score":
            operation = self.score_api.score(plan["workload"], plan["image"], plan["params"])
        else:
            operation = self.score_api.flat(plan["endpoint"], plan["workload"], plan["params"])
        await self._run_task(context, event_queue, summary, operation)
        return None

    async def _run_task(self, context: RequestContext, event_queue: EventQueue, summary: str, operation) -> None:
        """Track a score-api call as an A2A task, so callers can return immediately and poll GetTask."""
        await event_queue.enqueue_event(new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work(updater.new_agent_message([new_text_part(summary)]))
        try:
            result = await operation
        except ScoreApiError as exc:
            log.error("score-api call failed: %s", exc)
            await updater.failed(updater.new_agent_message([new_text_part(str(exc))]))
            return
        await updater.add_artifact([new_data_part(result)], name="score-api-response")
        reply = updater.new_agent_message([new_text_part(_outcome(result))])
        await (updater.failed(reply) if result["status"] == "error" else updater.complete(reply))

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        requests = [as_tool_call(r, self.manifest_agent) for r in structured_requests(context.message)]
        kinds = [p.WhichOneof("content") for p in context.message.parts]
        # Log field names, never values: arguments can carry AWS credentials.
        log.info("Request parts %s -> %s", kinds,
                 f"skill {requests[0].get('skill')} {requests[0].get('tool') or ''} "
                 f"argument fields {sorted(requests[0].get('arguments') or {})}" if requests
                 else "free text (not answered)")
        if requests and requests[0].get("skill") == "call_tool":
            arguments = requests[0].get("arguments")
            log.info("Received %s arguments %s", requests[0].get("tool"),
                     masked(arguments) if isinstance(arguments, dict) else repr(arguments))
            parts = await self.call_tool(context, event_queue, requests[0])
            if parts is None:
                return
        else:
            parts = await self.handle(context)
        await event_queue.enqueue_event(new_message(parts, context_id=context.context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()


class BearerAuth:
    """Require `Authorization: Bearer <token>` on everything except discovery and health endpoints."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in OPEN_PATHS:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(store: CapabilityStore, *, public_url: str, auth_token: str | None,
              warm_up: bool = True, manifest_provider: str = "valueops", manifest_agent: str = "infra",
              score_api: ScoreApi | None = None, manifest_plane: str = "resource") -> Starlette:
    card = agent_card(public_url, auth=bool(auth_token), actions=score_api is not None)
    executor = CapabilityExecutor(store, manifest_provider, manifest_agent, score_api, manifest_plane)
    handler = DefaultRequestHandler(agent_executor=executor,
                                    task_store=InMemoryTaskStore(), agent_card=card)

    async def crawl_until_ready() -> None:
        # Read the deterministic facts at startup (Git only, no LLM) so tool calls are ready at once. The LLM crawl
        # happens only when a list_capabilities request arrives.
        while store.facts_snapshot is None:
            try:
                await asyncio.to_thread(store.facts)
            except Exception as exc:
                log.error("Reading the repositories failed, next attempt in 30s: %s", exc)
                await asyncio.sleep(30)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(crawl_until_ready()) if warm_up else None
        yield
        if task:
            task.cancel()

    async def healthz(request):
        return JSONResponse({"status": "ok"})

    async def readyz(request):
        snapshot = store.snapshot or store.facts_snapshot
        if snapshot is None:
            return JSONResponse({"status": "reading repositories"}, status_code=503)
        return JSONResponse({"status": "ready"})   # no repositories or capabilities on unauthenticated paths

    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/"),
              Route("/healthz", healthz), Route("/readyz", readyz)]
    app = Starlette(routes=routes, lifespan=lifespan)
    if auth_token:
        app.add_middleware(BearerAuth, token=auth_token)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # a2a-sdk 1.1.5 warns after every immediate Message reply because its dispatcher has already finished.
    logging.getLogger("a2a.server.events.event_queue_v2").setLevel(logging.ERROR)
    auth_token = secret("A2A_AUTH_TOKEN")
    if not auth_token and os.environ.get("A2A_ALLOW_ANONYMOUS") != "true":
        raise SystemExit("Set A2A_AUTH_TOKEN (or A2A_AUTH_TOKEN_FILE), or A2A_ALLOW_ANONYMOUS=true for local testing.")
    key = secret("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("Set ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY_FILE): list_capabilities needs Claude.")
    os.environ["ANTHROPIC_API_KEY"] = key
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    store = CapabilityStore(
        module_repo=os.environ.get("MODULE_REPO", DEFAULT_MODULE_REPO),
        module_ref=os.environ.get("MODULE_REF") or None,
        score_repo=os.environ.get("SCORE_REPO") or None,
        score_ref=os.environ.get("SCORE_REF") or None,
        workdir=Path(os.environ.get("CHECKOUT_DIR") or tempfile.mkdtemp(prefix="capability-agent-")),
        describe=lambda report, roots: describe_with_llm(report, roots, model),
        check_interval=float(os.environ.get("GIT_CHECK_SECONDS", "60")),
        max_crawls_per_hour=int(os.environ.get("MAX_CRAWLS_PER_HOUR", "6")),
        cache_dir=Path(os.environ["CRAWL_CACHE_DIR"]) if os.environ.get("CRAWL_CACHE_DIR") else None,
    )
    score_api = None
    if score_api_secret := secret("SCORE_API_SECRET"):
        score_api = ScoreApi(os.environ.get("SCORE_API_URL", "http://score-api.default.svc.cluster.local"),
                             score_api_secret, timeout=float(os.environ.get("SCORE_API_TIMEOUT", "1500")),
                             verify=os.environ.get("SCORE_API_INSECURE") != "true")
        log.info("Tools execute through score-api at %s", score_api.base_url)
    else:
        log.warning("SCORE_API_SECRET is not set: capabilities are listed but call_tool is disabled.")
    port = int(os.environ.get("PORT", "8080"))
    app = build_app(store, public_url=os.environ.get("PUBLIC_URL", f"http://localhost:{port}/"),
                    auth_token=auth_token,
                    manifest_provider=os.environ.get("MANIFEST_PROVIDER", "valueops"),
                    manifest_agent=os.environ.get("MANIFEST_AGENT", "infra"), score_api=score_api,
                    manifest_plane=os.environ.get("MANIFEST_PLANE", "resource"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
