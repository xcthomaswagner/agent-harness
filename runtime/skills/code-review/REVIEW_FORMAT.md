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
