# How the Agentic Developer Harness Works

## The Trigger

1. Someone adds the `ai-implement` label to a Jira ticket
2. A Jira automation rule fires a webhook POST to the L1 service

## Layer 1: Pre-Processing (L1)

3. **Webhook received** — `main.py` at `/webhooks/jira` validates the signature and normalizes the Jira payload into a `TicketPayload` using the Jira adapter. The adapter handles ADF (Atlassian Document Format) conversion so rich text descriptions come out as clean plain text. An idempotency guard prevents duplicate processing if the same ticket is already in the pipeline (Jira automation can fire multiple webhooks).

4. **Ticket Analyst** — `analyst.py` makes a direct Anthropic API call (Claude Opus) with the ticket content. The system prompt is composed from the `/ticket-analyst` skill files — SKILL.md + the rubric for the ticket type (story/bug/task) + templates. The analyst evaluates completeness and produces one of three outputs:

   - **Enriched** — generates acceptance criteria, test scenarios, edge cases, and size assessment. Most tickets go here.
   - **Info Request** — posts targeted questions as a Jira comment, sets status to "Needs Clarification". Pipeline stops until the human responds.
   - **Decomposition** — ticket is too large for a single agent team. Adds `needs-splitting` label and comments with suggested sub-tickets for the PM to create.

5. **Design extraction** — before the analyst runs, the pipeline downloads any image attachments on the ticket (PNG, JPEG, GIF, WebP up to 5 MB each). Downloaded images are sent to the analyst as vision content blocks so it can interpret mockups and wireframes visually. Both approaches work and can be combined:

   - **Attached images** — upload a design screenshot or mockup directly to the Jira ticket. The pipeline downloads it, sends it to the analyst via the Anthropic vision API, and copies it into the worktree at `.harness/attachments/` so L2 agents can read it too.
   - **Figma URLs** — paste a Figma link in the ticket description or acceptance criteria. The pipeline calls the Figma REST API to extract components, colors, typography, layout patterns, and interactive states into a structured `DesignSpec`.

6. **Pipeline routing** — `pipeline.py` handles the enriched ticket:
   - Checks for file scope conflicts with other in-progress tickets
   - Extracts Figma design spec if a Figma URL is found in the ticket
   - Sets the platform profile (Sitecore/Salesforce) from client config or auto-detection
   - Transitions the Jira ticket to "In Progress"
   - Posts the generated AC and edge cases as a Jira comment
   - Writes the enriched ticket to a temp JSON file
   - Calls the spawn script to trigger Layer 2

## The Bridge: Spawn Script

7. **`spawn-team.sh`** creates an isolated workspace:
   - Creates a git worktree from the client repo (separate copy on its own branch `ai/TICKET-ID`)
   - Runs `inject-runtime.sh` which copies the 7 skills and agent definitions into the worktree's `.claude/skills/` and `.claude/agents/`
   - Appends the pipeline instructions (`harness-CLAUDE.md`) to the client's `CLAUDE.md` — client conventions first (priority), harness instructions second
   - Copies the MCP config (Playwright, Figma) to `.mcp.json`
   - Creates `/.harness/` directory for logs, messages, and plans
   - Writes the enriched ticket JSON to `/.harness/ticket.json`
   - **Strips `ANTHROPIC_API_KEY`** from the environment so the session uses the Max subscription (flat-rate) instead of per-token API billing
   - Launches `claude -p` with the team lead prompt in `--dangerously-skip-permissions` mode

## Layer 2: Agent Team Execution

7. **Team Lead starts** — reads `.harness/ticket.json` and `CLAUDE.md`. Creates the feature branch `ai/<ticket-id>`. Checks `size_assessment.estimated_units` to select the pipeline:

### Simple Pipeline (1 unit)

8. **Developer sub-agent** — spawned via the `Agent` tool. Reads the ticket, explores the codebase, implements the changes, writes tests, runs the full test suite. Self-corrects up to 3 times if tests fail. Commits only when all tests pass.

### Full Pipeline (2+ units)

8a. **Planner sub-agent** — decomposes the ticket into atomic implementation units with a dependency graph. Each unit lists affected files, test criteria, and dependencies. Two parallel units must not touch the same file.

8b. **Plan Reviewer sub-agent** — validates the plan: no parallel file conflicts, all AC covered, valid DAG, descriptions specific enough to implement. Max 2 correction cycles.

8c. **Parallel Developer sub-agents** — independent units spawn simultaneously (multiple `Agent` calls in one message, each with `isolation: "worktree"`). Each dev gets its own git worktree and creates branch `ai/<ticket-id>/unit-N`. Units with dependencies wait for their prerequisites. Failed units transitively block dependents but don't halt independent units.

