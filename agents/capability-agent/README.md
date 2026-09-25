# Capability agent

A Claude agent that other agents talk to over [A2A](https://a2a-protocol.org). When asked what it can do, it **crawls the Git repositories** to work out which infrastructure a Score workload can deploy through Terraform, then returns the list of capabilities. It can also check whether a specific Score resource request will work before the request is submitted.

Today it crawls `score-tf-modules` (Terraform) and `score-gp-aws-rds` (Score provisioners). It finds one capability: an Amazon RDS PostgreSQL instance, requested as `postgres.aws-terraform`.

## How a request flows

```
Other agent ──A2A──▶ "What capabilities do you have?"
                         │
                         ▼
          Has the Git ref moved since the last crawl?  (git ls-remote, at most every GIT_CHECK_SECONDS)
              │ no                          │ yes
              ▼                             ▼
      reuse cached crawl         clone repos ─▶ Claude crawls them:
                                   list_files, read_file (*.tf, provisioners, READMEs),
                                   get_facts (HCL parser) ─▶ submit_capabilities
                                             │
                                             ▼
                                 grounding check: drop anything not in the repo
                                             │
                                             ▼
Other agent ◀──A2A── capability list (text + JSON), with the Git commit it came from
```

- **Crawl on demand, cached per commit.** The agent re-crawls only when the watched branch or tag points to a new commit, so repeat questions are answered instantly and the LLM runs once per change. A failed crawl is reported to the caller and not cached, so the next request tries again.
- **The agent does the crawling.** It reads the Terraform and provisioner files itself, including comments. The HCL parser is one of its tools, so it can check what it read against exact variables, resources and conditions.
- **Grounded output.** Any capability, variable, attribute or constraint the LLM names that doesn't exist in the repos is dropped. Constraints have to cite a file that exists.

### Claude

The agent runs on Claude through the official `anthropic` Python SDK's Tool Runner (`client.beta.messages.tool_runner`), which handles the tool-call loop. The model is `claude-opus-5` with adaptive thinking (its default), and it can be overridden with `ANTHROPIC_MODEL`. A crawl is capped at 40 tool rounds and a question at 15.

Requests enable server-side refusal fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). If a safety classifier declines, the API re-runs the request on a fallback model instead of stopping. If the whole chain still declines, or a response is cut off at `max_tokens`, the crawl fails and nothing is cached.

## What a calling agent can ask

Discovery: `GET /.well-known/agent-card.json` (no auth). All other calls use JSON-RPC `SendMessage` on `/` with `Authorization: Bearer <token>` and header `A2A-Version: 1.0`.

| Ask | How | Answer |
| --- | --- | --- |
| Anything in plain text, e.g. "What capabilities do you have?", "Can I get Postgres with backups in dev?" | text part | The agent's answer, plus a data part with the capability or check results it used (`evidence`) |
| List capabilities | data part `{"skill": "list_capabilities"}` | A tool manifest: `{provider, discoveryMode: "agent-discovery-live", manifestDigest, tools[]}`. Each capability is a tool (`id` like `infra.aws_terraform.provision_postgres`) whose `inputSchema` is the JSON Schema of the `score.yaml` params it accepts, plus `annotations`. `manifestDigest` is a SHA-256 of `tools` and changes only when a capability changes. `"detail": "summary"`, `"detailed"` or `"full"` return other views. |
| Check a request | data part `{"skill": "check_request", "request": {"score_type": "postgres", "params": {...}, "expect": {"multi_az": true}}}` | `accepted`, `rejected` or `unsupported`, with `issues`, `applied_defaults`, `resolved_attributes` and a ready `score_resource` |

`check_request` evaluates the real Terraform expressions against the crawled capabilities. It rejects a request when:
- a param is unknown, or is owned by the platform, Score or a Secret
- a value has the wrong type
- a Terraform `validation` rule fails
- an expected attribute would come out differently
- the provisioner doesn't set a variable the module requires

