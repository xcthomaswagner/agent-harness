# Unit Test Validation

## Process

1. **Discover the test command** from the project's CLAUDE.md or package.json/Makefile:
   - `npm test`, `pnpm test`, `pytest`, `dotnet test`, etc.

2. **Run the full test suite** (not just new tests):
   ```bash
   # Example for Node.js
   npm test -- --verbose
   # Example for Python
   pytest -v
   ```

3. **Check results:**
   - All tests pass → proceed to coverage check
   - Existing tests fail → regression introduced. Flag the failing tests and the files that were changed.
   - New tests fail → implementation bug. Route back to the developer.

4. **Check coverage** (if coverage tooling is available):
   ```bash
   # Use the helper script
   bash .claude/skills/code-review/scripts/check_coverage.sh
   ```
   - New code should have test coverage
   - Coverage should not decrease from the baseline

## What to Report

For each test:
- Test name
- Pass/fail
- If failed: error message and stack trace
- Which acceptance criterion the test validates (from test_criteria in the plan)
