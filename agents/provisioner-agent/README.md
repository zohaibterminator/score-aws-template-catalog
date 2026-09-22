# LangChain Provisioner Agent

This agent uses the `application-stack` root to provision any combination of `network`, `postgres`, `object-storage`, `cache`, and `queue`. The root keeps them in one Terraform state. The agent renders a tofu-controller Terraform manifest and can commit and push it to `score-api/.score-k8s/provisioners/`.

Publishing is a live GitOps request if Flux watches that path. It can create AWS resources.

## Files

| File | Use |
| --- | --- |
| `agent.py` | LangChain tool-calling wrapper. |
| `renderer.py` | Deterministic manifest renderer and validation. |
| `publish.py` | Commits and pushes one manifest to `score-api`. |
| `example-request.yaml` | Small request you can edit for testing. |

## Preview

From the catalog root in PowerShell:

```powershell
python agents/provisioner-agent/renderer.py agents/provisioner-agent/example-request.yaml
```

## Publish with LangChain

Edit the request first. Set `components` to the child templates needed. Use a stable Score GUID and resource UID. If RDS is enabled, create the named Kubernetes Secret with a `db_password` key before publishing. Production cache also needs `cache_auth_token`. The Secret stays in the cluster; the request and Git contain only its name.

```powershell
python -m pip install -r requirements-agent.txt
$env:OPENAI_API_KEY = "YOUR_NEW_API_KEY"
python agents/provisioner-agent/agent.py path/to/request.yaml --publish
```

The agent validates the request, calls the publisher tool, stages only `.score-k8s/provisioners/stack-<guid>.yaml`, commits it on `score-api/main`, and pushes to `origin/main`. The example GUID is for preview only. Git authentication and the `score-aws-template-catalog` Flux GitRepository must already work. Flux must watch the destination path for deployment.

Without `--publish`, the LangChain agent previews the same manifest. If the push fails, the commit remains local in `score-api` and the command reports the Git error.
