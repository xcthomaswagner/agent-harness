# Plan Schema

The implementation plan MUST be valid JSON matching this schema.

```json
{
  "ticket_id": "PROJ-123",
  "plan_version": 1,
  "units": [
    {
      "id": "unit-1",
      "description": "Clear description of what this unit implements",
      "affected_files": [
        "src/components/user-greeting.tsx",
        "src/__tests__/user-greeting.test.tsx"
      ],
      "dependencies": [],
      "complexity": "simple|moderate|complex",
      "test_criteria": [
        {
          "scenario": "Test scenario name from enriched ticket",
          "test_type": "unit|integration|e2e",
          "description": "What to verify"
        }
      ]
    },
    {
      "id": "unit-2",
      "description": "Another unit that depends on unit-1",
      "affected_files": ["src/app/page.tsx"],
      "dependencies": ["unit-1"],
      "complexity": "simple",
      "test_criteria": [
        {
          "scenario": "Integration test",
          "test_type": "integration",
          "description": "Verify the component renders on the page"
        }
      ]
    }
  ],
  "test_strategy": {
    "unit_tests": "Description of unit test approach",
    "integration_tests": "Description of integration test approach",
    "e2e_tests": "Description of e2e test approach (or 'none' if not needed)"
  },
  "architecture_notes": "How this work fits the existing codebase patterns",
  "sizing": {
    "total_units": 2,
    "parallel_tracks": 1,
    "recommended_devs": 1,
    "estimated_complexity": "small|medium|large"
  },
  "recommendation": "full_pipeline|simple_pipeline"
}
```

## Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `ticket_id` | string | The ticket this plan is for |
| `plan_version` | int | Incremented on each revision (starts at 1) |
| `units[].id` | string | Unique identifier (e.g., "unit-1", "unit-2") |
| `units[].description` | string | What this unit implements (be specific) |
| `units[].affected_files` | string[] | Files this unit will create or modify |
| `units[].dependencies` | string[] | Unit IDs that must complete before this one |
| `units[].complexity` | enum | simple (1-2 files), moderate (3-5 files), complex (5+ files) |
| `units[].test_criteria` | object[] | Test scenarios assigned to this unit |
| `test_strategy` | object | Overall test approach for the ticket |
| `architecture_notes` | string | How the changes fit existing patterns |
| `sizing` | object | Summary sizing assessment |
| `recommendation` | enum | `full_pipeline` (default) or `simple_pipeline` if all units form a linear chain with no parallelism benefit. The team lead may switch pipeline mode based on this. |

## Validation Rules

1. Every unit `id` must be unique
2. Dependencies must reference existing unit IDs
3. No circular dependencies
4. `affected_files` must not overlap between units with no dependency relationship
5. Every test scenario from the enriched ticket must appear in at least one unit's `test_criteria`
