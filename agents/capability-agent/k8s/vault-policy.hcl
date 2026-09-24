# Vault policy for the capability agent. Apply with:
#   vault policy write capability-agent k8s/vault-policy.hcl
#   vault write auth/kubernetes/role/capability-agent-role \
#     bound_service_account_names=score-capability-agent bound_service_account_namespaces=default \
#     policies=capability-agent ttl=1h
#   vault kv put secret/capability-agent/config a2a_token=<random> anthropic_api_key=<key> git_token=<read-only GitHub PAT>

path "secret/data/capability-agent/config" {
  capabilities = ["read"]
}
