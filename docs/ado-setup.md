# Azure DevOps Integration Setup

Connect an ADO project to the harness so that work items with the `ai-implement`
tag trigger the L1 pipeline and produce PRs on GitHub.

## Prerequisites

1. **ADO Personal Access Token (PAT)** with scopes:
   - Work Items: Read & Write (for status transitions, comments)
   - Service Hooks: Read & Write (for creating webhook subscriptions)
   - Project and Team: Read (for resolving project IDs)

2. **GitHub repo** — the target repo where agent PRs are created (same as any
   other harness client profile)

3. **ngrok tunnel** (or public URL) pointing to the L1 service on port 8000

## 1. Create the Client Profile

Copy from the schema template or an existing ADO profile:

```bash
cp runtime/client-profiles/schema.yaml runtime/client-profiles/<name>.yaml
```

Key fields for ADO:

```yaml
ticket_source:
  type: "ado"
  instance: "https://xc-devops.visualstudio.com"  # Your ADO org URL
  project_key: "XCSF30"                            # Short key used in ticket IDs (e.g. XCSF30-123)
  ado_project_name: "XC-SF-30in30"                  # Exact ADO project name (case-insensitive match)
  ai_label: "ai-implement"                          # ADO tag that triggers the pipeline
  done_status: "Done"                               # Target state on completion
```

The `project_key` is your chosen prefix for normalized ticket IDs. It does not
need to match the ADO project name -- the harness maps between them using
`ado_project_name`.

## 2. Set Environment Variables

Add to your `.env` file:

```bash
ADO_ORG_URL=https://xc-devops.visualstudio.com
ADO_PAT=<your-pat>
ADO_WEBHOOK_TOKEN=<generate-a-random-secret>
```

The `ADO_WEBHOOK_TOKEN` is a shared secret sent via the `X-ADO-Webhook-Token`
header. Generate one with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 3. Create Service Hook Subscriptions

### Option A: Script (recommended)

```bash
python scripts/setup-ado-webhook.py \
    --org-url https://xc-devops.visualstudio.com \
    --project "XC-SF-30in30" \
    --target-url https://<ngrok-subdomain>.ngrok.io/webhooks/ado \
    --pat <your-pat> \
    --webhook-token <your-ado-webhook-token>
```

Add `--dry-run` to preview the API request without sending it.

### Option B: Manual (ADO UI)

```bash
python scripts/setup-ado-webhook.py --manual \
    --org-url https://xc-devops.visualstudio.com \
    --project "XC-SF-30in30" \
    --webhook-token <your-ado-webhook-token>
```

This prints step-by-step instructions for creating the subscriptions in the ADO
Project Settings UI.

## 4. Test the Integration

1. Start the L1 service: `cd services/l1_preprocessing && python -m uvicorn main:app --port 8000`
2. Start the ngrok tunnel: `ngrok http 8000`
3. In ADO, create or update a work item:
   - Add the `ai-implement` tag
   - Verify the webhook fires (check ngrok inspector at `http://127.0.0.1:4040`)
   - Check L1 logs for `ado_webhook_received`

### Verifying ticket ID remapping

If your profile has `project_key: XCSF30` and `ado_project_name: XC-SF-30in30`,
a work item with ID 123 in the `XC-SF-30in30` project will be normalized to
ticket ID `XCSF30-123`.

## 5. Troubleshooting

### Webhook returns 401

- Check that `ADO_WEBHOOK_TOKEN` in `.env` matches the token in the Service Hook
  `httpHeaders` field (format: `X-ADO-Webhook-Token:<token>`)
- If using HMAC: ensure `WEBHOOK_SECRET` matches and the signature header is
  `x-hub-signature` (not all ADO configurations send this)

### Webhook returns `{"status": "skipped", "reason": "Tag 'ai-implement' not found"}`

- The work item does not have the `ai-implement` tag. ADO tags are
  semicolon-separated in `System.Tags`. Add the tag and re-save.

### Profile not found (ticket ID not remapped)

- Verify `ado_project_name` in the profile matches the `System.TeamProject`
  field in the webhook payload (case-insensitive)
- Check that `ticket_source.type` is `"ado"` (not `"jira"`)

### Write-back fails (comments, status transitions)

- Verify `ADO_PAT` has Work Items Read & Write scope
- The PAT user must have permissions on the target project
- ADO work item state transitions must follow the board's allowed transitions
  (e.g. you can't go from "New" directly to "Done" if the board requires
  intermediate states)
