# Agentic Developer Harness

Transforms Jira tickets into reviewed, tested, merge-ready Pull Requests using Claude Code.

Add the `ai-implement` label to a Jira ticket → analyst enriches it → agents implement, review, and QA the code → draft PR appears on GitHub.

## Get Started

**[→ Quickstart Guide](docs/quickstart.md)** — 5 minutes to your first PR

## How It Works

```
Jira label → L1 analyst enriches → L2 agents implement + review + QA → PR → L3 architecture review
```

- **[How it works](docs/how-it-works.md)** — full pipeline explanation
- **[Architecture plan](docs/Agentic_Developer_Harness_Architecture_Plan_V2.md)** — original design document

## Documentation

| Doc | Description |
|-----|-------------|
| [Quickstart](docs/quickstart.md) | 5-minute setup guide |
| [Client onboarding](docs/client-onboarding-guide.md) | Full setup with all options |
| [Operational runbook](docs/operational-runbook.md) | Monitoring, troubleshooting, retest |
| [Jira automation](docs/jira-automation-setup.md) | Webhook setup with screenshots |
| [How it works](docs/how-it-works.md) | End-to-end pipeline walkthrough |
| [Security scanning](docs/security-scanning.md) | Two-layer defense, tool coverage, adding new languages |

## Dashboard

View pipeline history and debug tickets without starting the full service:

```bash
python scripts/dashboard.py              # http://localhost:8080
python scripts/dashboard.py --port 8090  # custom port
```

Three views:
- **Table** (default) — filterable trace list with phase dots, duration bars, stats
- **Board** — Kanban columns (In-Flight / Stuck / Completed)
- **Detail** — click any ticket for L1/L2/L3 span tree with expandable artifacts
