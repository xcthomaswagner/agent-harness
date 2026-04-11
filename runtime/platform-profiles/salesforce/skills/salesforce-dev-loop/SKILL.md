# Salesforce Dev Loop Skill

## Role

You are a **Salesforce Developer Teammate** running the inner implementation loop against a real Salesforce org. This skill replaces the generic "edit → `npm test` → commit" loop from the `implement` skill whenever the client repo contains `sfdx-project.json`.

## When This Skill Applies

**Detection rule (run this first):**

```bash
test -f sfdx-project.json && echo "sf" || echo "generic"
```

- If `sfdx-project.json` exists at the repo root, follow this skill for the verification loop.
- Continue using the `implement` skill for the code-writing phases (understanding the codebase, matching conventions, writing code, writing tests).
- This skill **supplements** `implement` — it does not replace the design/writing phases, only the "does it compile and pass tests" phases.

## Why a Different Loop

Salesforce has no local compile step. You cannot run `tsc --noEmit` or `mvn compile` to validate code before pushing. The only reliable "does this compile?" check is a **dry-run deploy**:

```
sf project deploy start --dry-run --test-level NoTestRun  (or sf_deploy with checkOnly=true)
```

against a real Salesforce org — typically a **scratch org** provisioned for the ticket. Everything in this skill exists because that single constraint changes how the inner loop works.

**Important:** do not confuse this with `sf project deploy validate`. Despite the name, `deploy validate` does not accept `NoTestRun` and always runs tests — it is slower and narrower in scope. Use `deploy start --dry-run` for compile-only checks. See `DEPLOY_VALIDATE.md` for details.

**Three things you must internalize:**

1. **The scratch org IS your compile target.** No scratch org, no verification. Bootstrap it before you write code, not after.
2. **Apex tests run in the org, not locally.** There is no `pytest`. You use `sf apex run test` (or `sf_apex_test`) against the scratch org, and the platform enforces a hard ≥75% coverage gate at deploy time.
3. **Metadata deployment order matters.** You cannot deploy a `GenAiPlannerBundle` before its `GenAiFunction` dependencies exist on the target. Get the order wrong and deploy fails, even when every file is individually valid.

## Available Tools

You have two execution channels. **Always prefer the MCP tools** — they return structured JSON and are dramatically more reliable than parsing shell output.

### Salesforce MCP Tools (preferred)

| Phase | Tool | Purpose |
|---|---|---|
| Bootstrap | `sf_scratch_create` | Provision a scratch org for the ticket |
| Bootstrap | `sf_org_use` | Set the active org for subsequent calls |
| Bootstrap | `sf_org_status` | Verify the org is healthy and connected |
| Compile | `sf_deploy` (with `checkOnly: true`) | Validate-only deploy — the compile check |
| Compile | `sf_deploy` (with `checkOnly: false`) | Actual deploy to the scratch org |
| Compile | `sf_deploy_status` | Poll an async deploy job |
| Test | `sf_apex_test` (with `codeCoverage: true`) | Run Apex tests + collect coverage |
| Test | `sf_apex_test_status` | Poll an async test run |
| Test | `sf_apex_coverage` | Per-class coverage details |
| Debug | `sf_debug_logs` / `sf_debug_get_log` | Retrieve debug logs for failure analysis |
| Query | `sf_query` | SOQL query to inspect data state |
| Cleanup | `sf_scratch_delete` | Tear down the scratch org (merge coordinator only) |

### `sf` CLI fallback

If the MCP is unavailable, fall back to `sf` CLI with `--json`:

```bash
sf project deploy start --dry-run --source-dir force-app/ --test-level NoTestRun --json
sf apex run test --code-coverage --result-format json --test-level RunLocalTests --wait 20
```

Parse the JSON yourself. Never parse human-formatted output — it changes between releases.

### Production guard

The MCP server runs in harness mode (`SF_HARNESS_MODE=true`). This **blocks write operations against production orgs** at the MCP layer. You do not need to check org type yourself — the tool will return an error and a clear message telling you to use a scratch or sandbox org. If you see that error, you pointed the wrong org. Fix the active org, don't try to bypass the guard.

## The Five-Phase Loop

### Phase 1: Bootstrap (once per ticket)

At the start of the implementation unit, check whether a scratch org already exists for this ticket. The harness persists the alias at `.harness/sf-org.json`.

```bash
if [ -f .harness/sf-org.json ]; then
  # Reuse the existing scratch org — don't burn daily scratch limits
  cat .harness/sf-org.json
else
  # Create one
  # See SCRATCH_ORG_LIFECYCLE.md for the full procedure
fi
```

The scratch org is **shared across the developer team** working on this ticket. Do NOT create a new org for each self-correction cycle. Do NOT tear it down when you finish your unit — the merge coordinator owns teardown.

See `SCRATCH_ORG_LIFECYCLE.md`.

### Phase 2: Compile (dry-run deploy)

After editing any Apex, LWC, or metadata file, run a **dry-run** deploy against the scratch org. This is your "does it compile" check.

