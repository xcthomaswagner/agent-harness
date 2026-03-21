# Jira Automation Rule Setup

## Prerequisites

- Jira Cloud project (team-managed or company-managed)
- The L1 Pre-Processing Service running and accessible via a public URL
  - Local dev: use `ngrok http 8000` to create a tunnel
  - Production: deploy the service with a stable URL

## Step 1: Start the L1 Service

```bash
cd services/l1_preprocessing
source .venv/bin/activate
uvicorn main:app --port 8000
```

For local development, start a tunnel:
```bash
ngrok http 8000
# Note the https URL (e.g., https://xxxx.ngrok-free.app)
```

Verify: `curl https://xxxx.ngrok-free.app/health` should return `{"status":"ok"}`

## Step 2: Create the Automation Rule

### Navigate to Automation
1. Open your Jira project
2. Go to **Project Settings** (gear icon) → **Automation**
3. Click **Create rule**

### Configure the Trigger
1. Select **"Field value changed"** from the Work item triggers
2. Set:
   - **Fields to monitor:** `Labels`
   - **Change type:** `Value added`
   - **For:** `All work item operations`
3. Click **Next**

### Add the Webhook Action
1. Click **"New component"** → **"THEN: Add an action"**
2. Search for and select **"Send web request"**
3. Configure:
   - **Web request URL:** `https://<your-url>/webhooks/jira`
   - **HTTP method:** `POST`
   - **Web request body:** `Issue data (Jira format)`
   - **Headers:** Leave default (Content-Type: application/json is automatic)
4. Click **Save**

### Name and Enable
1. Click **"Rule details"** (top right)
2. Name: `AI Harness — Webhook on ai-implement label`
3. Click **"Turn on rule"** (green button, top right)

## Step 3: Test

1. Open any ticket in the project
2. Add the label `ai-implement`
3. Within ~30 seconds, the automation fires and the L1 service receives the webhook
4. Check the service logs or ngrok dashboard (`http://localhost:4040`) to verify

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Webhook not firing | Check automation rule is enabled. Check Audit Log in automation settings. |
| 401 from L1 | Webhook signature mismatch. Either set `WEBHOOK_SECRET` in `.env` to match the rule, or leave it empty to skip validation. |
| 422 from L1 | Invalid JSON body. Ensure "Issue data (Jira format)" is selected, not "Automation format". |
| ngrok URL changed | Free ngrok URLs change on restart. Update the automation rule URL, or use a paid ngrok plan with a stable subdomain. |
| Automation says "No executions" | The rule triggers on label *added*, not label *present*. Remove and re-add `ai-implement` to trigger. |

## Environment Variables

Set these in `services/l1_preprocessing/.env`:

```
JIRA_BASE_URL=https://your-instance.atlassian.net
JIRA_API_TOKEN=<your-api-token>
JIRA_USER_EMAIL=<your-atlassian-email>
WEBHOOK_SECRET=                  # Leave empty for dev, set for production
ANTHROPIC_API_KEY=<your-key>     # Required for the ticket analyst
```

## Production Notes

- Replace ngrok with a stable deployment URL (Vercel, Railway, AWS, etc.)
- Set `WEBHOOK_SECRET` and configure the same secret in the Jira automation rule
- Consider using Jira's IP allowlist for additional security
- The automation rule fires for ALL tickets in the project when `ai-implement` is added — no per-ticket filtering needed
