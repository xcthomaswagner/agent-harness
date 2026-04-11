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

## Available Tools — Use the MCP, NOT the `sf` CLI

**Read this section before you do anything else.** The #1 failure mode on the first live run of this skill was the agent shelling out to `sf ...` commands via Bash instead of calling the `mcp__salesforce__*` tools. Do not repeat that mistake.

You have a Salesforce MCP server wired into your session. Its tools appear in your available tool list with the prefix `mcp__salesforce__`. **Every SF operation you perform must go through these tools.** The CLI fallback at the very bottom of this section is ONLY for the case where the MCP server is literally unavailable (tool list does not contain any `mcp__salesforce__*` entries), which should never happen in a properly configured harness worktree.

### Why the MCP, not the CLI

- MCP tool calls return structured JSON you can read directly — no output parsing, no regex, no fragility across `sf` CLI versions.
- The MCP server enforces `SF_HARNESS_MODE=true`, which blocks writes against production orgs. Shell `sf` calls do not have this safety net.
- Operation history is logged centrally by the MCP server for audit — shell calls are invisible.
- The skill documentation, error taxonomy, and failure-recovery guidance in this directory assume MCP output shapes. If you shell out you lose the entire guidance surface.

### MCP Tools Reference

| Phase | MCP Tool | Purpose |
|---|---|---|
| Bootstrap | `mcp__salesforce__sf_scratch_create` | Provision a scratch org for the ticket |
| Bootstrap | `mcp__salesforce__sf_org_use` | Set the active org for subsequent calls |
| Bootstrap | `mcp__salesforce__sf_org_status` | Verify the org is healthy and connected |
| Bootstrap | `mcp__salesforce__sf_org_list` | List authenticated orgs (to find the Dev Hub) |
| Compile | `mcp__salesforce__sf_deploy` (with `checkOnly: true`) | Dry-run deploy — the compile check |
| Deploy | `mcp__salesforce__sf_deploy` (with `checkOnly: false`) | Actual deploy to the scratch org |
| Deploy | `mcp__salesforce__sf_deploy_status` | Poll an async deploy job |
| Test | `mcp__salesforce__sf_apex_test` (with `codeCoverage: true`) | Run Apex tests + collect coverage |
| Test | `mcp__salesforce__sf_apex_test_status` | Poll an async test run |
| Test | `mcp__salesforce__sf_apex_coverage` | Per-class coverage details |
| Debug | `mcp__salesforce__sf_debug_logs` / `sf_debug_get_log` | Retrieve debug logs for failure analysis |
| Query | `mcp__salesforce__sf_query` | SOQL query to inspect data state |
| Cleanup | `mcp__salesforce__sf_scratch_delete` | Tear down the scratch org (merge coordinator only) |

### How to Invoke These Tools

Call them exactly like any other tool in your tool list. They take named arguments and return structured results. Concrete examples:

**Dry-run compile check (Phase 2):**
```
mcp__salesforce__sf_deploy(
  sourcePath="force-app/",
  testLevel="NoTestRun",
  checkOnly=true
)
```

**Run Apex tests with coverage (Phase 4):**
```
mcp__salesforce__sf_apex_test(
  testLevel="RunLocalTests",
  codeCoverage=true
)
```

**Create a scratch org (Phase 1):**
```
mcp__salesforce__sf_scratch_create(
  alias="ai-xcsf30-88424",
  definitionFile="config/project-scratch-def.json",
  durationDays=7
)
```

**Set the active org after creating it:**
```
mcp__salesforce__sf_org_use(alias="ai-xcsf30-88424")
```

The results come back as JSON. Read the fields, act on them, feed errors into the next cycle. See `DEPLOY_VALIDATE.md` and `APEX_TEST_STRATEGY.md` for the field shapes and how to interpret them.

### Checking That the MCP Is Actually Available

Before your first MCP tool call, verify the tools exist. Run this once at the start of Phase 1:

```
mcp__salesforce__sf_org_list()
```

If this returns a list of orgs, the MCP is live and you proceed with the rest of the skill. If it returns "tool not found" or similar, **STOP**. Do not fall back to CLI without first reporting "Salesforce MCP not available in this session" to the orchestrator — that is an infrastructure problem, not a normal operating condition, and somebody needs to investigate it before work continues.

### Production Guard

The MCP server runs with `SF_HARNESS_MODE=true`. This blocks write operations against production orgs at the MCP layer. You do not need to check org type yourself — if you point at production, the tool will return an error and a clear message telling you to use a scratch or sandbox org. If you see that error, the fix is to call `mcp__salesforce__sf_org_use` to switch to a scratch org. It is NEVER to disable the guard or fall back to the CLI to bypass it.

### CLI Fallback — Last Resort Only

