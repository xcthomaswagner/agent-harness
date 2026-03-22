# Secrets Management

## Current State (Development)

Secrets are stored in `.env` files per service:
- `services/l1_preprocessing/.env`
- `services/l3_pr_review/.env`

These files are gitignored and never committed.

## Production: HashiCorp Vault

### Setup

```bash
# Install Vault
brew install vault

# Start dev server (for testing)
vault server -dev

# Set address
export VAULT_ADDR='http://127.0.0.1:8200'
```

### Storing Secrets

```bash
# Per-client secrets
vault kv put secret/clients/acme \
  jira_api_token="..." \
  jira_user_email="..." \
  github_token="..." \
  anthropic_api_key="..." \
  figma_api_token="..."

vault kv put secret/clients/contoso \
  jira_api_token="..." \
  ...
```

### Integration with Harness

Add to `config.py`:
```python
# If VAULT_ADDR is set, load secrets from Vault
vault_addr: str = ""
vault_token: str = ""
vault_path: str = ""  # e.g., "secret/clients/acme"
```

At startup, the service loads secrets from Vault if configured, falling back to `.env`.

### Per-Client Isolation

Each client profile references a vault path:
```yaml
# runtime/client-profiles/acme.yaml
credentials:
  vault_path: "secret/clients/acme"
```

Each Agent Team session receives only its client's credentials. No cross-client leakage because:
1. The spawn script reads credentials from Vault at session start
2. Credentials are passed as environment variables to the Claude Code process
3. Credentials are never written to disk in worktrees

### Rotation

- Jira API tokens: rotate every 90 days
- GitHub PATs: rotate every 90 days, use fine-grained tokens with minimal scopes
- Anthropic API keys: rotate on personnel changes
- Set up Vault audit logging to track credential access

## Alternative: AWS Secrets Manager

For AWS-hosted deployments:

```python
import boto3

client = boto3.client('secretsmanager')
response = client.get_secret_value(SecretId='harness/clients/acme')
secrets = json.loads(response['SecretString'])
```

Same per-client isolation pattern applies.
