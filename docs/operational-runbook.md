# Operational Runbook

## Starting the System

```bash
# Quick start (L1 + L3 + ngrok)
./scripts/start-services.sh

# Without tunnel (for production or when using a stable URL)
./scripts/start-services.sh --no-tunnel
```

## Stopping the System

```bash
# Kill services
kill $(lsof -ti:8000) $(lsof -ti:8001) 2>/dev/null

# Kill all agent sessions
pkill -f "claude -p" 2>/dev/null

# Clean up worktrees
cd <client-repo> && git worktree prune
```

## Monitoring

### Service Health
```bash
curl http://localhost:8000/health  # L1
curl http://localhost:8001/health  # L3
```

### Service Logs
```bash
tail -f /tmp/l1-service.log  # L1 service
tail -f /tmp/l3-service.log  # L3 service
```

### Active Agents
```bash
ps aux | grep "claude -p" | grep -v grep
```

### Agent Session Logs
```bash
# Per-ticket logs in the worktree
cat <client-repo>/../worktrees/ai/<ticket-id>/.harness/logs/pipeline.jsonl
cat <client-repo>/../worktrees/ai/<ticket-id>/.harness/logs/session.log
cat <client-repo>/../worktrees/ai/<ticket-id>/.harness/logs/code-review.md
cat <client-repo>/../worktrees/ai/<ticket-id>/.harness/logs/qa-matrix.md
```

### ngrok Dashboard
Open `http://localhost:4040` to see incoming webhooks and their responses.

## Troubleshooting

### Webhook not reaching L1
1. Check ngrok is running: `curl http://localhost:4040/api/tunnels`
2. Check the ngrok URL matches the Jira automation rule
3. Check ngrok dashboard for incoming requests and response codes
4. Free ngrok URLs change on restart — update the Jira rule

### Agent stuck or hanging
```bash
# Find the PID
ps aux | grep "claude -p" | grep <ticket-id>

# Kill it
kill <PID>

# Clean up worktree
./scripts/cleanup-worktree.sh --client-repo <path> --branch-name ai/<ticket-id>
```

### Agent can't push to GitHub
1. Check `gh auth status` — token may be expired
2. Re-auth: `gh auth login`
3. Verify token scopes: needs `repo` + `read:org`

### Ticket re-triggered but worktree exists
The spawn script handles this automatically — it kills the existing agent and recreates the worktree. If it doesn't:
```bash
./scripts/cleanup-worktree.sh --client-repo <path> --branch-name ai/<ticket-id>
```

### Analyst returns error
1. Check `ANTHROPIC_API_KEY` is set in `.env`
2. Check API key is valid: `curl https://api.anthropic.com/v1/messages -H "x-api-key: $KEY"`
3. Check for rate limiting in L1 logs

### PR body missing review/QA content
The team lead may not have read the review/QA files before creating the PR. This is a prompt adherence issue — check `/.harness/logs/code-review.md` and `qa-matrix.md` exist, then manually update the PR body.

## Queue Mode (Redis)

### Starting Redis
```bash
redis-server  # or: brew services start redis
```

### Starting RQ Worker
```bash
cd services/l1_preprocessing
source .venv/bin/activate
rq worker harness-tickets --verbose
```

### Monitoring Queue
```bash
rq info  # Show queue stats
rq info harness-tickets  # Show specific queue
```

### Set `REDIS_URL` in `.env`
```
REDIS_URL=redis://localhost:6379/0
```

## Autonomy Metrics

### View Current Metrics
```python
from autonomy import AutonomyEngine
engine = AutonomyEngine()
print(engine.get_metrics())
```

### Record PR Outcome
```python
from autonomy import AutonomyEngine, PROutcome
engine = AutonomyEngine()
engine.record_outcome(PROutcome(
    ticket_id="SCRUM-5",
    pr_url="https://github.com/.../pull/5",
    ticket_type="story",
    created_at="2026-03-22T00:00:00Z",
    first_pass_accepted=True,
    merged=True,
))
```

## Maintenance

### Cleaning Up Old Worktrees
```bash
cd <client-repo>
git worktree list  # See all worktrees
git worktree prune  # Remove stale references
# Manually delete old worktree directories if needed
```

### Rotating Secrets
1. Generate new Jira API token at id.atlassian.com
2. Update `services/l1_preprocessing/.env`
3. Generate new GitHub PAT at github.com/settings/tokens
4. Update `.env` and re-auth `gh`: `echo $TOKEN | gh auth login --with-token`
5. Restart services

### Updating Dependencies
```bash
cd services/l1_preprocessing
source .venv/bin/activate
pip install --upgrade -e ".[dev]"
```