**Do not use this path unless `mcp__salesforce__sf_org_list()` actually fails with "tool not found".** If the MCP is available, shell `sf` calls are forbidden.

If (and only if) the MCP is confirmed unavailable, you may fall back to `sf` CLI with `--json`:

```bash
sf project deploy start --dry-run --source-dir force-app/ --test-level NoTestRun --json
sf apex run test --code-coverage --result-format json --test-level RunLocalTests --wait 20
```

Parse the JSON yourself. Never parse human-formatted output. **Do NOT use `sf project deploy validate`** — it does not accept `NoTestRun` and will always run the full test suite. Use `deploy start --dry-run` for compile-only checks.

## The Five-Phase Loop

### Phase 1: Bootstrap (mandatory — hard gate on Phase 2)

**Phase 1 is NOT optional.** You cannot proceed to Phase 2 until a ticket-scoped scratch org exists and its alias is written to `.harness/sf-org.json`. The previous run of this skill (XCSF30-88424, session 2026-04-10) skipped this phase and pointed the dev loop at a Dev Hub directly, which produced misleading coverage numbers and polluted the hub with test data. Do not repeat that mistake.

**The hard rule:** if the target org for any `mcp__salesforce__sf_deploy` or `mcp__salesforce__sf_apex_test` call in Phase 2–4 is NOT a scratch org (i.e., its alias does not start with `ai-`), you are doing it wrong. Back up and complete Phase 1 first.

#### Step 1.1: Check for an existing scratch org

Read `.harness/sf-org.json` to see if a prior step already provisioned one. Use the Read tool, not Bash:

```
Read(file_path=".harness/sf-org.json")
```

If the file exists and contains a valid `alias` field (e.g., `ai-xcsf30-88424`), **verify it is still alive** before reusing:

```
mcp__salesforce__sf_org_status(alias="ai-xcsf30-88424")
```

If the status call succeeds, call `mcp__salesforce__sf_org_use(alias="ai-xcsf30-88424")` to set it as active and proceed to Step 1.4 (skip creation).

If the file doesn't exist OR the status call fails (expired, deleted, auth error), continue to Step 1.2.

#### Step 1.2: Identify the Dev Hub

Before creating a scratch org, you need to know which Dev Hub to create it against. Two sources, in priority order:

1. **Client profile hint.** If the client profile YAML specifies a `dev_hub` field under `source_control` or `client_repo`, use that alias.
2. **Default Dev Hub.** Otherwise, list authenticated orgs and find the one marked as the default Dev Hub:

```
mcp__salesforce__sf_org_list()
```

Look for an entry with `isDevHub: true` and use its alias. If there is more than one, prefer the one marked `isDefault: true`. If neither marker is present, pick the alphabetically first Dev Hub and proceed — but log which one you chose in your unit status so it's visible for debugging.

**If no Dev Hub is authenticated** (empty or no `isDevHub: true` entries), STOP. This is an infrastructure problem, not a ticket problem. Report "No authenticated Dev Hub available for scratch org creation" to the orchestrator and do NOT proceed. A human must run `sf org login web --set-default-dev-hub` before the pipeline can continue.

#### Step 1.3: Create the scratch org

```
mcp__salesforce__sf_scratch_create(
  alias="ai-<ticket-id-lowercased>",
  definitionFile="config/project-scratch-def.json",
  durationDays=7,
  devHub="<dev hub alias from Step 1.2>"
)
```

Use a deterministic alias derived from the ticket ID: lowercase, hyphens replacing underscores, prefix `ai-`. Examples: `SCRUM-142` → `ai-scrum-142`, `XCSF30-88424` → `ai-xcsf30-88424`, `ROC-7` → `ai-roc-7`.

**Duration:** 7 days is the default. If the ticket is expected to take longer, bump to 30.

**On failure:** see `SCRATCH_ORG_LIFECYCLE.md` for specific error taxonomy. Common causes: daily scratch limit hit (40/day typical), feature in `project-scratch-def.json` not licensed on the Dev Hub, Dev Hub auth expired. Report the specific error — do not retry blindly.

#### Step 1.4: Write `.harness/sf-org.json`

After creation (or successful reuse), write the cache file so downstream steps and other developers on the same ticket reuse the same org:

```
Write(
  file_path=".harness/sf-org.json",
  content='{"alias":"ai-xcsf30-88424","ticket_id":"XCSF30-88424","created_at":"2026-04-11T02:15:00Z"}'
)
```

This file is machine-local — it is already in `.forceignore` and `.gitignore`, do not commit it.

#### Step 1.5: Set the scratch org as active and push base metadata

```
mcp__salesforce__sf_org_use(alias="ai-xcsf30-88424")
```

Then push the current repo state to the new scratch org so your ticket changes are built on top of the client's existing codebase:

```
mcp__salesforce__sf_deploy(
  sourcePath="force-app/",
  testLevel="NoTestRun",
  checkOnly=false
)
```

