# Scratch Org Lifecycle

Scratch orgs are the compile target for the dev loop. This document covers the full lifecycle: when to create, how to reuse, what to put in `project-scratch-def.json`, and when to tear down.

## Ownership Rules

- **One scratch org per ticket.** Not per developer, not per unit, not per self-correction attempt. All developers working on the same ticket share the same scratch org.
- **The first developer to run Phase 1 creates the org.** Subsequent developers read `.harness/sf-org.json` and reuse.
- **The merge coordinator owns teardown.** Developers do NOT delete the scratch org when their unit finishes — the coordinator deletes it after the merge is complete.

## The Alias Convention

Use a deterministic alias derived from the ticket ID: `ai-<TICKET_ID>` (lowercase, hyphens replacing underscores).

Examples:
- Ticket `SCRUM-142` → alias `ai-scrum-142`
- Ticket `ROC-7` → alias `ai-roc-7`
- Ticket `12345` (ADO) → alias `ai-12345`

This means any developer on the team can reconstruct the alias from the ticket ID alone, without reading `.harness/sf-org.json`.

## Bootstrap Procedure

All commands below use the `mcp__salesforce__*` MCP tools, not the `sf` CLI. If the MCP is unavailable, see the CLI fallback section in `SKILL.md` — but that is a last-resort path, not a normal one.

### Step 1: Check for an existing org

Read the cache file (use the Read tool, not Bash):

```
Read(file_path=".harness/sf-org.json")
```

If the file exists and contains an alias, verify the org is still alive:

```
mcp__salesforce__sf_org_status(alias="ai-<ticket-id>")
```

If status returns `Connected`, call `mcp__salesforce__sf_org_use(alias="ai-<ticket-id>")` and skip to Step 4. If status fails (auth error, deleted, expired), fall through to Step 2.

### Step 2: Identify the Dev Hub

Before you can create a scratch org, you need to know which Dev Hub to create it against. Priority:

1. **Client profile hint.** Check the client profile YAML for a `dev_hub` field under `source_control` or `client_repo`. If set, use that alias.
2. **Default Dev Hub from `sf_org_list`.** Otherwise, list authenticated orgs and find the Dev Hub:

```
mcp__salesforce__sf_org_list()
```

Look for entries with `isDevHub: true`. If there is exactly one, use it. If there are multiple, prefer the one marked `isDefault: true` or (failing that) the alphabetically first. Record which one you chose in the unit status so it's visible for debugging.

**If no Dev Hub is authenticated** (no entries with `isDevHub: true`), STOP. Report "No authenticated Dev Hub available for scratch org creation" to the orchestrator. A human must run `sf org login web --set-default-dev-hub` before the pipeline can continue — this is not something you can fix inside the skill.

### Step 3: Create the scratch org

```
mcp__salesforce__sf_scratch_create(
  alias="ai-<ticket-id-lowercased>",
  definitionFile="config/project-scratch-def.json",
  durationDays=7,
  devHub="<dev hub alias from Step 2>"
)
```

**Duration:** 7 days is the default. If the ticket is expected to take longer, bump to 30 (most editions' max). Don't use 1-day orgs — they expire mid-ticket.

**On failure:** Common causes are (a) Dev Hub scratch org daily limit reached (typically 40/day), (b) `project-scratch-def.json` references a feature the Dev Hub isn't licensed for, (c) Dev Hub auth expired. Report the specific error — do not retry blindly.

### Step 4: Set it as active and cache

```
mcp__salesforce__sf_org_use(alias="ai-<ticket-id>")
```

Then write the alias to `.harness/sf-org.json` using the Write tool:

```json
{
  "alias": "ai-scrum-142",
  "ticket_id": "SCRUM-142",
  "created_at": "2026-04-10T14:32:00Z",
  "expires_at": "2026-04-17T14:32:00Z"
}
```

This file is **machine-local** — it is already in `.forceignore` and `.gitignore`, do not commit it.

### Step 5: Push the base metadata

Before you start making ticket-specific changes, push the current repo state to the scratch org so you're building on top of the client's existing codebase:

```
mcp__salesforce__sf_deploy(
  sourcePath="force-app/",
  testLevel="NoTestRun",
  checkOnly=false
)
```

If this fails, the client repo has pre-existing issues — stop and report them, don't try to fix them as part of your ticket.

## Feature Flags in `project-scratch-def.json`

Many Salesforce features are **not available by default in a scratch org** and must be declared in the scratch org definition file. If the ticket involves any of these, verify the features are present before you create the org:

| Feature | Flag |
|---|---|
| Agentforce / GenAI | `"GenAIPlatform"`, `"EinsteinGPTPlatform"` |
| B2B Commerce | `"B2BCommerce"` |
| Order Management | `"OrderManagement"` |
| Service Cloud | `"ServiceCloud"` |
| Field Service | `"FieldService"` |
| Experience Cloud | `"Communities"` |
| Data Cloud | `"CustomerDataPlatform"` |
| LWR sites | `"LightningWebRuntime"` |

If the file is missing a flag you need, **edit `config/project-scratch-def.json` first, commit the change, then create the org.** Creating the org with a partial feature set and trying to patch it later does not work — features are provisioned at org creation time.

## Reuse Across the Team

When a second developer picks up a unit on the same ticket:

1. They run Phase 1 (Bootstrap).
2. `.harness/sf-org.json` already exists.
3. They call `mcp__salesforce__sf_org_status(alias="<cached alias>")` to verify it's still alive.
4. If alive, `mcp__salesforce__sf_org_use(alias="<cached alias>")` to set it active and continue.
5. If dead (expired, deleted, or unreachable), they recreate it using the same alias. `mcp__salesforce__sf_scratch_create` with an existing alias will either reuse or replace depending on the MCP implementation — check the response.

## Teardown

**Developer units: DO NOT tear down.** Leave the org running.

**Merge coordinator: tear down after merge.** When the PR is merged (or permanently abandoned), call:

```
mcp__salesforce__sf_scratch_delete(
  alias="ai-<ticket-id>",
  confirm=true
)
```

Then delete `.harness/sf-org.json`.

**Harness cleanup-worktree script:** should also attempt teardown as a safety net in case the coordinator misses it. If the alias no longer exists, the tool should succeed silently (idempotent).

## Failure Modes

### "Daily scratch org limit reached"

The Dev Hub has hit its cap (typically 40/day for prod-linked Dev Hubs, varies by license). Do NOT retry. Report to the orchestrator; the ticket must wait or use a sandbox instead of a scratch org.

### "Scratch org definition file not found"

The repo's `config/project-scratch-def.json` is missing. Before reporting failure, check the `sfdx-project.json` for a different path — some projects keep it elsewhere.

### "Feature not enabled on Dev Hub"

The requested feature isn't licensed for this Dev Hub. This is a configuration issue the client must resolve — it is NOT something you can fix by editing the scratch def. Report and stop.

### "Org created but push failed"

You created the scratch org successfully but the initial metadata push failed. **Do not delete the org** — investigate the push failure first. Deleting and recreating wastes a daily scratch org slot and the second attempt will almost certainly fail the same way.

### "Org status returns inactive"

The org was created but Salesforce marked it inactive (happens rarely during provisioning). Wait 30 seconds and poll status again. If still inactive after 2 minutes, delete and recreate once.