It only enforces limits that are written in the repos. For example, the 20 GB free-tier cap is enforced only once `score-tf-modules` has a `validation` block for `storage_gb`.

### Executing tools through score-api

When `SCORE_API_SECRET` is set, the manifest also lists what the agent can execute, and callers run a tool with:

```json
{"skill": "call_tool", "tool": "<tool id>", "arguments": {...}}
```

| Tool id | score-api endpoint | Arguments | Reply |
| --- | --- | --- | --- |
| `infra.aws_terraform.provision_postgres` | `/cgi-bin/score` | `workload`, `image` (required), plus any params from the `inputSchema` | Task. Artifact `score-api-response` has `run_id` and `guid`. |
| `infra.score_api.update_aws_credentials` | `/cgi-bin/update-aws-creds` | `access_key_id`, `secret_access_key`, `region` | Message (not a task, so the credentials are never kept in the task store) |
| `infra.score_api.delete_all_resources` | `/cgi-bin/delete-all` | `confirm: "DELETE-ALL"`; leave it out for a dry run | Task. Takes 5-20 minutes. |

- **Validation before the call:** provision requests go through `check_request` first, and the workload and image formats are checked too. The manifest defaults are then filled in, so the resource matches what the manifest advertised. A rejected request never reaches score-api.
- **Task states:** a task ends `TASK_STATE_COMPLETED` when score-api answers `ok`, `partial` or `dry_run`. It ends `TASK_STATE_FAILED` when score-api answers `error` or can't be reached. The status message says which stage failed.
- **Long calls:** by default `SendMessage` waits for the task to finish. For delete-all, send `"configuration": {"returnImmediately": true}` and poll `GetTask` with `{"id": "<task id>"}`, because the agent's ingress closes requests after 300 s. The agent itself calls score-api through its in-cluster Service, so its own call is not cut off.
- **Plain text:** a plain-text request never executes anything. Claude checks the request and replies with the `call_tool` payload to send.

## Files

| File | Role |
| --- | --- |
| `a2a_server.py` | A2A endpoint: Agent Card, bearer auth, routing messages to the agent or the skills, `/healthz`, `/readyz` |
| `store.py` | Checks Git for new commits, runs the crawl, caches the result per commit |
| `capability_agent.py` | The crawling Claude agent (tools, prompt, `describe_with_llm`, `run_claude`) and a one-shot CLI |
| `qa.py` | The Claude agent that answers free-text questions using `list_capabilities` / `check_request` / file tools |
| `report.py` | Builds the facts and merges the agent's output, dropping anything ungrounded |
| `terraform_facts.py`, `score_bindings.py` | Parsers the agent uses as tools (Terraform via `python-hcl2`, Score provisioners) |
| `skills.py`, `hcl_eval.py` | `list_capabilities`, `check_request`, the tool manifest and provision validation, plus the evaluator for Terraform conditions |
| `score_api.py` | Client for the score-api endpoints (`X-App-Secret` auth) |
| `git_source.py` | Clones at a ref, `ls-remote`, token auth |

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY_FILE` | required | Claude API key |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `A2A_AUTH_TOKEN` or `A2A_AUTH_TOKEN_FILE` | required | Bearer token for callers (`A2A_ALLOW_ANONYMOUS=true` for local testing only) |
| `MODULE_REPO` / `MODULE_REF` | `score-tf-modules` on GitHub / default branch | Terraform repo to crawl. Pin `MODULE_REF` to a reviewed tag in production. |
| `SCORE_REPO` / `SCORE_REF` | unset | Score provisioner repo. Without it, the Score mapping is not reported. |
| `GIT_CHECK_SECONDS` | `60` | How often requests may check Git for a new commit |
| `MANIFEST_PROVIDER` / `MANIFEST_AGENT` | `valueops` / `infra` | Top-level `provider` and each tool's `agent` in the manifest |
| `GIT_TOKEN` or `GIT_TOKEN_FILE`, `GIT_USER`, `GIT_TOKEN_HOST` | unset, `x-access-token`, `https://github.com/` | Private repo access. Sent as an HTTP header through Git's environment config, never on the command line. |
| `SCORE_API_SECRET` or `SCORE_API_SECRET_FILE` | unset | score-api's shared secret, sent as `X-App-Secret`. Without it the agent lists capabilities but cannot execute them. |
| `SCORE_API_URL` | `http://score-api.default.svc.cluster.local` | score-api base URL. Use the in-cluster Service: score-api's ingress times out after 120 s. |
| `SCORE_API_TIMEOUT` / `SCORE_API_INSECURE` | `1500` / unset | Seconds to wait for score-api (delete-all takes up to 20 minutes); `true` skips TLS verification for an `https` URL |
| `PUBLIC_URL`, `PORT`, `CHECKOUT_DIR`, `LOG_LEVEL` | `http://localhost:8080/`, `8080`, temp dir, `INFO` | |

