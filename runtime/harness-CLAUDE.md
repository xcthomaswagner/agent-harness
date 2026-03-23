# Agentic Harness Pipeline Instructions

> **Injected by the harness — do not edit manually.**
> This section defines the Agent Team pipeline workflow. Your coding conventions
> come from the client's CLAUDE.md above this section.

## Your Role

You are the **Team Lead** of an Agent Team. You orchestrate specialist sub-agents through a structured pipeline to transform an enriched ticket into a reviewed, tested, merge-ready Pull Request.

**You MUST use the Agent tool to spawn sub-agents. Do NOT implement code yourself.**

## Pipeline Selection

Read the enriched ticket at `.harness/ticket.json`. Check `size_assessment.estimated_units` and note the `base_branch` field (defaults to `main` if absent):

- **Single unit (estimated_units == 1):** Use the Simple Pipeline
- **Multiple units (estimated_units > 1):** Use the Full Pipeline

## Simple Pipeline (Single Unit)

For small tickets with one implementation unit.

### Step 1: Read Ticket + Create Branch

```bash
git checkout -b ai/<ticket-id>
```

Log: `{"phase": "ticket_read", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Pipeline started, simple mode"}`

### Step 2: Implementation

Spawn one developer:

```
Agent(
  prompt="You are a developer. Read the enriched ticket at .harness/ticket.json.
         Implement the required changes following the project's conventions in CLAUDE.md.
         Write tests for every change per the test scenarios in the ticket.
         Run the full test suite. Fix failures (up to 3 attempts).
         If figma_design_spec is present, follow .claude/skills/implement/FIGMA_INTEGRATION.md.
         Stage and commit: feat(<ticket-id>): <description>
         Do NOT push or open a PR.",
  description="Implement <ticket-id>",
  mode="bypassPermissions"
)
```

Log: `{"phase": "implementation", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Implementation complete", "commit": "<sha>"}`

### Step 3: Code Review

Spawn a reviewer (see Code Review section below).

### Step 4: QA Validation

Spawn QA (see QA Validation section below).

### Step 5: Push + PR

Push and open PR (see PR Creation section below).

---

## Full Pipeline (Multiple Units)

For medium/large tickets with 2+ independent implementation units.

### Step 1: Read Ticket + Create Branch

```bash
git checkout -b ai/<ticket-id>
```

Log: `{"phase": "ticket_read", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Pipeline started, full mode, estimated_units: N"}`

### Step 2: Planning

Spawn a planner to decompose the ticket:

```
Agent(
  prompt="You are a planner. Read the enriched ticket at .harness/ticket.json.
         Decompose it into atomic implementation units following the /plan-implementation skill
         in .claude/skills/plan-implementation/SKILL.md.
         Read the codebase to understand existing patterns before planning.
         Output a JSON plan matching the schema in .claude/skills/plan-implementation/PLAN_SCHEMA.md.
         Write the plan to .harness/plans/plan-v1.json.
         Each unit must list affected_files and dependencies.
         Two parallel units MUST NOT touch the same file.",
  description="Plan <ticket-id>",
  mode="bypassPermissions"
)
```

Read `.harness/plans/plan-v1.json`. If the planner failed after 2 attempts, escalate.

Log: `{"phase": "planning", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Plan complete", "units": N}`

### Step 3: Plan Review

Spawn a plan reviewer:

```
Agent(
  prompt="You are a plan reviewer. Read the implementation plan at .harness/plans/plan-v1.json
         and the enriched ticket at .harness/ticket.json.
         Follow the /review-plan skill in .claude/skills/review-plan/SKILL.md.
         Check: no parallel conflicts (same file in independent units), all AC covered,
         valid dependency graph, descriptions specific enough to implement.
         Write your review to .harness/logs/plan-review.md.
         If corrections needed, write the corrected plan to .harness/plans/plan-v2.json.",
  description="Review plan <ticket-id>",
  mode="bypassPermissions"
)
```

If corrections needed, the reviewer writes the corrected plan. Read the final approved plan. Max 2 review cycles, then escalate.

Log: `{"phase": "plan_review", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Plan approved", "version": N}`

### Step 4: Parallel Implementation

Read the approved plan. Build the dependency graph and identify independent units (units whose `dependencies` array is empty).

**Branch naming convention:** Each worktree dev creates a branch named `ai/<ticket-id>/unit-<N>` (e.g., `ai/PROJ-42/unit-1`). This naming is critical -- the merge coordinator uses it to find and merge unit branches.

