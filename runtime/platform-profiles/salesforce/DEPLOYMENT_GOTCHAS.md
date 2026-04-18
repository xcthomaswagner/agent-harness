# Salesforce Deployment Gotchas

Load this when the implementation involves `sf project deploy start`, `sf project retrieve start`, or any metadata operation. These traps make `sf` report success while the org is actually in a broken state — bad for auto-merge pipelines.

---

## 1. Schema propagation lag is real — don't trust "Deploy Succeeded"

After a successful deploy of a custom field, `Schema.describe()` + SOQL can fail to see the field for **60+ seconds** even though FieldDefinition (Tooling API) reports it as present.

**Observed sequence.**
1. Deploy succeeds. CLI returns exit 0.
2. `SELECT Id, Name FROM FieldDefinition WHERE DeveloperName = 'MyField__c'` → returns 1 row.
3. `SELECT Id, MyField__c FROM Account` → **No such column.**
4. Apex `Schema.DescribeSObjectResult.fields.getMap()` → field missing.

**Working path.** Three-gate verification after any deploy that creates fields:
1. CLI deploy success (exit 0)
2. FieldDefinition Tooling poll (retry until present)
3. Apex compile probe — deploy a throwaway class referencing `MyField__c` and confirm it compiles

The `mcp__salesforce-capability-mcp__sf_deploy_smart` tool implements this three-gate automatically. **Prefer it over raw `sf project deploy start` whenever new custom fields are involved.**

## 2. `sf project deploy start --pre-destructive-changes` silently no-ops when the component is still in the source tree

Deploy reports success, but the component survives because the source-format destructive-changes flag is fundamentally broken when the target is still in `force-app/`.

**Working path.** Use `mcp__salesforce-capability-mcp__sf_destroy` — it generates an MDAPI destructive package outside the project tree and polls for deletion to verify.

## 3. `.sf/orgs/<orgId>/localSourceTracking/` corruption (UnsafeFilepathError)

`sf project deploy start` writes an isomorphic-git repo at `.sf/orgs/<orgId>/localSourceTracking/`. If the local tracking state was written with a different cwd than the current one, `isomorphic-git.statusMatrix` throws `UnsafeFilepathError` because it sees paths escaping the repo root with `../`.

**High-risk scenarios for the harness.**
- Worktrees — each L2 team operates in its own worktree, so the project path changes
- Switching between absolute and project-relative `--source-dir` values
- Re-running a deploy from a different shell cwd

**Fix.** Delete `.sf/` at worktree entry, or always run deploys from the project root with project-relative paths. The harness's spawn-team script should consider cleaning `.sf/` on worktree creation for SF projects.

## 4. macOS `/tmp` symlink trap

`/tmp` is a symlink to `/private/tmp`. `findProjectRoot('/tmp/x/force-app')` finds `/tmp/x`, then `relative('/tmp/x', '/tmp/x/force-app')` = `force-app` — but `sf` itself resolves via `realpath`, sees `/private/tmp/...`, and the two no longer agree. Error: "path is outside project root."

**Fix.** `fs.realpath()` any path before passing to `sf project *` commands.

## 5. SFDX project preconditions for MCP tools

`sf_deploy_smart`, `sf_destroy`, and `sf_experience_bundle_bootstrap` require a valid `sfdx-project.json` in the cwd or ancestors. A disposable tempdir with a minimal project will not work for retrieve/destroy because `sf` rejects `--output-dir` / `--metadata-dir` paths that overlap the declared `packageDirectories`. Either run from within an existing project, or write the project at the target path and use relative paths within it.

## 6. `sanitizeApiName` is too strict for Network / Site names

Apex identifier rules require a leading letter. Site/network names can start with digits — `30in30` is a real site. Use the looser network-name sanitizer (`sanitizeNetworkName` in the capability MCP) for anything that's a `Network`, `CustomSite`, or `ExperienceBundle` name.

## 7. Apex exception constructors

`new System.NullPointerException('msg')` does **not** compile — `NullPointerException` has no String constructor. When a schema-check probe needs to throw with a message, use `System.QueryException`, `System.IllegalArgumentException`, or similar.

## 8. Agentforce metadata deployment order (hard requirement)

Metadata types in Agentforce reference each other. Deploy out of order and the deploy reports success with warnings, but the agent is broken at runtime.

```
1. Bot / BotVersion
2. GenAiPromptTemplate
3. GenAiFunction          (dependencies must exist on target)
4. GenAiPlugin            (referenced functions must exist)
5. GenAiPlannerBundle     (references plugins and functions)
6. Activate BotVersion    (manual — not deployable via metadata API)
```

Also:
- `schema.json` changes are **ignored without accompanying XML changes** — always touch the `genAiFunction-meta.xml` when changing schemas.
- Formatting-only `.json` changes won't deploy.
- Custom topics based on default topics embed inside GenAiPlannerBundle — they don't appear as separate GenAiPlugin files.
- Hardcoded IDs break cross-org deployments.
- `"sourceApiVersion": "60.0"` minimum in `sfdx-project.json` for Agentforce.

## 9. Metadata is additive by default

Source deploy will **not** delete a component that exists on the target but is missing from source. Apply equally to:
- NavigationMenuItem (use REST DELETE)
- CustomField (use destructive-changes via `sf_destroy`)
- ExperienceBundle sub-components

**Implication for the harness.** A developer agent that "cleans up" by removing a component from source will not actually remove it from the org. Either use destructive changes or document the cleanup as a manual follow-up.

## 10. Auth and keychain issues on macOS

- `SF_USE_GENERIC_UNIX_KEYCHAIN=true` must be set in the **same shell** as `sf org login web`, or the credential gets stored in the broken keychain and fails on next use.
- `AuthDecryptError` in `sf org list` means the credential was written before the env var was active. Fix: `sf org logout --target-org <alias> --no-prompt && export SF_USE_GENERIC_UNIX_KEYCHAIN=true && sf org login web --alias <alias> -r https://test.salesforce.com`.
- For unattended agent use, prefer JWT + External Client App over interactive `sf org login web`.

---

## Prefer MCP tools for these operations

| Task | Tool |
|---|---|
| Deploy metadata (especially with new fields) | `mcp__salesforce-capability-mcp__sf_deploy_smart` |
| Delete metadata | `mcp__salesforce-capability-mcp__sf_destroy` |
| Fetch Experience Cloud bundle (handles cold-start) | `mcp__salesforce-capability-mcp__sf_experience_bundle_bootstrap` |
| Create B2B Commerce store | `mcp__salesforce-capability-mcp__sf_b2b_setup` |
| Upload product images | `mcp__salesforce-capability-mcp__sf_commerce_images` |
| Create Experience Cloud site | `mcp__salesforce-capability-mcp__sf_settings_ui` (`operation: create-experience-site`) |
| Inject head markup | `mcp__salesforce-capability-mcp__sf_settings_ui` (`operation: inject-head-markup`) |
| SOQL / describe / generic deploy | Standard `mcp__sf*` or raw `sf` CLI |

The capability-MCP tools verify post-conditions (schema convergence, component absence, cold-start diagnostics) that raw `sf` commands do not. See `~/.claude/rules/sf-capability-mcp.md` for the full directive set.