On startup the agent crawls once, so `/readyz` returns 503 until the first crawl finishes. A request that arrives just after a new commit waits for the re-crawl, typically tens of seconds, so callers should allow for that in their timeouts. Secrets are read once at startup; restart the pod after rotating one.

## Run locally

```powershell
python -m pip install -r agents/capability-agent/requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."; $env:A2A_ALLOW_ANONYMOUS = "true"; $env:SCORE_REPO = "..\score-gp-aws-rds"
python agents/capability-agent/a2a_server.py
```

Wait for `Agent found N capabilities at <commit>` in the log. One-shot crawl without the server: `python agents/capability-agent/capability_agent.py --score-repo ..\score-gp-aws-rds --output caps.json`. `--no-llm` shows the parsed facts the agent starts from.

## Deploy to Kubernetes (Rancher / RKE2)

1. **Build and push the image** from this directory (PowerShell or any shell):
   ```sh
   podman build -t docker.io/abdurrahman126/score-capability-agent:0.2.0 .
   podman push docker.io/abdurrahman126/score-capability-agent:0.2.0
   ```
2. **Set up Vault** as described at the top of [`k8s/vault-policy.hcl`](k8s/vault-policy.hcl): a policy, a `capability-agent-role` bound to the `score-capability-agent` service account, and `secret/capability-agent/config` with `a2a_token`, `anthropic_api_key` and `git_token` (a read-only GitHub token for both repos). The policy also reads score-api's `secret/score-api/app-secret`, so the agent can call score-api.
3. **Apply the single manifest** [`k8s/capability-agent.yaml`](k8s/capability-agent.yaml) (ServiceAccount, Deployment, Service, TLS Issuer/Certificate, Ingress). In Rancher: cluster → Import YAML → namespace `default`. Or:
   ```sh
   kubectl apply -f k8s/capability-agent.yaml
   kubectl rollout status deploy/score-capability-agent -n default
   ```
4. **Point the calling agent** at `https://score-capability-agent.apps.ai.cdis.systemsltd.local` and give it the same `a2a_token`. The hostname must resolve to the ingress node `192.168.76.25`, and the certificate is self-signed.

The pod has no RBAC and no cloud credentials. It runs as non-root with a read-only root filesystem and needs outbound access to `github.com`, `api.anthropic.com` and the in-cluster `score-api` Service only. `PUBLIC_URL` is the ingress URL because A2A clients connect to the URL the Agent Card advertises.

## Tests

- `scripts/test-capability-agent.py` covers the parsers, grounding of LLM output, and the tool sandbox.
- `scripts/test-capability-a2a.py` covers crawl on request, caching per commit, failed crawls not being cached, auth, and the skills.
- `scripts/test-capability-actions.py` covers `call_tool` against a fake score-api: validation before the call, provision and delete-all tasks, `returnImmediately` with `GetTask`, and credential handling.

All three run against throwaway Git repos with the LLM stubbed out, so they need no network or API key, and all run from `scripts/validate.sh`. They check the wiring, not the model's judgement. Check that by running locally with a real key.
