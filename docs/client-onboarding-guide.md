# Client Onboarding Guide

## Overview

This guide walks through setting up the Agentic Developer Harness for a new client project. The harness transforms Jira tickets into reviewed, tested, merge-ready Pull Requests.

## Prerequisites

- A git repository on GitHub (the client's codebase)
- A Jira Cloud project
- Claude Max subscription (flat-rate unlimited)
- Python 3.12+ on the machine running the harness

## Step 1: Clone the Harness

```bash
git clone git@github.com:xcthomaswagner/agent-harness.git
cd agent-harness
```

## Step 2: Install Dependencies

### L1 Service (Python)
```bash
cd services/l1_preprocessing
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Claude Code CLI
Ensure `claude` is available on the system PATH.

### GitHub CLI
```bash
gh auth login
# Select: GitHub.com → HTTPS → Paste token
# Token needs scopes: repo, read:org
```

## Step 3: Configure Environment

Copy the example and fill in your credentials:

```bash
cp services/l1_preprocessing/.env.example services/l1_preprocessing/.env
```

Edit `.env`:

| Variable | Description | Where to get it |
|----------|-------------|----------------|
| `ANTHROPIC_API_KEY` | Claude API key | console.anthropic.com → API Keys |
| `JIRA_BASE_URL` | Your Jira instance URL | e.g., `https://yourcompany.atlassian.net` |
| `JIRA_API_TOKEN` | Jira API token | id.atlassian.com → Security → API tokens |
| `JIRA_USER_EMAIL` | Your Atlassian email | The email tied to your Jira account |
| `GITHUB_TOKEN` | GitHub PAT with `repo` + `read:org` | github.com → Settings → Developer settings → Tokens |
| `DEFAULT_CLIENT_REPO` | Local path to the client git repo | e.g., `/Users/you/code/client-project` |
| `WEBHOOK_SECRET` | (Optional) Shared secret for webhook validation | Leave empty for dev |

## Step 4: Add CLAUDE.md to the Client Repo

The client repo should have a `CLAUDE.md` at its root describing:
- Tech stack and framework
- Coding conventions (naming, imports, formatting)
- Test framework and commands (`npm test`, `pytest`, etc.)
- Project structure
- Any patterns the AI should follow

This file is critical — the agent reads it before writing any code.

## Step 4b: Set Up E2E Testing (Optional)

If the client project has UI components and you want the QA agent to validate them visually:

### Install Playwright
```bash
cd <client-repo>
npm install -D @playwright/test
npx playwright install chromium
```

### Add Playwright Config
Create `playwright.config.ts` at the project root:
```typescript
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30000,
  use: {
    baseURL: "http://localhost:3000",  // adjust to your dev server port
    headless: true,
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run dev",  // adjust to your dev server command
    port: 3000,
    reuseExistingServer: true,
  },
});
```

### Add Test Script
In `package.json`:
```json
{
  "scripts": {
    "test:e2e": "npx playwright test"
  }
}
```

### Create E2E Directory
```bash
mkdir e2e
```

The QA agent will automatically detect `playwright.config.ts` and use Playwright MCP to:
- Start the dev server
- Navigate the app and interact with UI elements
- Take screenshots as evidence
- Validate acceptance criteria that have `test_type: "e2e"`

If Playwright is not installed, the QA agent skips E2E and marks those criteria as NOT_TESTED.

## Step 5: Set Up Jira Automation

See [jira-automation-setup.md](jira-automation-setup.md) for detailed instructions.

Summary:
1. Start the L1 service: `uvicorn main:app --port 8000`
2. Start a tunnel: `ngrok http 8000` (note the HTTPS URL)
3. In Jira: Project Settings → Automation → Create rule
4. Trigger: **Field value changed** → Labels → Value added
5. Action: **Send web request** → POST → `https://<your-tunnel>/webhooks/jira` → Issue data (Jira format)
6. Name: `AI Harness — Webhook on ai-implement label`
7. Turn on

## Step 6: Test

1. Create a ticket in the Jira project
2. Add the `ai-implement` label
3. Watch the L1 service logs: `tail -f /tmp/l1-service.log`
4. Within ~60 seconds: analyst enriches → comment posted to Jira → agent implements → PR created

## Pipeline Modes

The harness supports two modes, controlled by Jira labels:

| Label | Mode | What Happens |
|-------|------|-------------|
| `ai-implement` | **Multi-agent (default)** | Full pipeline: implement → code review → QA validation → PR. Takes ~6 min. Produces a code review report and QA matrix as audit artifacts. |
| `ai-quick` | **Single-agent** | Fast mode: implement → test → PR. Takes ~3-4 min. No separate review or QA step. Use for low-risk changes, typo fixes, config changes. |

> **Note:** `ai-quick` requires a second Jira automation rule with the same webhook URL but triggering on the `ai-quick` label. The L1 service detects which label was used and sets the pipeline mode accordingly.

## Platform Profiles

If the client project uses a specific platform, activate a profile by setting `platform_profile` in the client configuration or by having the L1 analyst auto-detect it:

| Platform | Auto-detection | What it adds |
|----------|---------------|-------------|
| Sitecore | `sitecore.json` or `@sitecore-jss/*` in package.json | JSS/Next.js patterns, Helix architecture, Experience Editor compatibility checks |
| Salesforce | `sfdx-project.json` | Apex patterns, LWC conventions, governor limits, SOQL injection checks |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Webhook not firing | Check Jira automation rule is enabled. Check Audit Log. |
| Analyst returns error | Verify `ANTHROPIC_API_KEY` is set and valid. |
| Agent can't push | Verify `gh auth status` shows authenticated. Token needs `repo` scope. |
| Agent stuck on permissions | Ensure spawn script uses `--dangerously-skip-permissions`. |
| PR not created | Verify GitHub token has `repo` + `read:org` scopes. |
| Wrong code style | Improve the client repo's `CLAUDE.md` with more specific conventions. |
| ngrok URL changed | Update the Jira automation rule URL. Use paid ngrok for stable subdomain. |

## Architecture

```
Jira ticket + ai-implement label
  → Jira Automation fires webhook
    → L1 Service receives webhook
      → Ticket Analyst (Claude Opus) enriches ticket
        → Comment posted to Jira with generated AC
        → Agent Team spawned in git worktree
          → Developer sub-agent implements + tests
          → Code Reviewer sub-agent reviews diff
          → QA sub-agent validates against acceptance criteria
          → Draft PR created on GitHub
```

All audit artifacts are stored in the worktree at `/.harness/logs/`:
- `pipeline.jsonl` — structured phase-by-phase log
- `code-review.md` — code review findings
- `qa-matrix.md` — acceptance criteria pass/fail matrix
- `session.log` — human-readable summary
