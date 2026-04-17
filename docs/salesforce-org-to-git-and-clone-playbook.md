# Salesforce: Org → Git → Clone-to-New-Org Playbook

**Status:** Draft — picked up 2026-04-13, ready to execute when greenlit.
**Scope:** Take a Salesforce org with custom code (Apex, LWC, B2B Commerce, custom Service Cloud forms) that is **not yet in source control**, snapshot it into a new GitHub repo, then deploy that repo's metadata + data into a new target org. Optionally restyle the storefront on a feature branch before promoting.

This doc captures the agreed approach so we can resume without re-deriving it.

---

## Assumptions (locked in)

- **No user migration.** Target starts with an admin user only. Buyer users + contacts get recreated in target via metadata + seed script, not exported from source.
- **Source has no git history.** First commit on the new repo becomes the baseline.
- **Target may be empty (greenfield) or have minimal scaffolding.** Either way, treat metadata deploy as a fresh push.
- **B2B Commerce is in scope** (WebStore, ProductCatalog, BuyerGroup, ExperienceBundle, etc.) plus standard Apex/LWC/Flow/custom objects.
- **Restyle is a separate, optional phase** that runs after the baseline lands in git.

---

## End-to-end flow

```
SOURCE org (no git)
  ↓ Phase 1: retrieve all metadata as DX project (sf project retrieve)
  ↓ Phase 2: export reference data as JSON tree / CSV (sf data export)
Local repo
  ↓ Phase 3: init git, commit baseline, push to GitHub, tag v0-source-snapshot
GitHub repo (durable record — source of truth from here forward)
  ↓ Phase 3.5 (optional): feature branch with restyle changes, visual QA loop
Restyled main branch
  ↓ Phase 4: deploy metadata to TARGET org in dependency order (sf project deploy)
  ↓ Phase 5: load data into TARGET in dependency order (sf data import)
  ↓ Phase 6: post-load (publish site, rebuild search index, set buyer passwords, smoke test)
TARGET org now matches main; future changes go through git
```

---

## Phase 1 — Metadata retrieve

**Goal:** local DX project that can recreate the org's customizations.

### Steps

1. Auth source org if not already:
   `sf org login web --alias <SourceAlias>`
2. Generate DX project scaffold:
   `sf project generate --name xc-<client>-source`
3. Build comprehensive `package.xml`. Start with discovery:
   `sf project retrieve preview --target-org <SourceAlias> > preview.json`
   Then assemble `package.xml` covering:
   - `ApexClass`, `ApexTrigger`, `ApexPage`, `ApexComponent`
   - `LightningComponentBundle`, `AuraDefinitionBundle`
   - `Flow`, `FlowDefinition`
   - `CustomObject`, `CustomField`, `ValidationRule`, `RecordType`
   - `Layout`, `CompactLayout`
   - `PermissionSet`, `PermissionSetGroup` (prefer over Profile where possible)
   - `CustomLabel`, `CustomMetadata`, `CustomTab`, `CustomApplication`
   - `EmailTemplate`, `StaticResource`
   - `WebStore`, `WebStoreCatalog`, `WebStorePricebook`, `WebStoreBuyerGroup`
   - `ProductCatalog`, `ProductCategory`, `BuyerGroup`, `CommerceEntitlementPolicy`
   - `Network`, `NetworkBranding`, `SiteDotCom`, `CustomSite`
   - `ExperienceBundle` (the actual page/theme/nav definition)
   - `CMSResource` (if custom CMS content present)
   - OmniStudio types (`OmniScript`, `OmniDataTransform`, `FlexCard`) if used for Service Cloud forms
4. Run retrieve:
   `sf project retrieve start --manifest package.xml --target-org <SourceAlias>`
5. Survey what came back. Specifically check storefront-relevant pieces are present:
   - `force-app/main/default/experiences/<SiteName>/`
   - `force-app/main/default/staticresources/`
   - `force-app/main/default/lwc/`, `aura/`
   - `force-app/main/default/themes/`, `brandingSets/`

