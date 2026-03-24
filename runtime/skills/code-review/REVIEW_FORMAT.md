# Code Review Output Format

Write your review to `.harness/logs/code-review.md` using this exact format:

```markdown
## Code Review — <ticket-id>
### Verdict: APPROVED | CHANGES_NEEDED
### Issues Found
- [severity: critical] [category] Description — Suggestion
- [severity: warning] [category] Description — Suggestion
### Summary
One paragraph overall assessment.
```

If no issues found, write `No issues found.` under Issues Found.

## Severity Levels

| Severity | Description | Action |
|----------|-------------|--------|
| `critical` | Must be fixed before merge. Security issues, logic errors, missing validation. | Block until fixed |
| `warning` | Should be fixed but won't block. Style issues, minor improvements. | Fix if time allows |

## Categories

| Category | Description |
|----------|-------------|
| `correctness` | Logic errors, wrong behavior, doesn't match AC |
| `security` | Vulnerability or security anti-pattern |
| `style` | Convention violations, naming, formatting |
| `coverage` | Missing tests, untested edge cases |
| `dependencies` | Wrong dependency classification, unnecessary packages |
| `performance` | Unnecessary computation, N+1 queries, memory leaks |

## Judge Validation

Code review findings with verdict `CHANGES_NEEDED` are validated by the **Judge**
agent before reaching the developer. The Judge scores each finding 0-100 and only
passes findings scoring 80+.

To help the Judge validate your findings:
- Always include the exact file path and line number
- Include the specific code snippet that's problematic
- Explain WHY it's a problem, not just WHAT the problem is
- If referencing a convention, cite the specific CLAUDE.md rule
- Do NOT rationalize issues away — flag them and let the Judge decide