8d. **Merge Coordinator sub-agent** — merges unit branches into `ai/<ticket-id>` in topological order (dependencies first). Runs tests after each merge. If conflicts: routes to the owning dev. Squash fallback after 2 failed attempts. Cleans up unit branches after merge.

### Both Pipelines Continue With:

9. **Code Reviewer sub-agent** — reviews the diff on the merged branch. Evaluates for:
   - Correctness against acceptance criteria
   - Security issues (hardcoded secrets, injection vectors, auth gaps)
   - Style compliance with project conventions
   - Test coverage completeness
   - Logic errors and bugs

   Writes findings to `/.harness/logs/code-review.md` with a verdict: APPROVED or CHANGES_NEEDED.

10. **Judge sub-agent** (only if CHANGES_NEEDED) — validates each reviewer finding before it reaches the developer. For each issue the reviewer flagged, the Judge:
   - Reads the actual code with 20+ lines of context
   - Checks if it's real, reachable, and the suggested fix is correct
   - Runs `git blame` to reject findings on pre-existing (unchanged) code
   - Scores each finding 0–100: only issues scoring 80+ pass through

   Writes verdict to `/.harness/logs/judge-verdict.md`. This prevents false positives from consuming limited correction cycles. If all issues are rejected, the pipeline skips the developer fix and proceeds to QA.

   If validated issues remain, the team lead spawns the developer to fix only the validated issues, then re-reviews. Maximum 2 review-fix cycles.

11. **QA sub-agent** — spawned last. Reads the enriched ticket's acceptance criteria (both original and generated). Reads the code changes. Runs the full test suite. For EACH acceptance criterion, determines PASS, FAIL, or NOT_TESTED with specific evidence (which test covers it, or why it fails). For EACH edge case, determines COVERED or NOT_COVERED. Writes the QA matrix to `/.harness/logs/qa-matrix.md`. If failures are found, routes back to the developer for fixes. Maximum 2 QA-fix cycles. **Circuit breaker:** if >50% of acceptance criteria fail, the QA agent halts the pipeline and escalates the entire ticket with a diagnostic summary instead of routing individual failures.

12. **Final screenshot** — if the implementation has a visual UI, the team lead starts the dev server, navigates to the main page, and captures a screenshot saved as `/.harness/screenshots/final.png`. This screenshot is uploaded to the Jira ticket as visual proof during the completion callback.

13. **PR creation** — after code review and QA are both complete, the team lead pushes the branch and opens a draft PR via `gh pr create`. The PR body includes:
    - Summary of changes
    - Link to the Jira ticket
    - Code review verdict and any warnings
    - Full QA matrix (pass/fail per acceptance criterion)
    - Edge case coverage table
    - Test results (total passed/failed)

    Every phase transition is logged to `/.harness/logs/pipeline.jsonl` as structured JSON Lines.

14. **Completion callback** — when the agent session ends, the spawn script reads the pipeline log, extracts the PR URL and status, and POSTs to L1's `/api/agent-complete` endpoint. L1 then:
    - Uploads the final screenshot to Jira (if `/.harness/screenshots/final.png` exists)
    - Posts a completion comment to Jira with the PR link
    - Transitions the ticket to "Done"
    - Unregisters the ticket from conflict detection
    - If status is "partial": adds `partial-implementation` label and reports failed units
    - If status is "escalated": adds `needs-human` label

## Layer 3: PR Review & Feedback (L3)

15. **GitHub webhook fires** when the draft PR is opened. L1 proxies the webhook to L3 (running on port 8001) via the `/webhooks/github` proxy endpoint.

16. **Event classification** — `event_classifier.py` classifies the GitHub webhook into one of 8 event types: PR opened, PR ready for review, CI failed, CI passed, review approved, review changes requested, review comment, or ignored.

17. **PR opened** → L3 spawns a Claude Opus headless session with the `/pr-review` skill for architecture-level review. This catches cross-cutting concerns that individual code review might miss: naming consistency across files, API contract alignment, security flow integrity, dependency risks.

18. **CI failure** → L3 fetches the actual failure logs from the GitHub Actions API (failed jobs and steps), then spawns a Claude Sonnet session to fix the issue and push to the same branch. Maximum 3 fix attempts.

19. **Human review comment** → L3 spawns a session to respond. For questions: reads the relevant code and posts an explanation. For change requests: applies the fix, pushes, and confirms. Bot self-loop prevention ensures the harness doesn't respond to its own comments.

20. **Review approved** → L3 notifies L1 for autonomy tracking. The graduated autonomy engine tracks:
    - First-pass acceptance rate (target >90%)
    - Defect escape rate (target <5%)
    - Self-review catch rate (target >85%)

    When thresholds are met over a rolling 30-day window, the system recommends expanding auto-merge: first for low-risk PRs (bugs, config, deps), then for all PRs.

## Pipeline Modes