### Risks / triage points

- **Profile bloat.** Profile XML references every field/layout/tab. Often easier to `.forceignore` Profiles entirely and rely on PermissionSets.
- **Hardcoded IDs in Apex/LWC/Flow.** Survives retrieve fine but breaks at runtime in target. Need a scrub pass before commit.
- **Hardcoded URLs / instance refs.** Same.
- **Managed package metadata.** Don't commit it — `.forceignore` it. Target needs the package installed separately.

### Deliverable

DX project under `xc-<client>-source/force-app/` with all custom metadata.

---

## Phase 2 — Data export

**Goal:** reference-data fixtures under `data/` that can rehydrate a target org.

### Approach by volume

| Tool | When to use |
|---|---|
| `sf data export tree --plan` | Relationship-heavy reference data, < 2K records per query. Default for B2B Commerce core. |
| `sf data export bulk` | Large flat tables, > 10K records. CSV output. |
| `sf data query --result-format csv` | Ad-hoc one-offs, small seed sets. |

### Object order (for B2B Commerce)

Export in dependency order so import can replay it:

1. `Account` (filter `IsBuyer = true` if applicable)
2. `Contact`
3. `BuyerAccount`, `BuyerGroup`, `BuyerGroupMember`
4. `Pricebook2`
5. `Product2`
6. `PricebookEntry` (depends on Product2 + Pricebook2)
7. `ProductCatalog`
8. `ProductCategory`
9. `ProductCategoryProduct`
10. `CommerceEntitlementPolicy`, `CommerceEntitlementPolicyProduct`, `CommerceEntitlementBuyerGroup`
11. `WebStoreCatalog`, `WebStorePricebook`, `WebStoreBuyerGroup`
12. *(skip OrderSummary/OrderItemSummary unless explicitly needed — order history rarely worth migrating)*

### Pattern (per object)

```
sf data export tree \
  --query "SELECT Id, Name, ProductCode, Family, IsActive FROM Product2 WHERE IsActive = true" \
  --output-dir data/export/products \
  --target-org <SourceAlias> \
  --plan
```

The `--plan` flag generates a `plan.json` that drives the import side later. Reference IDs in the JSON files preserve relationships across orgs.

### Deliverable

`data/export/<object>/` directories per object, plus a `data/manifest.json` documenting:
- Exact load order
- Queries used per object
- Expected record counts (for import-side verification)

---

## Phase 3 — Initialize git and push to GitHub

### Steps

1. `git init` in the DX project root
2. Write `.gitignore` excluding `.sfdx/`, `*.log`, `node_modules/`, `.vscode/`
3. Write `.forceignore` excluding:
   - `**/profiles/**` (unless explicitly maintaining specific profiles)
   - `**/installedPackages/**`
   - Anything org-specific (e.g., `connectedApps/` if secrets are embedded)
4. Create GitHub repo via `mcp__github__create_repository` (or whatever scope is agreed — personal vs `xcentium` org)
5. Initial commit:
   - `force-app/` — all metadata
   - `data/` — all exports + manifest
   - `package.xml` — the manifest used for retrieve
   - README documenting the source org, retrieve date, and intended target
6. Push, tag `v0-source-snapshot-YYYY-MM-DD`
7. Add a GitHub Actions workflow that runs `sf project deploy validate` against a scratch org (or a designated validation sandbox) on every PR. This is the safety net.

### Deliverable

Public/private GitHub repo with metadata + data baseline, CI passing.

---

## Phase 3.5 (optional) — Restyle on a feature branch

Only if restyle is part of this engagement. See three tiers:

**Tier 1 — Token swap (1-2 days):** new colors/fonts/spacing via `themes/`, `brandingSets/`, custom CSS static resource. No structural changes.

**Tier 2 — Component restyle (3-7 days):** new header/footer/cards via custom LWCs that wrap or replace stock components.

**Tier 3 — Page-level redesign (1-3 weeks):** new pages, new component composition, ExperienceBundle JSON edits.

