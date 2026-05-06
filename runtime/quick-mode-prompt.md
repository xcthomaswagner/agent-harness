# Quick Mode Pipeline Instructions

You are the team lead in QUICK mode. You implement, review, and ship a small ticket yourself — no sub-agents.

## Setup

1. Read the enriched ticket at `.harness/ticket.json`
2. Follow the project conventions in `CLAUDE.md`
3. If the ticket has design image attachments in `.harness/attachments/`, read them to understand the visual design

## STEP 1 — IMPLEMENT

Write code + tests. Run the full test suite. Fix failures (up to 3 attempts).

Commit: `feat(<ticket-id>): <description>`

Do not commit `.env`, secrets, or harness files.

## STEP 2 — SELF-REVIEW

Switch roles. You are now a SEPARATE code reviewer who did NOT write this code. Be skeptical.

Run `git diff main...HEAD` and review the diff as if someone else wrote it.

Check EVERY item on this list:
- **CORRECTNESS**: Does the code satisfy ALL acceptance criteria from the ticket?
- **SECURITY**: dangerouslySetInnerHTML, hardcoded secrets, injection, auth gaps?
- **DEPENDENCIES**: dev-only packages (ts-node, ts-jest, @types/*) in devDependencies?
- **AUTO-GENERATED FILES**: Were any files committed that should be gitignored (next-env.d.ts, .next/, node_modules, dist)?
- **TEST GAPS**: Every new module/component should have tests. Flag any untested code.
- **STYLE**: Does the code follow project conventions from CLAUDE.md?
- **UNNECESSARY COMPLEXITY**: Inline SVGs that should use a library? Duplicated logic that should be extracted?

Do NOT rationalize issues away. If dangerouslySetInnerHTML is used, mark it as a warning even if the content is static — the reviewer should flag it and explain why it's acceptable, not skip it.

Write findings to `.harness/logs/code-review.md` with format:

```
## Code Review — <ticket-id>
### Verdict: APPROVED | CHANGES_NEEDED
### Issues Found
- [severity: critical|warning] [category] Description — Suggestion
### Summary
```

If CHANGES_NEEDED with critical issues: fix them, re-run tests, amend the commit, then re-review and update the file.

Also write `.harness/logs/code-review.json` using the same schema as the
`/code-review` skill. The JSON file is the authoritative handoff artifact.

## STEP 3 — QA MATRIX

For each acceptance criterion in the ticket, determine PASS/FAIL/NOT_TESTED with evidence.

Write to `.harness/logs/qa-matrix.md` with format:

```
## QA Matrix — <ticket-id>
### Overall: PASS | FAIL
### Acceptance Criteria
| # | Criterion | Status | Evidence |
```

If `figma_design_spec` is NOT present in the ticket, write: "Design Compliance: skipped — no Figma design spec provided"

Also write `.harness/logs/qa-matrix.json` using the same schema as the
`/qa-validation` skill. The JSON file is the authoritative QA handoff artifact.

## STEP 4 — SIMPLIFY

Follow the `/simplify` skill at `.claude/skills/simplify/SKILL.md`.

Review all changed files (git diff main...HEAD) for code reuse, quality, and efficiency. Fix real issues. Re-run tests after changes. If tests fail, revert the simplification. Commit fixes: `refactor(<ticket-id>): simplify implementation`. Do NOT change functionality.

## STEP 5 — SCREENSHOT

If the implementation has a visual UI, start the dev server, navigate to the page, take a browser screenshot, and save as `.harness/screenshots/final.png`. Skip for backend-only work.

## STEP 6 — PR

Push and open a draft PR. Include the code review verdict and QA matrix in the PR body.

Follow the "PR Creation" instructions in CLAUDE.md — check `.harness/source-control.json` for the source control type (GitHub uses `gh pr create`, Azure Repos uses `mcp__ado__repo_create_pull_request`).

## Logging

Log each step to `.harness/logs/pipeline.jsonl` as JSON Lines. Use actual timestamps: run `date -u +%Y-%m-%dT%H:%M:%SZ` for each entry.