| Label | Mode | Agents | Time | When to Use |
|-------|------|--------|------|-------------|
| `ai-implement` | Multi-agent | Dev + Reviewer + QA | ~6-10 min | Default. Full quality pipeline. |
| `ai-quick` | Single-agent | Dev only | ~3-4 min | Low-risk changes, typo fixes, config. |

## Re-running Skipped or Failed Tests

If the QA matrix shows tests as NOT_TESTED or FAIL (e.g., E2E skipped due to port conflict), the developer can re-run just that phase:

```bash
# Re-run E2E tests only
curl -X POST localhost:8000/api/retest -H 'Content-Type: application/json' \
  -d '{"ticket_id": "SCRUM-8", "phase": "e2e"}'

# Re-run full QA
curl -X POST localhost:8000/api/retest -d '{"ticket_id": "SCRUM-8", "phase": "qa"}'

# Re-run code review
curl -X POST localhost:8000/api/retest -d '{"ticket_id": "SCRUM-8", "phase": "review"}'
```

Results are written to `.harness/logs/retest-{phase}.log` in the ticket's worktree. The worktree must still exist.

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

## Observability

The harness uses a two-tier observability model inspired by OpenTelemetry:

### Trace (the sequence)

`pipeline.jsonl` is a JSON Lines file written **only by the Team Lead**. Each line is a phase transition with a real timestamp, ticket ID, and outcome. This is the top-level audit trail — read this first to understand what happened and when.

```json
{"phase": "ticket_read", "ticket_id": "SCRUM-16", "timestamp": "2026-03-24T13:06:49Z", "event": "Pipeline started, quick mode"}
{"phase": "implementation", "ticket_id": "SCRUM-16", "timestamp": "2026-03-24T13:09:49Z", "event": "Implementation complete", "commit": "c924401"}
{"phase": "code_review", "ticket_id": "SCRUM-16", "timestamp": "2026-03-24T13:09:49Z", "event": "Review complete", "verdict": "APPROVED", "issues": 4}
```

### Span Details (the depth)

Each sub-agent writes its own detail file. These are the rich output attached to each trace phase:

| File | Written by | What's in it |
|------|-----------|-------------|
| `code-review.md` | Code Reviewer | Verdict, issues list, summary |
| `judge-verdict.md` | Judge | Validated/rejected issues with scores |
| `qa-matrix.md` | QA | Pass/fail per AC, edge cases, test results |
| `merge-report.md` | Merge Coordinator | Per-unit merge status, conflicts |
| `plan-review.md` | Plan Reviewer | Corrections, approval notes |
| `blocked-units.md` | Team Lead | Why units were blocked, dependency chain |
| `escalation.md` | Team Lead | What failed, how to resume |
| `session.log` | Team Lead | Human-readable summary at end |

### Key rule

Sub-agents **never** write to `pipeline.jsonl`. They write their own files. The Team Lead reads those files and logs the phase summary to the trace.

### Viewing traces

- **Trace dashboard**: `http://localhost:8000/traces` — list of all tickets processed
- **Ticket detail**: `http://localhost:8000/traces/SCRUM-16` — timeline view of one ticket
- **Raw files**: Browse `.harness/logs/` in the worktree for full detail

### Future: Langfuse/Jaeger integration

The file-based approach maps directly to OpenTelemetry spans. When Docker is configured, traces can be exported to Langfuse or Jaeger for visualization, search, and alerting. Each `pipeline.jsonl` entry becomes a parent span; each detail file becomes an attached span event.

## What's in the Worktree

After the agent finishes, the worktree contains:

```
/.harness/
  ticket.json           # The enriched ticket from L1
  pipeline-mode         # "multi" or "quick"
  .agent.lock           # Lock file preventing duplicate agent spawns
  attachments/          # Design images downloaded from the ticket (if any)
    mockup.png
    figma-FORM.png      # Rendered Figma frames (if Figma URL in ticket)
  screenshots/
    final.png           # Curated screenshot uploaded to Jira as visual proof
  logs/
    pipeline.jsonl      # Structured phase-by-phase log (JSON Lines)
    session.log         # Human-readable summary
    code-review.md      # Code reviewer's findings and verdict
    judge-verdict.md    # Judge's validation of reviewer findings (if triggered)
    qa-matrix.md        # QA pass/fail matrix per acceptance criterion
```

## Data Flow Diagram

```
Jira (ai-implement label)
  │
  ▼ webhook
L1 Service (port 8000)
  ├── Jira Adapter (normalize payload + download image attachments)
  ├── Ticket Analyst (Claude Opus API → enrich, with vision for attached images)
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
  │     ├── Agent: Judge (validate findings → judge-verdict.md, if CHANGES_NEEDED)
  │     ├── Agent: QA (validate AC → qa-matrix.md)
  │     └── Screenshot (final.png → uploaded to Jira)
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
