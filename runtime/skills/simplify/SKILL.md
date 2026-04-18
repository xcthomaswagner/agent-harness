# /simplify — Code Simplification Review

Review changed code for reuse, quality, and efficiency. Fix real issues. Skip false positives.

## Inputs

- The code changes on this branch (`git diff <base-branch>...HEAD`)
- The project's coding conventions in `CLAUDE.md`
- The existing codebase (for finding reusable utilities)

## When This Runs

After QA passes, before PR creation. This is the final cleanup pass — functionality is complete and tested.

## Iteration Limit

One pass through the changed files. Do NOT loop (find issue → fix → find another → fix another). Review all files once, fix all issues found, run tests once. If tests fail, revert all simplifications and stop.

## Scope

Only review files changed in this branch:

```bash
git diff <base-branch>...HEAD --name-only
```

Do NOT review files that were not modified by this ticket.

## Review Checklist

### Code Reuse

- Search for existing utilities and helpers that could replace newly written code
- Flag new functions that duplicate existing functionality — suggest the existing one
- Flag inline logic that could use an existing utility (hand-rolled string manipulation, manual path handling, ad-hoc type guards)

### Code Quality

- **Redundant state**: state that duplicates existing state, cached values that could be derived
- **Copy-paste with variation**: near-duplicate code blocks that should be unified
- **Leaky abstractions**: exposing internal details that should be encapsulated
- **Stringly-typed code**: using raw strings where constants or enums already exist
- **Unnecessary comments**: comments explaining WHAT (well-named identifiers already do that) — keep only non-obvious WHY

### Efficiency

- **Unnecessary work**: redundant computations, repeated file reads, duplicate API calls, N+1 patterns
- **Missed concurrency**: independent operations run sequentially when they could be parallel
- **Unbounded structures**: data structures that grow without cleanup
- **Overly broad operations**: reading entire files when only a portion is needed

## Rules

1. **Do NOT change functionality.** The implementation is complete and QA-verified.
2. **Do NOT add features.** No new capabilities, no extra configurability.
3. **Do NOT add comments, docstrings, or type annotations** to code you didn't change.
4. **Re-run the test suite** after every change.
5. **If tests fail, revert** the simplification that broke them. Do not debug — the original code was correct.
6. **Skip false positives.** If a pattern looks suboptimal but works correctly and isn't worth the churn, leave it.

## Commit

If changes were made:

```bash
git add <changed files>
git commit -m "refactor(<ticket-id>): simplify implementation"
```

## Output — Mandatory Post-Condition

You MUST write `.harness/logs/simplify.md` before returning. This is a hard post-condition, not optional. The learning miner's `simplify_no_sidecar` detector reads this file to reconcile against the Team Lead's `pipeline.jsonl` entry `{phase: "simplify", changes_made: true|false}` — if `changes_made=true` but the sidecar is absent, the miner emits a warning lesson.

### Template — changes were made

```markdown
## Simplification — <ticket-id>

### Changes Made
- [description of each simplification, one bullet per change]

### Skipped
- [patterns noticed but intentionally left alone, with reason]

### Tests
All passing after changes.
```

### Template — no changes were warranted

```markdown
## Simplification — <ticket-id>

No simplification opportunities found. Code is clean.
```

### Verify before returning

Run these checks in order. If any fails, fix it before returning:

- [ ] `.harness/logs/simplify.md` exists.
- [ ] The file is non-empty (at least the template header line).
- [ ] If any `refactor(<ticket-id>)` commit was made this run, the file has a non-empty `### Changes Made` section listing the commits.
- [ ] If no commits were made, the file uses the "no changes" template verbatim.

The Team Lead will log `{"phase": "simplify", "changes_made": true|false}` to `pipeline.jsonl` based on whether a refactor commit was created. The presence of `simplify.md` is independent of that and is always required.