Each restyle scope = its own ticket through the harness. PR includes screenshots, deploys to a non-prod sandbox first for visual QA via the `browser` skill, merges to main when approved.

---

## Phase 4 — Metadata deploy to target

### Pre-flight

- Auth target org: `sf org login web --alias <TargetAlias>`
- Confirm prerequisite features enabled on target (B2B Commerce, Communities, OmniStudio if used)
- Install required managed packages on target manually before deploy
- Verify admin user has DeployMetadata permission

### Deploy in dependency order

The `salesforce-dev-loop` skill already encodes this iteration loop, but the dependency order is:

1. Custom objects + fields (`CustomObject` + `CustomField`)
2. Apex (classes, triggers, test classes — with ≥75% coverage check)
3. LWCs and Aura
4. Flows
5. Permission sets
6. WebStore + ProductCatalog + BuyerGroup shells
7. ExperienceBundle (depends on WebStore existing)
8. Site activation (manual UI step on first deploy to fresh org — known limitation)
9. Page-level metadata + navigation
10. Site publish (scriptable via `ConnectApi.SitePublish.publish()`)

### Triage patterns (from prior SF work)

| Error | Fix |
|---|---|
| `Component depends on X which doesn't exist` | Deploy X first, then retry |
| `Field type cannot be changed` | Field exists in target with different type — manually drop or align |
| `Profile X doesn't exist` | Create or remove the reference |
| `Test coverage 67%, need 75%` | Harness writes missing tests via `salesforce-dev-loop` |
| `Cannot activate site via metadata` | Manual step in Setup, then re-run deploy |

### Deliverable

Target org has all custom code/objects/components. Storefront *shell* exists but has no data.

---

## Phase 5 — Data load into target

### Pattern (per object, in same order as export)

```
sf data import tree \
  --plan data/export/products/plan.json \
  --target-org <TargetAlias>
```

Plan file's `@reference` mechanism resolves Product2 → PricebookEntry relationships even though IDs differ in the new org.

For Bulk API loads:
```
sf data import bulk \
  --sobject Product2 \
  --file data/export/products/Product2.csv \
  --target-org <TargetAlias>
```

### Pre-flight

- **Disable automation during bulk import.** Active flows, triggers, validation rules can fire on insert and produce unexpected state. Disable, import, re-enable.
- **Confirm "Set Audit Fields upon Record Creation" permission** if you need `CreatedDate`/`CreatedById` preserved. Usually not enabled — accept that timestamps reset to import time.
- **OwnerId scrub.** Any record with `OwnerId` referencing a source-org user that doesn't exist in target needs OwnerId cleared (defaults to running user) or remapped.

### Deliverable

Target org has products, catalog, pricebook, buyer groups, entitlements. Storefront has data to render.

---

## Phase 6 — Post-load

### Required steps before storefront works

1. **Search index rebuild.** Products in DB don't show in storefront search until indexed. Trigger via:
   ```
   sf data update record --sobject WebStore --record-id <id> --values "..."
   ```
   or via Connect API. Async — wait + verify.
2. **Site publish.** `ConnectApi.SitePublish.publish('<networkId>')` from anonymous Apex.
3. **Buyer user creation + password.** Since we're not migrating users:
   - Create buyer users in target via metadata or seed Apex script
   - Set passwords via `System.setPassword('005xxx', 'Initial2026!')` — see `~/.claude/rules/salesforce.md` for the pattern (avoid `!` in passwords due to shell escaping)
4. **Sharing recalculation** if any custom sharing rules deployed.
5. **Storefront cache.** Even after all above, may serve cached pages for several minutes. Plan smoke test accordingly.

### Smoke test

Browser-drive target storefront via the `browser` skill:
- Log in as a buyer user
- Browse catalog, verify products appear
- Verify pricing matches source
- Add to cart, walk through checkout (no payment capture needed for verification)
- Check order history page renders

### Deliverable

