# LangChain Provisioner Agent

This agent turns an `application-stack.aws` request into the two Kubernetes manifests expected by tofu-controller:

- `secret-<guid>` with sensitive Terraform inputs
- `stack-<guid>` Terraform CR pointing at `./templates/application-stack`

It only renders YAML. It does not call AWS, Kubernetes, Flux, Terraform, OpenTofu, plan, or apply.

## Files

| File | Use |
| --- | --- |
| `agent.py` | LangChain tool-calling wrapper. |
| `renderer.py` | Deterministic manifest renderer and validation. |
| `example-request.yaml` | Small request you can edit for testing. |

## Run Without LLM

```bash
python agents/provisioner-agent/renderer.py agents/provisioner-agent/example-request.yaml
```

## Run With LangChain

From the repository root in PowerShell:

```powershell
python -m pip install -r requirements-agent.txt
$env:OPENAI_API_KEY = "YOUR_NEW_API_KEY"
python agents/provisioner-agent/agent.py agents/provisioner-agent/example-request.yaml
```

The environment variable lasts for this PowerShell session. Use a newly created key if an earlier key was exposed.

The LangChain layer reads the same request and uses a local tool named `render_application_stack_manifests`. The renderer is still the source of truth, so output stays predictable.