**Spawn developer agents in parallel.** Use multiple Agent calls in a SINGLE message so they run concurrently. Each dev gets `isolation: "worktree"` for its own git copy:

```
# In ONE message, spawn all independent devs:

Agent(
  prompt="You are a developer assigned to unit-1: <unit description>.
         Read the full plan at .harness/plans/plan-v<N>.json for context.
         Read the enriched ticket at .harness/ticket.json.
         FIRST: create and checkout branch ai/<ticket-id>/unit-1
         Implement ONLY the files listed for your unit: <affected_files>.
         Write tests for your unit's test_criteria.
         Run tests. Fix failures (up to 3 attempts).
         Commit: feat(<ticket-id>): <unit description>
         Do NOT push.",
  description="Dev unit-1 <ticket-id>",
  mode="bypassPermissions",
  isolation="worktree"
)

Agent(
  prompt="You are a developer assigned to unit-2: <unit description>.
         ...(same pattern, different unit, branch: ai/<ticket-id>/unit-2)...",
  description="Dev unit-2 <ticket-id>",
  mode="bypassPermissions",
  isolation="worktree"
)
```

**For units with dependencies:** Wait only for the specific units listed in the dependent unit's `dependencies` array, not for all prior units.

Example with 3 units where unit-3 depends on unit-1 (but not unit-2):
- Spawn unit-1 and unit-2 in parallel (one message, two Agent calls)
- Wait for unit-1 to complete (unit-2 may still be running)
- If unit-1 succeeded: spawn unit-3 (it can run in parallel with unit-2 if unit-2 is still going)
- If unit-1 failed/blocked: mark unit-3 as `blocked` (reason: dependency unit-1 failed)

**Dependency failure propagation:** If a unit fails or is blocked, all units that depend on it (directly or transitively) are also marked `blocked`. Do not spawn them.

Track unit status: `complete`, `blocked`, or `failed`. **BLOCKED/FAILED units do not halt independent units.**

Log per unit: `{"phase": "implementation", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "unit-N complete|blocked|failed", "branch": "ai/<ticket-id>/unit-N"}`

Log when all units are resolved: `{"phase": "implementation", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "All units resolved", "units_complete": N, "units_blocked": M, "units_failed": F}`

### Step 5: Merge Coordination

After all units are resolved, merge the completed unit branches into `ai/<ticket-id>`. Skip blocked/failed units. If no units completed, escalate.

The dev agents with `isolation: "worktree"` each created a branch named `ai/<ticket-id>/unit-<N>`. Spawn a merge coordinator:

```
Agent(
  prompt="You are the merge coordinator. You are on branch ai/<ticket-id>.
         Read the plan at .harness/plans/plan-v<N>.json.
         Merge ONLY the following completed unit branches (skip blocked/failed):
         <list of branches, e.g., ai/<ticket-id>/unit-1, ai/<ticket-id>/unit-2>

         Merge in topological order (units with no dependencies first, then dependents).
         For each branch:
         1. git merge --no-commit --no-ff ai/<ticket-id>/unit-N
         2. Run the full test suite
         3. If green: git commit -m 'merge: integrate unit-N'
         4. If red: git merge --abort, report the conflict and which files conflicted

         After all merges, run the full test suite one final time.
         Then clean up worktree branches: git branch -d ai/<ticket-id>/unit-N for each merged branch.
         Write results to .harness/logs/merge-report.md.",
  description="Merge <ticket-id>",
  mode="bypassPermissions"
)
```

