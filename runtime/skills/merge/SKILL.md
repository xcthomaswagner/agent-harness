# /merge — Branch Integration

Integrate parallel developer branches into a single tested feature branch.

## When This Runs

After all developer units are resolved (complete, failed, or blocked) in the Full Pipeline. The team lead provides the list of completed branches and the target integration branch.

## Inputs

- The approved plan at `.harness/plans/plan-v<N>.json` (highest-numbered version)
- List of completed unit branches (from team lead)
- List of blocked/failed branches to skip
- Target integration branch: `ai/<ticket-id>`

## Step 1: Topological Sort

Read the plan's dependency graph. Sort completed branches so that units others depend on merge first.

Example: unit-2 depends on unit-1, unit-3 depends on unit-2:
1. Merge unit-1 first
2. Then unit-2
3. Then unit-3

Independent branches (no dependency relationship) can be merged in any order, but merge them sequentially to catch conflicts incrementally.

## Step 2: Sequential Merge with Validation

For each branch in order:

```bash
git checkout ai/<ticket-id>

# Merge without committing so we can test first
git merge --no-commit --no-ff ai/<ticket-id>/unit-<N>

# Run the full test suite
<test_command>

# If tests pass: commit
git commit -m "merge: integrate unit-<N>"

# If tests fail: abort
git merge --abort
```

## Step 3: Conflict Resolution

When a merge produces conflicts:

1. Identify which developer owns the conflicting files (from the plan's `affected_files`)
2. Report conflict details to the team lead for routing to the owning developer
3. The developer resolves on the integration branch
4. After resolution, re-run tests before proceeding

**Max 2 conflict resolution attempts per merge.**

## Step 4: Squash Fallback

If conflicts persist after 2 resolution attempts:

1. Abandon incremental merges
2. Reset the integration branch to the base
3. Cherry-pick all unit commits in topological order using `git cherry-pick --no-commit`
4. Resolve all conflicts in a single pass
5. Create one squash commit
6. Run the full test suite
7. If still failing, add label `needs-human-merge`

## Step 5: Final Validation

**Discovering test/lint commands:** Check `package.json` scripts, `pyproject.toml`, or the project's `CLAUDE.md` for the correct commands. Common patterns: `npm test`, `pnpm test`, `pytest`, `ruff check .`.

After all branches are merged:

```bash
# Full test suite
<test_command>

# Linting
<lint_command>
```

If failures remain after 2 fix attempts, label `needs-human-merge` and proceed.

## Anti-Patterns

- **Do NOT delete unit branches** — the team lead handles cleanup
- **Do NOT push** — the team lead handles push and PR creation
- **Do NOT skip the test suite** between merges — a green merge can break when combined with the next
- **Do NOT merge blocked/failed units** — only merge branches the team lead marked as complete

## Output

Write results to `.harness/logs/merge-report.md`:

```markdown
## Merge Report — <ticket-id>

### Merged Units
| Unit | Branch | Status | Tests After Merge |
|------|--------|--------|-------------------|
| unit-1 | ai/<ticket-id>/unit-1 | merged | 45 passed |
| unit-2 | ai/<ticket-id>/unit-2 | merged | 48 passed |

### Skipped Units
| Unit | Reason |
|------|--------|
| unit-3 | blocked (dependency unit-1 failed) |

### Final Test Suite
All 48 tests passing.

### Conflicts Encountered
None (or describe resolution)
```
