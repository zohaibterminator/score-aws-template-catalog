"""Build the Score provisioner from the catalog and application stack contract."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


class BlockDumper(yaml.SafeDumper):
    pass


def represent_string(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


BlockDumper.add_representer(str, represent_string)


def load_catalog() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    catalog = yaml.safe_load((ROOT / "catalog.yaml").read_text(encoding="utf-8"))
    entries = catalog["templates"]
    root_entry = next(item for item in entries if item["name"] == catalog["default_template"])
    root_contract = yaml.safe_load((ROOT / root_entry["contract"]).read_text(encoding="utf-8"))
    children = [item for item in entries if item["composition"] == "child"]
    return catalog, root_contract, children


def build_provisioner() -> str:
    catalog, contract, children = load_catalog()
    input_map = {item["name"]: item for item in contract["inputs"]}
    excluded = {"stack_name", "resource_guid", "workload", "environment", "plane", "region"}
    forbidden = {"allow_prod_destroy", "allow_nonprod_bucket_force_destroy"}
    secret_inputs = {name for name, item in input_map.items() if item["sensitive"]}
    param_names = sorted((set(input_map) - excluded - secret_inputs - forbidden) | {"input_secret_name", "stack_name", "region", "environment", "plane"})

    init = '''stackName: {{ default (printf "%.30s-%s" .SourceWorkload (substr 0 8 .Guid)) (default (index .Params "stack_name") .State.stackName) | quote }}
region: {{ default "us-east-1" (index .Params "region") | quote }}
environment: {{ default "dev" (index .Params "environment") | quote }}
plane: {{ default "application" (index .Params "plane") | quote }}
inputSecretName: {{ default "" (index .Params "input_secret_name") | quote }}
enableRds: {{ if hasKey .Params "enable_rds" }}{{ index .Params "enable_rds" }}{{ else }}true{{ end }}
enableS3: {{ if hasKey .Params "enable_s3" }}{{ index .Params "enable_s3" }}{{ else }}true{{ end }}
enableCache: {{ if hasKey .Params "enable_cache" }}{{ index .Params "enable_cache" }}{{ else }}false{{ end }}
enableSqs: {{ if hasKey .Params "enable_sqs" }}{{ index .Params "enable_sqs" }}{{ else }}false{{ end }}
'''
    state = 'stackName: {{ .Init.stackName | quote }}\n'

    output_lines = ["stack_name: {{ .State.stackName }}"]
    conditions = {
        "db_": ".Init.enableRds", "s3_": ".Init.enableS3",
        "cache_": ".Init.enableCache", "queue_": ".Init.enableSqs",
        "dead_letter_": ".Init.enableSqs",
    }
    for item in contract["outputs"]:
        name = item["name"]
        if name == "stack_name" or item["sensitive"]:
            continue
        condition = next((value for prefix, value in conditions.items() if name.startswith(prefix)), None)
        if condition:
            output_lines.append(f"{{{{ if {condition} }}}}")
        output_lines.append(f'{name}: {{{{ encodeSecretRef (printf "tf-output-%s" .Guid) "{name}" }}}}')
        if condition:
            output_lines.append("{{ end }}")

    lines = [
        '{{ if and (or .Init.enableRds (and (eq .Init.environment "prod") .Init.enableCache)) (eq .Init.inputSecretName "") }}{{ fail "input_secret_name is required for RDS or production cache" }}{{ end }}',
        '- apiVersion: infra.contrib.fluxcd.io/v1alpha2',
        '  kind: Terraform',
        '  metadata:',
        '    name: stack-{{ .Guid }}',
        '    namespace: default',
        '    labels:',
        '      platform.company/plane: {{ .Init.plane | quote }}',
        '    annotations:',
        '      k8s.score.dev/source-workload: {{ .SourceWorkload | quote }}',
        '      k8s.score.dev/resource-uid: {{ .Uid | quote }}',
        '  spec:',
        '    interval: 10m',
        '    approvePlan: ""',
        '    destroyResourcesOnDeletion: true',
        f'    path: {contract["terraform_cr_source_path"]}',
        '    sourceRef:',
        '      kind: GitRepository',
        '      name: score-aws-template-catalog',
        '      namespace: flux-system',
        '    runnerPodTemplate:',
        '      metadata:',
        '        annotations:',
        '          vault.hashicorp.com/agent-inject: "true"',
        '          vault.hashicorp.com/role: tf-runner-role',
        '          vault.hashicorp.com/agent-inject-secret-aws: secret/data/score-api/aws-creds',
        '          vault.hashicorp.com/agent-inject-template-aws: |',
        '            {{"{{"}}- with secret "secret/data/score-api/aws-creds" -{{"}}"}}',
        '            [default]',
        '            aws_access_key_id={{"{{"}} .Data.data.AWS_ACCESS_KEY_ID {{"}}"}}',
        '            aws_secret_access_key={{"{{"}} .Data.data.AWS_SECRET_ACCESS_KEY {{"}}"}}',
        '            {{"{{"}}- end -{{"}}"}}',
        '      spec:',
        '        env:',
        '        - name: AWS_SHARED_CREDENTIALS_FILE',
        '          value: /vault/secrets/aws',
        '        - name: AWS_REGION',
        '          value: {{ .Init.region | quote }}',
        '    vars:',
        '    - name: stack_name',
        '      value: {{ .State.stackName | quote }}',
        '    - name: resource_guid',
        '      value: {{ .Guid | quote }}',
        '    - name: workload',
        '      value: {{ .SourceWorkload | quote }}',
        '    - name: region',
        '      value: {{ .Init.region | quote }}',
        '    - name: environment',
        '      value: {{ .Init.environment | quote }}',
        '    - name: plane',
        '      value: {{ .Init.plane | quote }}',
    ]
    for name in sorted(set(input_map) - excluded - secret_inputs - forbidden):
        kind = input_map[name]["type"]
        value = f'(index .Params "{name}")'
        expression = f"{value} | toJson | quote" if kind.startswith(("list(", "map(")) else f"{value} | quote"
        lines += [f'    {{{{ if hasKey .Params "{name}" }}}}', f'    - name: {name}', f'      value: {{{{ {expression} }}}}', '    {{ end }}']
    lines += [
        '    {{ if or .Init.enableRds (and (eq .Init.environment "prod") .Init.enableCache) }}',
        '    varsFrom:',
        '    - kind: Secret',
        '      name: {{ .Init.inputSecretName | quote }}',
        '      varsKeys:',
        '      {{ if .Init.enableRds }}',
        '      - db_password',
        '      {{ end }}',
        '      {{ if and (eq .Init.environment "prod") .Init.enableCache }}',
        '      - cache_auth_token',
        '      {{ end }}',
        '    {{ end }}',
        '    writeOutputsToSecret:',
        '      name: tf-output-{{ .Guid }}',
        '      outputs:',
    ]
    lines += [f'      - {item["name"]}' for item in contract["outputs"] if not item["sensitive"]]

    provisioner = [{
        "uri": "template://systems-limited/application-stack-aws-tofu-controller",
        "type": contract["score_resource_type"],
        "class": contract["score_class"],
        "description": "Application stack using " + ", ".join(item["name"] for item in children),
        "supported_params": param_names,
        "init": init,
        "state": state,
        "outputs": "\n".join(output_lines) + "\n",
        "manifests": "\n".join(lines) + "\n",
    }]
    return yaml.dump(provisioner, Dumper=BlockDumper, sort_keys=False, width=1000)
