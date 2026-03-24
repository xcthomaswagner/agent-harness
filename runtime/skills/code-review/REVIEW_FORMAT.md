# Code Review Output Format

## Approved

```json
{
  "decision": "approved",
  "unit_id": "unit-1",
  "notes": "Optional positive feedback or minor suggestions (non-blocking)"
}
```

## Change Requests

```json
{
  "decision": "change_requests",
  "unit_id": "unit-1",
  "issues": [
    {
      "severity": "critical|warning",
      "category": "correctness|security|style|coverage|performance",
      "file": "src/lib/greeting.ts",
      "line": 15,
      "description": "What's wrong and why",
      "suggestion": "How to fix it"
    }
  ]
}
```

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
| `performance` | Unnecessary computation, N+1 queries, memory leaks |

## Judge Validation

Code review findings are validated by the **Judge** agent before reaching the developer.
The Judge scores each finding 0-100 and only passes findings scoring 80+.

To help the Judge validate your findings:
- Always include the exact file path and line number
- Include the specific code snippet that's problematic
- Explain WHY it's a problem, not just WHAT the problem is
- If referencing a convention, cite the specific CLAUDE.md rule