If this fails on a fresh scratch org with errors referencing existing client code (not your ticket's changes), the client repo has pre-existing issues. STOP and report them — do not try to fix them as part of your ticket.

#### Ownership and teardown

The scratch org is **shared across the developer team** working on this ticket. Do NOT create a new org for each self-correction cycle. Do NOT tear it down when you finish your unit — the merge coordinator owns teardown.

See `SCRATCH_ORG_LIFECYCLE.md` for full details on alias conventions, feature flags in `project-scratch-def.json`, failure modes, and teardown procedure.

### Phase 2: Compile (dry-run deploy)

After editing any Apex, LWC, or metadata file, run a **dry-run** deploy against the scratch org. This is your "does it compile" check.

Call the MCP tool:

```
mcp__salesforce__sf_deploy(
  sourcePath="force-app/",
  testLevel="NoTestRun",
  checkOnly=true
)
```

Read the structured result. On failure, the JSON contains `componentFailures[]` with file paths, line numbers, and error messages. Feed these back into the next edit cycle.

**Never skip this step.** A file that "looks right" in the editor can fail metadata deployment for reasons that have nothing to do with the file itself (API version mismatch, missing dependency on the target, field-level security constraint). Dry-run early and often.

See `DEPLOY_VALIDATE.md` for common failure signatures and how to interpret them.

### Phase 3: Deploy (for real)

Once the dry-run passes, run the actual deploy:

```
mcp__salesforce__sf_deploy(
  sourcePath="force-app/",
  testLevel="NoTestRun",
  checkOnly=false
)
```

Apex tests do NOT run here — you run them explicitly in Phase 4. This keeps the deploy fast and lets you iterate on test code separately.

**If the deploy hits a metadata ordering problem** (e.g., `GenAiPlannerBundle` references a `GenAiFunction` that doesn't exist yet), split the deployment into ordered sub-deploys. See `METADATA_DEPLOYMENT_ORDER.md`.

### Phase 4: Test (Apex tests + coverage gate)

Run Apex tests with coverage collection:

```
mcp__salesforce__sf_apex_test(
  testLevel="RunLocalTests",
  codeCoverage=true
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

- **Do NOT shell out to `sf ...` via Bash when `mcp__salesforce__*` tools are available.** This is the most common and most damaging mistake. If the MCP is available (verified via `mcp__salesforce__sf_org_list`), every SF operation must go through MCP tool calls. CLI is a last-resort fallback only when the MCP is literally missing from your tool list.
- **Do not skip Phase 1 and point at an existing Dev Hub or sandbox directly.** The dev-loop requires a ticket-scoped scratch org — that is what `.harness/sf-org.json` exists to cache. Running tests against a shared Dev Hub pollutes it with test data and produces misleading coverage numbers (the Dev Hub's org-wide coverage will drag down your ticket's number for reasons unrelated to the ticket). See Finding 2 in the session 2026-04-10 post-mortem.
- **Do not skip Phase 2 and go straight to Phase 3.** `checkOnly=true` is much faster than a real deploy and surfaces compile errors without polluting the org.
- **Do not run `sf_apex_test` without `codeCoverage=true`.** You will deploy code that fails the production coverage gate, and the failure will only surface at merge time.
- **Do not create a new scratch org per self-correction cycle.** Scratch orgs are rate-limited per Dev Hub (typically 40/day). Reuse the ticket-scoped org.
- **Do not use `sf project deploy start` (without `--dry-run`) as your compile check.** Only relevant in the rare CLI-fallback path. Use `mcp__salesforce__sf_deploy(checkOnly=true)` first.
- **Do not use `sf project deploy validate` for the compile check.** Only relevant in the rare CLI-fallback path. Despite the name, it does not accept `NoTestRun` and will always run the full local test suite, which is slow. Use `deploy start --dry-run` instead. See `DEPLOY_VALIDATE.md`.
- **Do not hand-edit `.json` schema files for Agentforce actions without also touching the `.xml`.** Metadata API ignores schema-only changes. See `METADATA_DEPLOYMENT_ORDER.md`.
- **Do not try to bypass the production guard.** If the MCP blocks you, you pointed at the wrong org. The fix is `mcp__salesforce__sf_org_use(alias="<scratch-alias>")`, not disabling the guard.

## Supporting Documents

- `SCRATCH_ORG_LIFECYCLE.md` — Creating, reusing, and tearing down ticket-scoped scratch orgs
- `DEPLOY_VALIDATE.md` — How to use `checkOnly` deploys and interpret failure JSON
- `APEX_TEST_STRATEGY.md` — Coverage gates, parsing test results, class-level analysis
- `METADATA_DEPLOYMENT_ORDER.md` — Ordering rules for Agentforce, custom fields, record types, and other dependent metadata
