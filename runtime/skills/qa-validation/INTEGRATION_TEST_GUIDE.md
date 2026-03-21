# Integration Test Validation

## When to Run

Run integration tests when the plan includes:
- API endpoint changes
- Database schema changes
- Service-to-service communication changes
- Authentication/authorization changes

Skip if the ticket only affects isolated utility functions or UI components with no backend interaction.

## Process

1. **Start required services:**
   - Check if a docker-compose or dev server is needed
   - Start using the project's standard command (e.g., `docker-compose up -d`, `npm run dev`)

2. **Run integration tests:**
   ```bash
   # Node.js projects often have a separate test script
   npm run test:integration
   # Python projects
   pytest tests/integration/ -v
   ```

3. **Verify contracts:**
   - API endpoints return expected status codes
   - Response bodies match expected schemas
   - Error responses are properly formatted
   - Auth-protected endpoints reject unauthenticated requests

4. **Check for regressions:**
   - Related endpoints still work correctly
   - No N+1 query regressions (if DB-backed)
   - Response times are reasonable

## What to Report

For each integration test:
- Test name and endpoint/service tested
- Pass/fail
- If failed: full request/response details
- Which acceptance criterion it validates
