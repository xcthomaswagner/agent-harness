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

## JSON Sidecar

In addition to code-review.md, use the Write tool to create
`.harness/logs/code-review.json` matching this shape exactly:

```json
{
  "verdict": "APPROVED" | "CHANGES_NEEDED",
  "issues": [
    {
      "id": "cr-1",
      "severity": "critical" | "warning",
      "category": "correctness" | "security" | "style" | "coverage" | "dependencies" | "performance",
      "file_path": "src/foo.ts",
      "line_start": 14,
      "line_end": 14,
      "summary": "One-line description",
      "details": "Longer explanation matching the Markdown entry",
      "acceptance_criterion_ref": "AC-2",
      "blocking": true,
      "is_code_change_request": true
    }
  ]
}
```

ID convention: `cr-1`, `cr-2`, ... in the order issues appear in the
Markdown review. The Judge agent echoes these ids. Do not reuse or
renumber ids between runs.

INDEPENDENT fields:
- `blocking`: true if this issue must be fixed before merge. Do NOT
  infer from severity — set it explicitly per issue. A critical issue
  may be non-blocking (e.g., pre-existing bug). A warning may be
  blocking (e.g., new lint violation the team wants gated).
- `is_code_change_request`: true if fixing this issue requires a source
  code change. Informational / out-of-scope / observation-only findings
  are false.

`acceptance_criterion_ref` required when the issue traces to a specific
AC; otherwise the empty string `""`.

Empty case: `{"verdict": "APPROVED", "issues": []}` — never omit the
issues key.
