# Agentic Developer Harness

> **Context A — Harness Development.** This file describes the harness system itself.
> Do NOT confuse with `runtime/harness-CLAUDE.md`, which is injected into client repos at execution time (Context C).

## What This Is

A multi-agent orchestration system that transforms Jira/Azure DevOps tickets into reviewed, tested, merge-ready Pull Requests using Claude Code Agent Teams as the execution engine.

## Architecture (Three Layers)

- **L1 Pre-Processing** (`services/l1_preprocessing/`): FastAPI webhook service. Receives Jira webhooks (ADO intake normalization exists but write-back is Jira-only), normalizes tickets, downloads image attachments, runs a Ticket Analyst (Claude Opus API call with vision support) to enrich and evaluate completeness. Supports design input via both attached images (PNG/JPEG/GIF/WebP) and Figma URLs.
- **L2 Agent Team Execution** (`runtime/`): Claude Code Agent Teams. Specialized teammates (planner, reviewer, judge, devs, QA, merge coordinator) collaborate on implementation in parallel worktrees. Judge validates code review findings to filter false positives before developer fix cycles.
- **L3 PR Review & Feedback** (`services/l3_pr_review/`): GitHub webhook service. AI PR review, CI failure auto-fix, human review comment routing. (ADO PR webhooks not yet implemented.)

See `docs/Agentic_Developer_Harness_Architecture_Plan_V2.md` for the full architecture specification.

## Tech Stack

- **Services (L1, L3):** Python 3.12+, FastAPI, Pydantic, httpx, structlog, Anthropic SDK
- **Skills & Agents:** Markdown files (Claude Code skill format)
- **Execution Layer:** Custom Bash scripts (spawn-team, inject-runtime, cleanup-worktree)
- **Scripts:** Bash (spawn, inject, cleanup, start-services, load-test)
- **CI:** GitHub Actions (ruff, mypy, pytest)
- **Testing:** pytest (unit/integration), pytest-asyncio for async FastAPI tests

## Directory Layout

```
services/
  l1_preprocessing/     # FastAPI webhook service (Python)
    adapters/            # Jira/ADO payload normalizers
    tests/               # pytest tests for L1
  l3_pr_review/          # PR review webhook service (Python)
scripts/                 # Bash scripts (spawn-team, inject-runtime, cleanup)
runtime/                 # Context C — deployed into client repo worktrees
  skills/                # 7 harness skills (ticket-analyst through pr-review)
  agents/                # Agent Team teammate definitions
  platform-profiles/     # Platform-specific knowledge (sitecore, salesforce)
  lib/                   # Shared protocols (messaging, logging)
  harness-CLAUDE.md      # Pipeline instructions injected into client repos
  harness-mcp.json       # MCP config template for agent sessions
docs/                    # Architecture docs, metrics, evaluations
tests/fixtures/          # Sample ticket payloads for testing
```

## Conventions

### Python (Services)
- Async FastAPI with type hints throughout
- Pydantic models for all data structures
- structlog for structured JSON logging
- httpx.AsyncClient for HTTP calls
- pytest + pytest-asyncio for tests
- ruff for linting/formatting, mypy for type checking

### Skills & Agents (Runtime)
- Each skill is a directory under `runtime/skills/<name>/` with a `SKILL.md` as the entry point
- Supporting files (rubrics, templates, checklists) are co-located in the skill directory
- Agent definitions live in `runtime/agents/<role>.md`
- Platform profiles supplement skills without modifying base files

### Shell Scripts
- All scripts accept `--help` and validate inputs before acting
- Error handling: fail fast, no silent overwrites
- Scripts are idempotent where possible

### Context Separation (Critical)
- **Context A (this repo):** Harness development. This CLAUDE.md applies.
- **Context B (client repos):** Where Agent Teams operate. Client's own CLAUDE.md + harness-CLAUDE.md.
- **Context C (runtime/):** Authored here, deployed to client worktrees at execution time.
- Never mix contexts. Runtime files are injected into worktrees, never committed to client repos.

## Development Workflow

1. Edit skill/agent files in `runtime/`
2. Test by injecting into a test client repo: `./scripts/inject-runtime.sh --client-repo <path>`
3. Run service tests: `cd services/l1_preprocessing && pytest`
4. Lint + typecheck: `ruff check . && mypy services/`
