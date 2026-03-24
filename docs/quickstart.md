# Quickstart — 5 Minutes to First PR

## Prerequisites

- Python 3.12+
- Git
- Claude Code CLI (`claude` on PATH) with Max subscription
- GitHub CLI (`gh auth login` done)
- A Jira Cloud project

## 1. Clone + Install

```bash
git clone git@github.com:xcthomaswagner/agent-harness.git
cd agent-harness

cd services/l1_preprocessing
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cd ../..
```

## 2. Configure

```bash
cp services/l1_preprocessing/.env.example services/l1_preprocessing/.env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...          # console.anthropic.com → API Keys
JIRA_BASE_URL=https://you.atlassian.net
JIRA_API_TOKEN=...                     # id.atlassian.com → Security → API tokens
JIRA_USER_EMAIL=you@company.com
GITHUB_TOKEN=ghp_...                   # github.com/settings/tokens (scopes: repo, read:org)
DEFAULT_CLIENT_REPO=/path/to/your/repo # the repo the agents will code in
```

## 3. Start

```bash
# Terminal 1: Start L1 service
cd services/l1_preprocessing && source .venv/bin/activate
uvicorn main:app --port 8000

# Terminal 2: Start tunnel (for Jira webhooks)
ngrok http 8000
# Note the https URL
```

Verify: `curl http://localhost:8000/health` → `{"status":"ok"}`

## 4. Set Up Jira Automation (one-time)

1. Jira → Project Settings → Automation → Create Rule
2. Trigger: **Field value changed** → Labels → Value added
3. Action: **Send web request** → POST → `https://<ngrok-url>/webhooks/jira` → Issue data (Jira format)
4. Name: `AI Harness` → Turn on

## 5. Run Your First Ticket

Add the `ai-implement` label to any ticket in your Jira project.

Within ~60 seconds: analyst enriches → agent implements → code review → QA → draft PR on GitHub.

Watch it: `tail -f /tmp/l1-service.log`

## What Happens

```
Jira label → webhook → L1 analyst enriches ticket → comment posted to Jira
  → agent spawns in git worktree → implements code → writes tests
  → code reviewer checks diff → QA validates all acceptance criteria
  → draft PR opened on GitHub with review + QA matrix
  → Jira ticket moves to "Done"
```

## Key URLs

| URL | What |
|-----|------|
| `http://localhost:8000/health` | Service health check |
| `http://localhost:8000/traces` | Trace dashboard — all tickets processed |
| `http://localhost:8000/traces/SCRUM-1` | Detailed trace for one ticket |
| `http://localhost:4040` | ngrok dashboard — incoming webhooks |

## Quick Commands

```bash
# Submit a ticket manually (skip Jira webhook)
curl -X POST localhost:8000/api/process-ticket \
  -H 'Content-Type: application/json' \
  -d '{"source":"jira","id":"TEST-1","ticket_type":"story","title":"Add a button","description":"Add a submit button to the form"}'

# Re-run E2E tests on an existing PR
curl -X POST localhost:8000/api/retest \
  -d '{"ticket_id":"SCRUM-1","phase":"e2e"}'

# Check what's running
ps aux | grep "claude -p" | grep -v grep
```

## Optional: E2E Testing with Playwright

If your project has UI components:
```bash
cd <your-repo>
npm install -D @playwright/test
npx playwright install chromium
# Create playwright.config.ts (see docs/client-onboarding-guide.md Step 4b)
```

The QA agent automatically detects Playwright and runs browser validation.

## Optional: Design Inputs

The harness supports two ways to provide design context on a ticket:

**Attach an image** — upload a PNG, JPEG, GIF, or WebP mockup directly to the Jira ticket (max 5 MB per file). The pipeline downloads it, sends it to the analyst via the Claude vision API, and copies it into the worktree so the developer agents can reference it during implementation.

**Paste a Figma URL** — include a Figma link in the ticket description or acceptance criteria (e.g., `https://www.figma.com/file/abc123/MyDesign?node-id=1:2`). The pipeline calls the Figma REST API to extract components, colors, typography, and layout patterns. Requires `FIGMA_API_TOKEN` in `.env`.

Both methods can be used together on the same ticket. No additional configuration is needed for image attachments — they work out of the box with Jira credentials.

## Agents

| Agent | Model | Triggered by | What it does |
|-------|-------|-------------|-------------|
| **Ticket Analyst** | Opus (API) | Webhook → L1 pipeline | Enriches ticket: generates AC, test scenarios, edge cases, size assessment. Only agent billed per-token (~$0.10/ticket). |
| **Team Lead** | Opus (Max) | `spawn_team.py` | Orchestrates the pipeline: reads ticket, spawns sub-agents, logs phases, creates PR. Never writes code itself. |
| **Planner** | Opus (Max) | Team Lead (full pipeline, 2+ units) | Decomposes ticket into implementation units with dependency graph. |
| **Plan Reviewer** | Sonnet (Max) | Team Lead, after planner | Validates plan: no file conflicts, all AC covered, valid DAG. |
| **Developer** | Opus (Max) | Team Lead | Implements code + tests. One per unit (full pipeline) or one total (simple pipeline). |
| **Merge Coordinator** | Sonnet (Max) | Team Lead (full pipeline only) | Merges unit branches in topological order, runs tests after each. |
| **Code Reviewer** | Sonnet (Max) | Team Lead, after implementation | Reviews diff for correctness, security, style, coverage. Read-only. |
| **Judge** | Sonnet (Max) | Team Lead, only on `CHANGES_NEEDED` | Validates reviewer findings (score 0-100). Filters false positives. Security at 60+, others at 80+. |
| **QA** | Sonnet (Max) | Team Lead, after review passes | Validates every AC as PASS/FAIL. Runs tests, E2E, design compliance. |
| **PR Reviewer** | Opus (Max) | GitHub webhook: PR opened/pushed | Architecture-level review posted as PR comment. |
| **CI Fixer** | Sonnet (Max) | GitHub webhook: CI failed | Reads failure logs, fixes issue, pushes. Max 3 attempts. |
| **Comment Responder** | Sonnet (Max) | GitHub webhook: human PR comment | Explains code or applies fix based on comment. |

**Quick mode** (`ai-quick` label): Team Lead handles everything itself — no sub-agents spawned. Faster (~5 min). Still includes an adversarial self-review (the agent switches roles and reviews its own diff as a skeptical external reviewer) and generates a full QA matrix.

**Cost**: Only the Ticket Analyst uses the Anthropic API (~$0.10-0.15/ticket). All other agents run on the Claude Max subscription (flat rate).

## Optional: Platform Profiles

If your project uses Sitecore or Salesforce, the agents automatically detect the platform from repo files (`sitecore.json`, `sfdx-project.json`) and load platform-specific coding standards, security checks, and test patterns.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Webhook not firing | Update Jira rule URL (ngrok URL changes on restart) |
| Agent can't push | Run `gh auth login` |
| Tests failing | Check `.harness/logs/session.log` in the worktree |
| Port conflict | `lsof -ti:8000 \| xargs kill` then restart |

