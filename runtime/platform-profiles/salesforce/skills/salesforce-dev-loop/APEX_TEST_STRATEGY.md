# Apex Test Strategy

Apex tests run **in the org**, not locally. There is no `pytest` or `jest` equivalent for Apex. You invoke them via `sf_apex_test` against the scratch org and parse the result JSON.

Coverage is enforced at deploy time by the platform itself — the org will reject a production deploy if overall coverage falls below 75% or if any class lacks ≥1% direct coverage. These gates are non-negotiable, so they become part of the dev loop, not an afterthought.

## The Two Gates

| Gate | Threshold | Enforcement | What to do if missed |
|---|---|---|---|
| Platform minimum | **≥75% overall org coverage** | Hard — Salesforce rejects the prod deploy | BLOCK the unit. Do not mark complete. |
| Team target | **≥85% overall** and **≥85% per-class** for changed classes | Soft — we enforce it | Surface a coverage concern in the unit output, but the unit can still complete |

The platform also requires **every trigger** to have ≥1% coverage. In practice this means every trigger needs at least one test that fires it, even if the test just calls `insert` and moves on.

## How to Run Tests

### Via MCP (preferred)

```
sf_apex_test(
  testLevel: "RunLocalTests",
  codeCoverage: true
)
```

For faster feedback on a specific test class:

```
sf_apex_test(
  classNames: ["AccountServiceTest", "AccountServiceHelperTest"],
  testLevel: "RunSpecifiedTests",
  codeCoverage: true
)
```

### Via CLI fallback

```bash
sf apex run test \
  --code-coverage \
  --result-format json \
  --test-level RunLocalTests \
  --wait 20
```

The `--wait 20` makes it synchronous with a 20-minute timeout. Without `--wait`, you get a `testRunId` back and have to poll with `sf_apex_test_status`.

## Test Levels

| Level | When to use |
|---|---|
| `RunLocalTests` | Default. Runs all non-managed tests in the org. The right level for Phase 4. |
| `RunSpecifiedTests` | During self-correction. When you know which test class(es) failed, re-run just those for speed. |
| `RunAllTestsInOrg` | Never during dev loop. Includes managed package tests — slow and irrelevant. |
| `NoTestRun` | Never in Phase 4. Only used during validate/deploy (Phase 2/3). |

## Reading the Result JSON

A successful test run returns:

```json
{
  "result": {
    "summary": {
      "outcome": "Passed",
      "testsRan": 47,
      "passing": 47,
      "failing": 0,
      "skipped": 0,
      "passRate": "100%",
      "failRate": "0%",
      "testRunCoverage": "87%",
      "orgWideCoverage": "82%",
      "commandTime": "54312 ms",
      "testExecutionTime": "48201 ms"
    },
    "tests": [ /* per-test results */ ],
    "coverage": {
      "coverage": [
        {
          "name": "AccountService",
          "totalLines": 120,
          "lines": { "43": 1, "44": 1, "45": 0, ... },
          "totalCovered": 108,
          "coveredPercent": 90
        }
      ]
    }
  }
}
```

**Fields that matter for the gate:**

- `summary.orgWideCoverage` — the 75% hard gate. If this is below 75%, you're BLOCKED.
- `summary.testRunCoverage` — coverage attributable to this test run only. Useful for "did my new tests actually cover my new code?"
- `coverage.coverage[]` — per-class breakdown. This is how you find the classes below the 85% target.
- `summary.failing` — any non-zero value means the unit is BLOCKED until tests pass.

## Coverage Enforcement Algorithm

After every test run:

```
1. If summary.failing > 0:
     Report failures, enter self-correction.
     Do not check coverage yet.

2. If summary.orgWideCoverage < 75:
     BLOCK. This is the platform minimum.
     Identify classes with the biggest coverage shortfall and add tests.

3. If summary.orgWideCoverage < 85:
     Add a "coverage_warning" to unit status, but continue.
     Note which classes pulled the number down.

4. For each class you touched in this unit:
     If coverage.coverage[class].coveredPercent < 85:
       Add to unit status coverage_shortfalls list.
       Write a test that covers the uncovered lines.
       Re-run Phase 4.
```

## Which Classes Are Exempt from Coverage

These classes don't need to hit 85% and should be excluded from coverage analysis:

