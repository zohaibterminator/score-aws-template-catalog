# Guarded Score provisioner agent

This agent builds one Score provisioner for `application-stack.aws`. That root covers network, PostgreSQL, S3, cache, and queue. The agent reads `catalog.yaml`, all six contracts, and the policy blocks in `docs/SECURITY.md`, `docs/COMPATIBILITY.md`, and `docs/LIFECYCLE.md`.

From the catalog root in PowerShell:

```powershell
python -m pip install -r requirements-agent.txt
python agents/provisioner-agent/harness.py --print
```

That previews the provisioner without an API key. To use LangChain:

```powershell
$env:OPENAI_API_KEY = "YOUR_NEW_API_KEY"
python agents/provisioner-agent/agent.py
python agents/provisioner-agent/agent.py --publish
```

`--publish` pushes `10-application-stack.provisioners.yaml` to a new `agent/application-stack-*` review branch in `score-gp-aws-rds/.score-k8s`. It does not change `main`, `.score-k8s/state.yaml`, or existing provisioners. The model only chooses the preview or publish tool; it cannot edit the rendered YAML or run shell commands. The harness runs before either tool.

The provisioner references an existing Kubernetes Secret for RDS and production cache inputs. It does not put secret values in Git. The generated Terraform CR has an empty `approvePlan`, so a reviewed plan name is required before AWS changes apply. See [`docs/PROVISIONER_CONTRACT.md`](../../docs/PROVISIONER_CONTRACT.md) for the handoff.

The `renderer.py` and `example-request.yaml` remain a local per-resource CR preview. They do not publish and are separate from the Score provisioner definition.