```
sf_deploy(
  sourcePath: "force-app/",
  testLevel: "NoTestRun",
  checkOnly: true
)
```

Read the structured result. On failure, the JSON contains `componentFailures[]` with file paths, line numbers, and error messages. Feed these back into the next edit cycle.

**Never skip this step.** A file that "looks right" in the editor can fail metadata deployment for reasons that have nothing to do with the file itself (API version mismatch, missing dependency on the target, field-level security constraint). Dry-run early and often.

See `DEPLOY_VALIDATE.md` for common failure signatures and how to interpret them.

### Phase 3: Deploy (for real)

Once the dry-run passes, run the actual deploy:

```
sf_deploy(
  sourcePath: "force-app/",
  testLevel: "NoTestRun",
  checkOnly: false
)
```

Apex tests do NOT run here — you run them explicitly in Phase 4. This keeps the deploy fast and lets you iterate on test code separately.

**If the deploy hits a metadata ordering problem** (e.g., `GenAiPlannerBundle` references a `GenAiFunction` that doesn't exist yet), split the deployment into ordered sub-deploys. See `METADATA_DEPLOYMENT_ORDER.md`.

### Phase 4: Test (Apex tests + coverage gate)

Run Apex tests with coverage collection:

```
sf_apex_test(
  testLevel: "RunLocalTests",
  codeCoverage: true
)
```

Parse the result. Enforce two gates:

1. **Platform minimum: ≥75% coverage.** This is hard — Salesforce will reject the production deploy otherwise.
2. **Team target: ≥85% coverage.** This is the goal; below this, raise a coverage concern in the unit output even if tests pass.

Failing tests go straight into the self-correction loop (max 3 attempts from the `implement` skill). Do not mark the unit complete with failing tests.

See `APEX_TEST_STRATEGY.md` for coverage parsing, class-level gates, and which classes are exempt.

### Phase 5: Handoff

When your unit is done:

1. Leave the scratch org running — the merge coordinator owns teardown.
2. Write the unit status to `.harness/units/<unit-id>/status.json` with:
   - `deploy_dry_run_passed: true/false`
   - `deploy_passed: true/false`
   - `apex_tests_passed: true/false`
   - `coverage_overall: <percent>`
   - `coverage_shortfalls: [list of classes below 85%]`
3. Do NOT commit `.harness/sf-org.json` — it's machine-local state.

## Self-Correction Loop

If deploy or tests fail, you get up to 3 self-correction attempts before marking the unit BLOCKED (same rule as the generic `implement` skill). For each attempt:

1. Read the structured error from the tool response.
2. Map the error to a specific file/line.
3. Make the minimal fix that addresses the root cause.
4. Re-run Phase 2 (dry-run) → Phase 3 (deploy) → Phase 4 (test) in order.

**Do NOT re-run phases you didn't invalidate.** If Phase 2 (dry-run) passed and Phase 4 (test) failed, you don't need to re-run the dry-run — go straight to a fresh Phase 4 after editing the test.

**If the same error repeats twice**, stop and read the failure more carefully before the third attempt. Repeated identical failures almost always mean you're misreading the error, not that the fix needs to be bigger.

## Anti-Patterns (Do Not Do These)

- **Do not skip Phase 2 and go straight to Phase 3.** `checkOnly=true` is much faster than a real deploy and surfaces compile errors without polluting the org.
- **Do not run `sf apex run test` without `--code-coverage`.** You will deploy code that fails the production coverage gate, and the failure will only surface at merge time.
- **Do not create a new scratch org per self-correction cycle.** Scratch orgs are rate-limited per Dev Hub (typically 40/day). Reuse the ticket-scoped org.
- **Do not use `sf project deploy start` (without `--dry-run`) as your compile check.** Use `sf project deploy start --dry-run --test-level NoTestRun` (or `sf_deploy` with `checkOnly: true`) first. A real deploy that fails leaves the scratch org in a partial state.
- **Do not use `sf project deploy validate` for the compile check.** Despite the name, it does not accept `NoTestRun` and will always run the full local test suite, which is slow. Use `deploy start --dry-run` instead. See `DEPLOY_VALIDATE.md`.
- **Do not hand-edit `.json` schema files for Agentforce actions without also touching the `.xml`.** Metadata API ignores schema-only changes. See `METADATA_DEPLOYMENT_ORDER.md`.
- **Do not try to bypass the production guard.** If the MCP blocks you, you pointed at the wrong org. The fix is `sf_org_use` to a scratch/sandbox, not disabling the guard.

## Supporting Documents

- `SCRATCH_ORG_LIFECYCLE.md` — Creating, reusing, and tearing down ticket-scoped scratch orgs
- `DEPLOY_VALIDATE.md` — How to use `checkOnly` deploys and interpret failure JSON
- `APEX_TEST_STRATEGY.md` — Coverage gates, parsing test results, class-level analysis
- `METADATA_DEPLOYMENT_ORDER.md` — Ordering rules for Agentforce, custom fields, record types, and other dependent metadata