- **`@IsTest` classes themselves** — test classes don't count toward coverage (and can't, by definition).
- **`@IsTest(SeeAllData=true)` legacy classes** — these shouldn't exist in new code, but if they do, don't add them to shortfalls. They're already technical debt.
- **Test data factories** — classes marked `@IsTest` or named `TestDataFactory*`, `*TestUtil*`, `*Mock*`.
- **Generated classes** — Apex stubs generated from WSDLs (`force-app/main/default/classes/wsdl/`) are often uncoverable.
- **Enum-only classes** — a class containing only enum definitions has no executable lines and will show 0% even if it's used everywhere.

**Never exempt "hard to test" business logic.** If a class is hard to test, that's a design problem, not a coverage problem. Refactor for testability.

## Writing Apex Tests — the Dev Loop View

You are not writing tests from scratch in isolation. You are writing tests to hit specific lines that showed up as uncovered in the result JSON. The loop is:

1. Run tests with coverage.
2. Read `coverage.coverage[class].lines` for each class you touched — the value `0` means uncovered, `1+` means covered.
3. For each uncovered line, understand what input would make execution reach it.
4. Add a test that provides that input.
5. Re-run.

**Anti-pattern: tests that don't assert.** A test that calls your method and doesn't assert anything covers lines but proves nothing. These pass coverage but fail review. Every test must make at least one assertion about the outcome.

**Anti-pattern: tests against hardcoded IDs.** IDs differ between orgs. Use `Account a = [SELECT Id FROM Account WHERE Name = 'Test'][0]` to look up IDs you inserted in the test's setup phase.

**Pattern: `@TestSetup` for shared fixtures.** Use `@TestSetup` methods to create test data once per class. Individual test methods then query for the fixtures instead of recreating them.

## Debugging Test Failures

When a test fails:

1. The result JSON includes `tests[].message` and `tests[].stackTrace` for each failure.
2. If the stack trace doesn't tell you enough, retrieve the debug log for the failing test run via `sf_debug_logs` / `sf_debug_get_log`.
3. Add `System.debug(...)` statements in the test temporarily if you need to trace execution, but **remove them before committing** — debug logs burn into the coverage calculation and clutter output.

**Common failure signatures:**

- `System.DmlException: Insert failed. INSUFFICIENT_ACCESS_OR_READONLY` → the test's user context (usually the default `System.runAs` user) doesn't have permission for the DML. Either assign a permission set or wrap in `System.runAs(testUser)`.
- `System.LimitException: Too many SOQL queries` → test is hitting governor limits, usually because a trigger is firing on bulk inserts without being bulkified. Fix the trigger, not the test.
- `System.QueryException: List has no rows for assignment` → test is querying for data that doesn't exist. Often means the previous operation silently failed.
- `System.AssertException: Assertion Failed` → the test's assertion didn't match the actual value. Read the expected vs actual from the message.

## Partial Runs During Self-Correction

Running the full test suite every self-correction cycle is wasteful. During self-correction, use `RunSpecifiedTests` with just the failing classes:

```
sf_apex_test(
  classNames: ["AccountServiceTest"],  # just the failing one
  testLevel: "RunSpecifiedTests",
  codeCoverage: true
)
```

**BUT** — before marking the unit complete, you MUST run a full `RunLocalTests` pass to confirm nothing else regressed. Partial runs are for speed during iteration, full runs are for verification before handoff.

## When Coverage Looks Right But the Deploy Still Fails Coverage

Sometimes you see 87% in the scratch org but the production deploy rejects with "Average test coverage across all Apex Classes and Triggers is 68%". Causes:

1. **Stale coverage data.** The org caches coverage results. Force a fresh run with `sf_apex_test` — do not rely on `sf_apex_coverage` alone, which may read cached numbers.
2. **Test classes not being counted.** Some deploy modes only count tests that actually ran during the deploy transaction. A production deploy that runs `RunLocalTests` measures coverage from THAT run, not from your last scratch org run.
3. **Classes deployed without tests.** You added a new class but didn't add (or forgot to include) a test for it. The overall org percentage drops even if your touched classes are well-covered.

The fix is always the same: make sure a deploy-time `RunLocalTests` pass gives ≥75% overall. If it doesn't, add tests.
