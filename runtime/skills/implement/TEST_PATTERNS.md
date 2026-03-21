# Test Patterns

## Test Organization

- Put tests where the project puts them (co-located, `tests/` directory, `__tests__/`, etc.)
- Name test files to match what they test: `auth.ts` → `auth.test.ts`
- Group related tests in describe blocks or test classes

## Test Structure (Arrange-Act-Assert)

```
1. Arrange — set up preconditions and inputs
2. Act — call the function or trigger the behavior
3. Assert — verify the expected outcome
```

Keep each test focused on one behavior. If you need multiple assertions, they should all relate to the same behavior.

## Test Type Guidelines

### Unit Tests
- Test pure functions and business logic
- Mock external dependencies (APIs, databases, file system)
- Fast — should run in milliseconds
- No network calls, no database access

### Integration Tests
- Test API endpoints, database queries, service interactions
- Use the project's test database or mock server
- May be slower — that's expected
- Verify contracts between components

### E2E Tests
- Test user-facing workflows through the browser
- Use the project's E2E framework (Playwright, Cypress, etc.)
- Focus on critical paths, not edge cases
- Include meaningful assertions (not just "page loaded")

## What to Test

For each acceptance criterion:
1. **Happy path** — the main success scenario
2. **Error cases** — what happens with bad input
3. **Edge cases** — boundary values, empty states, concurrent access

## Naming

Name tests descriptively:
- Good: `test_rejects_file_over_size_limit`, `should redirect unauthenticated users`
- Bad: `test1`, `test_upload`, `it works`
