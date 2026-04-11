# Deploy Validate — The Compile Check

Salesforce has no local compiler. A **dry-run deploy** (exposed as `sf_deploy` with `checkOnly: true`) is the closest equivalent to `tsc --noEmit`. It sends the metadata to the target org, runs all the same validation the real deploy would run, but does NOT commit anything to the org.

Under the hood this maps to `sf project deploy start --dry-run` — not `sf project deploy validate`. The two are different commands with different flag support: `deploy start --dry-run` accepts `NoTestRun` as a test level (what we want for compile-only checks), while `deploy validate` does NOT — its minimum is `RunLocalTests`. **Always use `deploy start --dry-run` for fast compile feedback.** Reserve `deploy validate` for quarantined production pre-flight checks where running tests is required.

**Rule: dry-run before every real deploy.** A dry-run is fast (typically 10-30 seconds for a handful of files) and catches 90% of what real deploys catch without leaving partial state in the org.

## When to Validate

- After editing any `.cls`, `.trigger`, `.js`, `.js-meta.xml`, or metadata XML file, before moving to real deploy.
- After generating/editing any Agentforce metadata (bots, plugins, functions, planner bundles).
- Before handing off a unit to the next stage.
- Whenever you're uncertain about a dependency ordering.

## How to Call It

### Via MCP (preferred)

```
sf_deploy(
  sourcePath: "force-app/",
  testLevel: "NoTestRun",
  checkOnly: true
)
```

Narrow the scope if you can:

```
sf_deploy(
  sourcePath: "force-app/main/default/classes/AccountService.cls",
  testLevel: "NoTestRun",
  checkOnly: true
)
```

A narrower path means a faster validate cycle and a clearer failure signal.

### Via CLI fallback

```bash
sf project deploy start \
  --dry-run \
  --source-dir force-app/ \
  --test-level NoTestRun \
  --json
```

Pipe the result into a file and parse — never rely on the human-formatted output.

**Do NOT use `sf project deploy validate` as your compile check.** Despite the name, `deploy validate` does NOT accept `--test-level NoTestRun` — it requires at least `RunLocalTests`, which means every validate attempt also runs the whole test suite. `deploy start --dry-run` is the correct command for fast compile-only feedback.

## Test Level — `NoTestRun` vs `RunLocalTests`

| Test level | When to use |
|---|---|
| `NoTestRun` | Default for the dry-run compile check. Fast feedback on compile errors without burning time on tests. Works with `deploy start --dry-run`, NOT with `deploy validate`. |
| `RunSpecifiedTests` | When you need to verify a specific test class passes after a change, without running the whole suite. |
| `RunLocalTests` | Only in Phase 4 (Test). Don't combine with the dry-run compile check — run the tests explicitly via `sf_apex_test`. |
| `RunAllTestsInOrg` | Never during dev loop. This includes managed package tests and is slow and irrelevant. |

**The recommended pattern:** `NoTestRun` during the dry-run compile check, then a separate `sf_apex_test(testLevel: "RunLocalTests")` call in Phase 4. This gives you cleaner failure signals — you know a dry-run failure is a compile problem, and a test failure is a behavior problem.

## Reading the Result JSON

A successful validate returns:

```json
{
  "success": true,
  "status": "Succeeded",
  "numberComponentsDeployed": 12,
  "numberComponentErrors": 0,
  "numberComponentsTotal": 12,
  "checkOnly": true
}
```

A failed validate returns `success: false` and a `details.componentFailures` array:

```json
{
  "success": false,
  "status": "Failed",
  "details": {
    "componentFailures": [
      {
        "problemType": "Error",
        "fileName": "force-app/main/default/classes/AccountService.cls",
        "lineNumber": 42,
        "columnNumber": 15,
        "problem": "Variable does not exist: acctIds",
        "componentType": "ApexClass",
        "fullName": "AccountService"
      }
    ]
  }
}
```

**Each failure tells you exactly which file, which line, and what the error is.** Read the full `componentFailures` array — there are often multiple errors from a single edit, and fixing them in batch is faster than one-at-a-time.

## Common Failure Signatures

### `Variable does not exist: X`

You referenced a variable that wasn't declared in scope. Check imports and field names — Apex is case-insensitive for references but case-sensitive for declarations, which trips people up.

### `Method does not exist or incorrect signature`

The method you called doesn't exist on the target org yet. Common causes:
1. You added a method in a file you haven't deployed yet — deploy the declaring class first.
2. The method is on a managed package class that's not installed on the scratch org.
3. You misspelled the method name or passed the wrong argument types.

### `Invalid field: X on Y`

A custom field referenced in code doesn't exist on the target org. Either:
1. The field is defined in `force-app/` but isn't being deployed (check `.forceignore`).
2. The field is pre-existing in the client's prod org but wasn't pushed to the scratch org — run the base metadata push from Phase 1 again.
3. You misspelled the field API name (remember custom fields end in `__c`).

### `Cannot modify managed component`

You're trying to modify a metadata component that belongs to a managed package. You can't — those are immutable on the target org. Either work around it by extending, or push back on the ticket scope.

### `Dependent class is invalid and needs recompilation`

A class you didn't touch is broken because a class you DID touch changed its public API in a way that breaks downstream callers. Read the full `componentFailures` — the first error is usually the root cause, and the rest are cascading failures.

### `sObject type 'X' is not supported`

The object isn't available in this org. Either:
1. The scratch org is missing a feature flag — check `project-scratch-def.json` (see `SCRATCH_ORG_LIFECYCLE.md`).
2. You're trying to reference a custom object that hasn't been deployed yet.
3. The object is gated behind a license the scratch org doesn't have.

### `Invalid type: Schema.X`

Usually means a custom object or custom setting doesn't exist. Same causes as the previous entry.

### `Field is not writeable: CreatedDate / SystemModstamp / LastModifiedDate`

You're trying to set an audit field. Apex tests can set these via `Test.setCreatedDate(id, dt)`. Production code can't set them at all.

### Agentforce-specific: `Invalid reference to GenAiFunction: X`

The `GenAiPlannerBundle` or `GenAiPlugin` references a function that doesn't exist on the target org. This is the **deployment ordering problem** — see `METADATA_DEPLOYMENT_ORDER.md`. You must deploy functions before plugins before planner bundles.

### Agentforce-specific: `Schema changes detected but no metadata change`

You edited a `schema.json` for a `GenAiFunction` without also touching the corresponding `.genAiFunction-meta.xml`. Metadata API ignores schema-only changes. Fix: make any edit to the XML file (adding a space, bumping a version) to force the deploy to pick up the schema change.

## After a Validate Failure

1. Read every `componentFailures` entry, not just the first.
2. Group errors by file — often one edit causes multiple failures in one file.
3. Fix the root cause, not the symptom (`Dependent class is invalid` is almost never about the file the error points to).
4. Re-run validate on the narrowest possible source path for the fastest feedback.
5. Only run a broad validate (`sourcePath: force-app/`) once the narrow validates pass.

## When Validate Passes But Real Deploy Fails

Rare, but happens. The usual causes:
- **Active triggers on the target org** that fire during deploy and fail on the data in the scratch org.
- **Validation rules** that block the DML the deploy performs (e.g., setting a required custom field).
- **Flows with SOQL errors** that only surface when the flow actually runs at deploy time.

If you hit this, the fix is the same as any runtime error: read the error, trace it to the cause, fix, re-validate, re-deploy. Don't assume validate is lying — it's telling you what it can see statically, and some things only fail at runtime.
