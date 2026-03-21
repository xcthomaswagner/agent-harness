# Correction Format

When sending corrections back to the Planner, use this structure:

```json
{
  "decision": "corrections_needed",
  "issues": [
    {
      "unit_id": "unit-2",
      "issue_type": "parallel_conflict",
      "description": "unit-2 and unit-3 both modify src/app/page.tsx but have no dependency",
      "suggestion": "Add unit-2 as a dependency of unit-3, or move the page.tsx changes to unit-3 only"
    }
  ]
}
```

## Issue Types

| Type | Description |
|------|-------------|
| `parallel_conflict` | Two parallel units touch the same file |
| `missing_coverage` | An AC or test scenario is not covered by any unit |
| `bad_dependency` | Dependency is missing, reversed, or circular |
| `unclear_description` | Unit description is too vague to implement |
| `over_decomposition` | Too many units for the scope |
| `under_decomposition` | A unit is too large and should be split |
| `missing_integration` | No unit wires the changes together |
| `other` | Anything else |

## Guidelines

- Be specific: reference unit IDs and file paths
- Always include a suggestion, not just the problem
- Prioritize mandatory check failures over quality warnings
- Don't suggest rewrites for minor issues — suggest targeted fixes
