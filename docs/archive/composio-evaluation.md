# Composio Agent Orchestrator — Evaluation

## Summary

**Decision: Adopt Composio as the execution layer.**

Composio Agent Orchestrator (`@composio/ao`) is actively maintained, MIT-licensed, TypeScript-based, and uses Claude Code as its default agent. It provides the execution infrastructure our harness needs without building it from scratch.

## Key Findings

### Repository
- **URL:** https://github.com/ComposioHQ/agent-orchestrator
- **Stars:** ~5,054 | **License:** MIT
- **Created:** February 2026 | **Last commit:** March 21, 2026 (today)
- **npm:** `@composio/ao`

### What It Provides (that we'd otherwise build)
- Parallel agent session management (via tmux)
- Git worktree isolation per agent
- CI failure feedback routing (reactions)
- PR event handling (review comments → agent)
- Web dashboard at localhost:3000
- Issue tracker integration (GitHub Issues, Linear — needs Jira plugin)

### How It Works with Claude Code
1. `ao start` spawns an orchestrator agent (Claude Code in tmux)
2. For each issue, `ao spawn <issue>` creates a worker in an isolated git worktree
3. Each worker is a Claude Code session with its own branch
4. Reactions handle CI failures and review comments automatically
5. Config: `agent-orchestrator.yaml` per project

### CLI (`ao`)
- `ao start` — auto-detect repo, generate config, start dashboard
- `ao spawn <issue>` — spawn agent for an issue
- `ao status` — overview of all sessions
- `ao dashboard` — open web dashboard
- `ao doctor` — health check

### Installation
```bash
npm install -g @composio/ao
# Prerequisites: Node.js 20+, Git 2.25+, tmux, gh CLI
```

## Integration Plan

### What Composio replaces
- Our `scripts/spawn-team.sh` → `ao spawn` handles worktree creation + session management
- Our worktree lifecycle management → Composio manages creation + cleanup
- Dashboard → Composio's React SPA (rebrandable in Phase 4)
- CI reaction → Composio's reaction system

### What we still need to build
- **L1 Pre-Processing Service** — Composio doesn't do ticket enrichment (our unique value)
- **L3 PR Review Service** — Composio has basic PR reactions but not our multi-skill AI review
- **Skills + Agent Definitions** — injected into worktrees before Composio spawns the agent
- **Platform Profiles** — our pluggable knowledge layer
- **Jira tracker plugin** — Composio supports GitHub Issues and Linear, needs Jira plugin

### Integration approach
1. Install Composio globally
2. Configure it for the client repo with Claude Code as agent
3. Modify `inject-runtime.sh` to run before Composio's agent spawn (via Composio's pre-spawn hook or wrapper)
4. Build a Jira tracker plugin using Composio's plugin architecture
5. L1 service calls `ao spawn` instead of our custom spawn script
6. L3 service extends Composio's reaction system for our enhanced PR review

### What we keep from our custom scripts
- `inject-runtime.sh` — still needed to inject our skills/agents/profiles into worktrees
- The injection must happen AFTER worktree creation but BEFORE the agent starts
- Composio may support this via pre-spawn hooks — needs testing

## Next Steps

1. `npm install -g @composio/ao` and run `ao doctor`
2. Test `ao start` with the synthetic test client repo
3. Test injecting our runtime files into Composio-managed worktrees
4. Build the Jira tracker plugin
5. Wire L1 service to call `ao spawn` instead of `spawn-team.sh`
