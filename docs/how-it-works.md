# How the Agentic Developer Harness Works

## The Trigger

1. Someone adds the `ai-implement` label to a Jira ticket
2. A Jira automation rule fires a webhook POST to the L1 service

## Layer 1: Pre-Processing (L1)

3. **Webhook received** — `main.py` at `/webhooks/jira` validates the signature and normalizes the Jira payload into a `TicketPayload` using the Jira adapter. The adapter handles ADF (Atlassian Document Format) conversion so rich text descriptions come out as clean plain text.

4. **Ticket Analyst** — `analyst.py` makes a direct Anthropic API call (Claude Opus) with the ticket content. The system prompt is composed from the `/ticket-analyst` skill files — SKILL.md + the rubric for the ticket type (story/bug/task) + templates. The analyst evaluates completeness and produces one of three outputs:

   - **Enriched** — generates acceptance criteria, test scenarios, edge cases, and size assessment. Most tickets go here.
   - **Info Request** — posts targeted questions as a Jira comment, sets status to "Needs Clarification". Pipeline stops until the human responds.
   - **Decomposition** — ticket is too large for a single agent team. Adds `needs-splitting` label and comments with suggested sub-tickets for the PM to create.

5. **Pipeline routing** — `pipeline.py` handles the enriched ticket:
   - Checks for file scope conflicts with other in-progress tickets
   - Extracts Figma design spec if a Figma URL is found in the ticket
   - Sets the platform profile (Sitecore/Salesforce) from client config or auto-detection
   - Transitions the Jira ticket to "In Progress"
   - Posts the generated AC and edge cases as a Jira comment
   - Writes the enriched ticket to a temp JSON file
   - Calls the spawn script to trigger Layer 2

## The Bridge: Spawn Script

6. **`spawn-team.sh`** creates an isolated workspace:
   - Creates a git worktree from the client repo (separate copy on its own branch `ai/TICKET-ID`)
   - Runs `inject-runtime.sh` which copies the 7 skills and agent definitions into the worktree's `.claude/skills/` and `.claude/agents/`
   - Appends the pipeline instructions (`harness-CLAUDE.md`) to the client's `CLAUDE.md` — client conventions first (priority), harness instructions second
   - Copies the MCP config (Playwright, Figma) to `.mcp.json`
   - Creates `/.harness/` directory for logs, messages, and plans
   - Writes the enriched ticket JSON to `/.harness/ticket.json`
   - **Strips `ANTHROPIC_API_KEY`** from the environment so the session uses the Max subscription (flat-rate) instead of per-token API billing
   - Launches `claude -p` with the team lead prompt in `--dangerously-skip-permissions` mode

## Layer 2: Agent Team Execution

7. **Team Lead starts** — reads `/.harness/ticket.json` and `CLAUDE.md`. Creates the feature branch `ai/<ticket-id>`. Then orchestrates three sub-agents:

8. **Developer sub-agent** — spawned via the `Agent` tool. Reads the ticket, explores the codebase (reads 3-5 similar files for patterns), implements the changes, writes tests for every acceptance criterion, runs the full test suite. Self-corrects up to 3 times if tests fail. Commits only when all tests pass. If a Figma design spec is present, follows the design tokens and component mappings.

9. **Code Reviewer sub-agent** — spawned next. Runs `git diff main...HEAD` to read the changes. Evaluates for:
   - Correctness against acceptance criteria
   - Security issues (hardcoded secrets, injection vectors, auth gaps)
   - Style compliance with project conventions
   - Test coverage completeness
   - Logic errors and bugs

   Writes findings to `/.harness/logs/code-review.md` with a verdict: APPROVED or CHANGES_NEEDED. If critical issues are found, the team lead spawns the developer again to fix them, then re-reviews. Maximum 2 review-fix cycles.

10. **QA sub-agent** — spawned last. Reads the enriched ticket's acceptance criteria (both original and generated). Reads the code changes. Runs the full test suite. For EACH acceptance criterion, determines PASS, FAIL, or NOT_TESTED with specific evidence (which test covers it, or why it fails). For EACH edge case, determines COVERED or NOT_COVERED. Writes the QA matrix to `/.harness/logs/qa-matrix.md`. If failures are found, routes back to the developer for fixes. Maximum 2 QA-fix cycles. **Circuit breaker:** if >50% of acceptance criteria fail, the QA agent halts the pipeline and escalates the entire ticket with a diagnostic summary instead of routing individual failures.

