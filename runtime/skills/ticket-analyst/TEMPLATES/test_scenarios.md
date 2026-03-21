# Test Scenario Template

## Structure

Each test scenario maps to one or more acceptance criteria:

```json
{
  "name": "Short descriptive name",
  "test_type": "unit|integration|e2e",
  "description": "What to verify and how",
  "criteria_ref": "Which AC this validates"
}
```

## Test Type Selection

- **unit** — Pure logic, no external dependencies. Business rules, transformations, validations.
- **integration** — API endpoints, database queries, service interactions. Requires running services.
- **e2e** — User-facing workflows through the browser. Requires the full application stack.

## Guidelines

- Each acceptance criterion should have at least one test scenario
- Prefer unit tests where possible (fast, reliable)
- Use integration tests for API contracts and data flow
- Use e2e tests only for critical user journeys
- Include both positive (happy path) and negative (error/edge) scenarios
- Name tests descriptively: "rejects_file_over_5mb" not "test_upload_error"