Working storefront on target, parity with source for the data scope migrated.

---

## Confidence per phase

| Phase | First-pass autonomous | With me iterating + Thomas available |
|---|---|---|
| 1. Metadata retrieve | 90% | 95% |
| 2. Data export | 85% | 95% |
| 3. Init git + push | 95% | 98% |
| 3.5. Restyle (T1) | 80% | 90% |
| 3.5. Restyle (T2) | 65% | 80% |
| 3.5. Restyle (T3) | 50% | 70% |
| 4. Metadata deploy | 70% | 85% |
| 5. Data load | 75% | 85% |
| 6. Post-load | 80% | 90% |
| **End-to-end** | **50-60%** | **80-85%** |

Lower numbers reflect visual-judgement and error-iteration realities, not technical capability.

---

## Recommended execution shape (through the harness)

Three (or four with restyle) sequential tickets:

**Ticket 1: Snapshot source org into git**
Phase 1 + 2 + 3. Output: GitHub repo with metadata under `force-app/`, data exports under `data/`, manifest, CI green. Tag `v0-source-snapshot`.

**Ticket 2 (optional): Restyle pass on feature branch**
Phase 3.5. Output: feature branch with restyle changes, deployed to a preview sandbox, visual QA screenshots, PR ready.

**Ticket 3: Deploy metadata to TargetOrg**
Phase 4. Output: deployment log, errors resolved via `salesforce-dev-loop`, target org has all custom code/objects/components.

**Ticket 4: Load data into TargetOrg + post-load**
Phase 5 + 6. Output: data load log, post-load Apex run log, smoke test result with screenshots.

Each ticket gets its own PR. Each phase is independently re-runnable.

---

## Decisions still needed when we resume

1. **Source org alias** — confirm. (Default: `HRSandbox` since it's already authenticated.)
2. **Target org alias** — needs to be authenticated.
3. **GitHub repo name + scope** — `xcthomaswagner/...` personal, or somewhere under an XCentium org?
4. **Data scope** — "everything moveable" or specific objects only? Recommend: minimum viable (Products + Catalog + Pricebook + BuyerGroup + a few test buyer accounts).
5. **Restyle in scope?** If yes, which tier and what's the brand direction?
6. **Preview sandbox** for restyle visual QA — same as source, or separate?

---

## Out of scope (intentional)

- User migration. Target starts with admin; buyer users created in target via metadata + seed.
- OrderSummary / OrderItemSummary migration. Order history typically doesn't warrant cross-org migration; create test orders fresh in target if needed.
- Connected App secrets, Named Credential auth tokens. Document; recreate in target manually.
- Production deployment. This playbook covers sandbox-to-sandbox. Prod deployment adds change set / quick-deploy / release management considerations not covered here.

---

## Related references

- `~/.claude/rules/salesforce.md` — global SF CLI patterns, password reset via Apex, B2B Commerce object reference
- `~/.claude/projects/-Users-thomaswagner-Desktop-Projects-nosync-agent-harness/memory/sf_mcp_use_it.md` — when to prefer SF MCP over raw CLI
- `~/.claude/projects/-Users-thomaswagner-Desktop-Projects-nosync-agent-harness/memory/session_2026_04_10_p0_p2_sf_live.md` — first live SF ticket through harness, validated approach
- `runtime/platform-profiles/salesforce/skills/salesforce-dev-loop/` — the harness skill that drives the deploy + test iteration loop
- `runtime/platform-profiles/salesforce/harness-mcp.json` — SF MCP injection config

---

## Next action when we pick this back up

Read the "Decisions still needed" section above. Get answers from Thomas. Then either:

- **Start interactively in a Claude session** with Phase 1 against the source org (fastest feedback, exploratory)
- **Or write Ticket 1** in ADO under the relevant project profile and let the harness pick it up (durable audit trail, dogfoods the system)

Default recommendation: **Phase 1 interactively to produce the inventory**, then **Phases 2-6 as harness tickets** for the audit trail.
