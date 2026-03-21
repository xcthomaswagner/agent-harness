# Bug Completeness Rubric

Evaluate each criterion as **present**, **partial**, or **missing**.

## Required Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 1 | **Summary** | Clear one-line description of the bug. |
| 2 | **Expected Behavior** | What should happen. |
| 3 | **Actual Behavior** | What actually happens. The observable problem. |
| 4 | **Reproduction Steps** | Steps to reproduce the bug. At minimum: where in the app, what action triggers it. |

## Recommended Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 5 | **Environment** | Browser, OS, device, or environment where the bug occurs. |
| 6 | **Severity/Impact** | How many users affected, workaround exists, data loss risk. |
| 7 | **Screenshots/Logs** | Visual evidence or error logs/stack traces. |
| 8 | **Regression Info** | Did this work before? When did it break? Related deployments. |

## Decision Logic

- **Criteria 1-3 present, criterion 4 partial but inferable** → Path A (the fix is investigatable)
- **Criteria 1-4 present** → Path A (enrich with test scenarios for the fix)
- **Criterion 3 missing (no actual behavior described)** → Path B (info request — "What happens when you do X?")
- **Criterion 2 AND 3 missing** → Path B (info request — ticket is just a title with no context)