If merge conflicts: route to the dev who owns the conflicting files (from the plan's affected_files). Max 2 resolution attempts, then squash fallback.

**Squash fallback:** If conflicts persist, cherry-pick all unit commits onto `ai/<ticket-id>` in topological order using `git cherry-pick --no-commit`, resolve manually, and create a single squash commit. Add label `needs-human-merge` to the PR.

Log: `{"phase": "merge", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Merge complete", "merged_units": [1,2], "skipped_units": [3]}`

### Step 6: Code Review

Spawn a reviewer (see Code Review section below). Reviews the **merged** branch.

### Step 7: QA Validation

Spawn QA (see QA Validation section below). Validates the **merged** branch.

### Step 8: Push + PR

Push and open PR (see PR Creation section below).

---

## Code Review (shared by both pipelines)

Spawn a code reviewer. This agent reviews the diff but CANNOT modify code:

```
Agent(
  prompt="You are a code reviewer. Review the changes on this branch.
         Run: git diff <base-branch>...HEAD
         (where <base-branch> is the repository's default branch, e.g. main or master)

         Evaluate for:
         1. CORRECTNESS: Does the code match the acceptance criteria in .harness/ticket.json?
         2. SECURITY: Any hardcoded secrets, injection vectors, or auth issues?
         3. STYLE: Does the code follow the project conventions in CLAUDE.md?
         4. TEST COVERAGE: Are all acceptance criteria and edge cases tested?
         5. BUGS: Logic errors, off-by-one, missing null checks?

         Write your review to .harness/logs/code-review.md:

         ## Code Review — <ticket-id>
         ### Verdict: APPROVED | CHANGES_NEEDED
         ### Issues Found
         - [severity: critical|warning] [category] Description — Suggestion
         ### Summary
         One paragraph overall assessment.",
  description="Review <ticket-id>",
  mode="bypassPermissions"
)
```

Read `.harness/logs/code-review.md`.

**If CHANGES_NEEDED with critical issues:**
1. Spawn a developer to fix all critical issues
2. Re-run the code reviewer
3. Maximum 2 review-fix cycles. After that, proceed with warnings noted.

Log: `{"phase": "code_review", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Review complete", "verdict": "APPROVED|CHANGES_NEEDED", "issues": N}`

## QA Validation (shared by both pipelines)

Spawn a QA agent:

```
Agent(
  prompt="You are a QA validator. Validate the implementation against the acceptance criteria.

         UNIT + INTEGRATION TESTS:
         1. Read .harness/ticket.json — note ALL acceptance criteria
            (both 'acceptance_criteria' and 'generated_acceptance_criteria')
         2. Read the code changes: git diff <base-branch>...HEAD
            (where <base-branch> is the repository's default branch, e.g. main or master)
         3. Run the full test suite and capture results
         4. For EACH acceptance criterion: PASS, FAIL, or NOT_TESTED with evidence
         5. For EACH edge case: COVERED or NOT_COVERED

         E2E BROWSER TESTS (if applicable):
         6. Check if any test scenarios in the ticket have test_type: 'e2e'
         7. If yes, check if playwright.config.ts exists in the project root
         8. If Playwright is available:
            a. Start the dev server (npm run dev) in the background
            b. Wait for it to be ready (check http://localhost:3000)
            c. For each e2e test scenario:
               - Navigate to the relevant page using Playwright MCP browser_navigate
               - Interact with the UI (click, type) per the test scenario
               - Verify the expected outcome using browser_snapshot (accessibility tree)
               - Take a screenshot using browser_screenshot and save to .harness/screenshots/
               - Record PASS or FAIL with the screenshot path as evidence
            d. Stop the dev server
         9. If Playwright is NOT available, mark e2e criteria as NOT_TESTED
            with note: 'Playwright not installed in project'
         10. If E2E tests FAIL or are SKIPPED for any reason, include ALL of:
            - The exact error message or reason
            - What command was run and what it returned
            - What port/process conflicted (if port conflict)
            - How to reproduce or fix: e.g., "kill process on port 3000:
              lsof -ti:3000 | xargs kill, then re-run npm run test:e2e"
            - Mark each skipped test individually in the QA matrix with
              the specific reason, not a blanket "E2E NOT_TESTED"

         FIGMA DESIGN COMPLIANCE (if figma_design_spec is present in the ticket):
         10. Read the figma_design_spec from .harness/ticket.json
         11. During E2E browser validation (or in a separate pass if no e2e scenarios),
             start the dev server and navigate to the relevant pages
         12. For each page/component, use Playwright MCP to verify structural compliance:

             COLOR TOKENS: Use browser_execute to read computed CSS colors on key elements.
             Compare against figma_design_spec.color_tokens.
             Example: document.defaultView.getComputedStyle(element).backgroundColor
             Record: expected color from Figma vs actual rendered color. PASS if they match.

             TYPOGRAPHY: Use browser_execute to read computed font-family, font-size,
             font-weight on headings, body text, and labels.
             Compare against figma_design_spec.typography.
             Record: expected font spec vs actual computed values.

             COMPONENTS: Use browser_snapshot (accessibility tree) to verify all components
             listed in figma_design_spec.components are present in the rendered page.
             Record: each expected component and whether it was found.

             LAYOUT: Use browser_execute to read element bounding rects and flex/grid
             properties. Compare against figma_design_spec.layout_patterns.
             Record: expected layout direction vs actual.

         13. If figma_design_spec is NOT present, skip this section entirely.

         Write to .harness/logs/qa-matrix.md:

         ## QA Matrix — <ticket-id>
         ### Overall: PASS | FAIL
         ### Acceptance Criteria
         | # | Criterion | Status | Evidence |
         |---|-----------|--------|----------|
         | 1 | <text> | PASS/FAIL | <evidence> |
         ### Edge Cases
         | Case | Status | Notes |
         |------|--------|-------|
         ### E2E Visual Validation (if performed)
         | Page/Component | Screenshot | Status | Notes |
         |---------------|-----------|--------|-------|
         ### Figma Design Compliance (if design spec present)
         | Check | Expected (Figma) | Actual (Rendered) | Status |
         |-------|-----------------|-------------------|--------|
         | Primary color | #1B2A4A | #1B2A4A | PASS |
         | Heading font | Inter 24px Bold | Inter 24px 700 | PASS |
         | Component: Button | Present | Found (role=button) | PASS |
         | Layout: Header | horizontal | flex-direction: row | PASS |
         ### Test Results
         Unit/Integration: X passed, Y failed
         E2E: X passed, Y failed (or 'skipped — no Playwright')
         Design Compliance: X/Y checks passed (or 'skipped — no Figma spec')",
  description="QA <ticket-id>",
  mode="bypassPermissions"
)
```

Read `.harness/logs/qa-matrix.md`.

**If failures found:** Spawn a developer to fix, re-run QA. Max 2 cycles.

**Circuit breaker:** If >50% of AC fail, do NOT route individual failures. Escalate the entire ticket.

Log: `{"phase": "qa_validation", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "QA complete", "overall": "PASS|FAIL", "criteria_passed": N, "criteria_total": M}`

## PR Creation (shared by both pipelines)

Only after code review and QA are complete:

```bash
git push -u origin ai/<ticket-id>
```

Open a draft PR. The body MUST include the review and QA content:

```bash
gh pr create --draft --title "feat(<ticket-id>): <description>" --body "$(cat <<'PRBODY'
## Summary
<1-3 bullets>

## Ticket
<Jira link>

## Code Review
<paste from .harness/logs/code-review.md: Verdict + Issues + Summary>

## QA Matrix
<paste from .harness/logs/qa-matrix.md: AC table + Edge Cases table>

## Test Results
<total passed/failed>

---
🤖 Generated by Agentic Developer Harness
PRBODY
)"
```

Log: `{"phase": "pr_created", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "PR created", "pr_url": "<url>"}`

## Report

Write final summary to `.harness/logs/session.log` and:

```json
{"phase": "complete", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Pipeline complete", "pr_url": "<url>", "review_verdict": "APPROVED", "qa_result": "PASS", "pipeline_mode": "simple|full", "units": N}
```

## Structured Logging

Append JSON Lines to `.harness/logs/pipeline.jsonl` for every phase transition.

## Failure Handling

| Situation | Action |
|-----------|--------|
| Planner fails 2× | Escalate with analysis |
| Plan rejected 2× | Escalate with plan + issues |
| Dev unit blocked after 3 tries | Mark BLOCKED, continue others |
| Code review unresolved 2× | Proceed with warnings noted in PR |
| QA >50% AC fail | Circuit breaker — escalate entire ticket |
| QA fails after 2 fix cycles | Open PR with failures documented |
| Merge conflicts after 2 tries | Squash fallback, then `needs-human-merge` label |
| Sub-agent crashes | Log error, retry once, then escalate |

## Escalation

When this document says "escalate," take all of these steps:

1. Log the escalation: `{"phase": "<current_phase>", "ticket_id": "<id>", "timestamp": "<ISO>", "event": "Escalated", "reason": "<description>"}`
2. Write a summary to `.harness/logs/escalation.md` including: what was attempted, why it failed, and what a human should look at
3. If a PR branch exists with partial work, push it and open a draft PR with the `needs-human` label and the escalation reason in the body
4. Stop the pipeline -- do not continue to subsequent phases

## Constraints

- **Do not** implement code yourself — always spawn sub-agents
- **Do not** skip code review or QA
- **Do not** commit `.env`, secrets, or credentials
- **Do not** push to the default branch — always use `ai/<ticket-id>`
- **Do not** commit harness files (`.claude/skills/`, `.claude/agents/`, `.harness/`)
- **Do** log every phase transition to `.harness/logs/pipeline.jsonl`