11. **PR creation** — after code review and QA are both complete, the team lead pushes the branch and opens a draft PR via `gh pr create`. The PR body includes:
    - Summary of changes
    - Link to the Jira ticket
    - Code review verdict and any warnings
    - Full QA matrix (pass/fail per acceptance criterion)
    - Edge case coverage table
    - Test results (total passed/failed)

    Every phase transition is logged to `/.harness/logs/pipeline.jsonl` as structured JSON Lines.

12. **Completion callback** — when the agent session ends, the spawn script reads the pipeline log, extracts the PR URL and status, and POSTs to L1's `/api/agent-complete` endpoint. L1 then:
    - Posts a completion comment to Jira with the PR link
    - Transitions the ticket to "Done"
    - Unregisters the ticket from conflict detection
    - If status is "partial": adds `partial-implementation` label and reports failed units
    - If status is "escalated": adds `needs-human` label

## Layer 3: PR Review & Feedback (L3)

13. **GitHub webhook fires** when the draft PR is opened. L1 proxies the webhook to L3 (running on port 8001) via the `/webhooks/github` proxy endpoint.

14. **Event classification** — `event_classifier.py` classifies the GitHub webhook into one of 8 event types: PR opened, PR ready for review, CI failed, CI passed, review approved, review changes requested, review comment, or ignored.

15. **PR opened** → L3 spawns a Claude Opus headless session with the `/pr-review` skill for architecture-level review. This catches cross-cutting concerns that individual code review might miss: naming consistency across files, API contract alignment, security flow integrity, dependency risks.

16. **CI failure** → L3 fetches the actual failure logs from the GitHub Actions API (failed jobs and steps), then spawns a Claude Sonnet session to fix the issue and push to the same branch. Maximum 3 fix attempts.

17. **Human review comment** → L3 spawns a session to respond. For questions: reads the relevant code and posts an explanation. For change requests: applies the fix, pushes, and confirms. Bot self-loop prevention ensures the harness doesn't respond to its own comments.

18. **Review approved** → L3 notifies L1 for autonomy tracking. The graduated autonomy engine tracks:
    - First-pass acceptance rate (target >90%)
    - Defect escape rate (target <5%)
    - Self-review catch rate (target >85%)

    When thresholds are met over a rolling 30-day window, the system recommends expanding auto-merge: first for low-risk PRs (bugs, config, deps), then for all PRs.

## Pipeline Modes

| Label | Mode | Agents | Time | When to Use |
|-------|------|--------|------|-------------|
| `ai-implement` | Multi-agent | Dev + Reviewer + QA | ~6-10 min | Default. Full quality pipeline. |
| `ai-quick` | Single-agent | Dev only | ~3-4 min | Low-risk changes, typo fixes, config. |

## What the Human Sees

1. A Jira comment appears within ~30 seconds with generated acceptance criteria and edge cases
2. The ticket moves to "In Progress"
3. A draft PR appears on GitHub within ~6-10 minutes containing:
   - The implementation (code + tests)
   - Code review findings embedded in the PR body
   - QA pass/fail matrix per acceptance criterion
   - Test evidence
4. The ticket moves to "Done"
5. They review one PR that has already been planned, implemented, reviewed, and QA-validated

## What's in the Worktree

After the agent finishes, the worktree contains:

```
/.harness/
  ticket.json           # The enriched ticket from L1
  pipeline-mode         # "multi" or "quick"
  logs/
    pipeline.jsonl      # Structured phase-by-phase log (JSON Lines)
    session.log         # Human-readable summary
    code-review.md      # Code reviewer's findings and verdict
    qa-matrix.md        # QA pass/fail matrix per acceptance criterion
```

## Data Flow Diagram

```
Jira (ai-implement label)
  │
  ▼ webhook
L1 Service (port 8000)
  ├── Jira Adapter (normalize payload)
  ├── Ticket Analyst (Claude Opus API → enrich)
  ├── Conflict Detector (check overlap)
  ├── Figma Extractor (if Figma URL found)
  ├── Pipeline Router (enriched → L2, info_request → Jira, decomposition → PM)
  │
  ▼ spawn-team.sh
Git Worktree (isolated branch)
  ├── inject-runtime.sh (skills, agents, CLAUDE.md, MCP config)
  ├── claude -p (team lead)
  │     ├── Agent: Developer (implement + test + commit)
  │     ├── Agent: Code Reviewer (review diff → code-review.md)
  │     └── Agent: QA (validate AC → qa-matrix.md)
  ├── git push + gh pr create
  │
  ▼ completion callback
L1 Service → Jira (Done + PR link)
  │
  ▼ GitHub webhook
L3 Service (port 8001)
  ├── Event Classifier
  ├── PR Review (Opus session)
  ├── CI Fix (Sonnet session)
  └── Comment Response (Sonnet session)
```
