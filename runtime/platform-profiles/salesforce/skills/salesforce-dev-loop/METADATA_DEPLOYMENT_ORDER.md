# Metadata Deployment Order

Salesforce metadata has dependencies. Deploy them in the wrong order and the deploy fails even when every individual file is valid. The platform does some dependency resolution automatically, but it has blind spots — especially with Agentforce metadata, custom fields, and record types.

## The Core Rule

**If metadata type A references metadata type B, deploy B first.**

This sounds obvious until you realize that in a single `sf_deploy` call the platform tries to deploy everything in parallel and only figures out dependencies when validation fails. For most dependency chains it gets it right. For a few — listed below — it doesn't, and you have to split the deploy into ordered sub-deploys.

## When a Single Deploy Is Fine

For ordinary Apex/LWC/custom field work, a single `sf_deploy(sourcePath: "force-app/")` works. The platform figures out the dependency graph. Don't overthink this — only split deploys when you hit a specific failure described below.

## Agentforce: Ordered Deployment Required

Agentforce metadata **must** be deployed in a specific order because the platform's dependency resolver doesn't fully understand the Bot → Plugin → Function chain.

### The canonical order

```
1. ApexClass         (Apex actions referenced by GenAiFunctions)
2. GenAiPromptTemplate
3. GenAiFunction     (must exist before plugins/bundles reference them)
4. GenAiPlugin       (the "topics" — must exist before bundles)
5. GenAiPlannerBundle
6. Bot + BotVersion
```

**Why this order:**
- A `GenAiFunction` of type `apex` references an Apex class. That class must exist first.
- A `GenAiPlugin` lists `GenAiFunction` names in its action set. Those functions must exist first.
- A `GenAiPlannerBundle` references `GenAiPlugin` names. Plugins must exist first.
- A `Bot` references a `GenAiPlannerBundle`. The bundle must exist first.

### How to deploy in order

If a single `sf_deploy(sourcePath: "force-app/")` fails with `Invalid reference to GenAiFunction: X`, split the deploy:

```
# Step 1: Deploy Apex + Prompt Templates first
sf_deploy(
  sourcePath: "force-app/main/default/classes/,force-app/main/default/genAiPromptTemplates/",
  testLevel: "NoTestRun",
  checkOnly: false
)

# Step 2: Deploy Functions
sf_deploy(
  sourcePath: "force-app/main/default/genAiFunctions/",
  testLevel: "NoTestRun",
  checkOnly: false
)

# Step 3: Deploy Plugins
sf_deploy(
  sourcePath: "force-app/main/default/genAiPlugins/",
  testLevel: "NoTestRun",
  checkOnly: false
)

# Step 4: Deploy PlannerBundles
sf_deploy(
  sourcePath: "force-app/main/default/genAiPlannerBundles/",
  testLevel: "NoTestRun",
  checkOnly: false
)

# Step 5: Deploy Bots + BotVersions
sf_deploy(
  sourcePath: "force-app/main/default/bots/",
  testLevel: "NoTestRun",
  checkOnly: false
)
```

Each step should pass validate (`checkOnly: true`) before you run the real deploy. Don't chain real deploys — a failure in step 3 leaves the org in a partial state.

## The `schema.json` Gotcha

`GenAiFunction` definitions include an `input/schema.json` and `output/schema.json` that define the LLM-facing shape of the action's parameters. The metadata API **ignores changes to `schema.json` unless the corresponding `.genAiFunction-meta.xml` also changes.**

**Symptom:** You edit `input/schema.json`, run `sf_deploy`, the deploy succeeds with `numberComponentsDeployed: 0`, and the scratch org still has the old schema.

**Fix:** Any edit to the `.genAiFunction-meta.xml` file will force the deploy to pick up the schema change. The smallest viable edit is whitespace or a version bump:

```xml
<!-- Before -->
<apiVersion>60.0</apiVersion>

<!-- After -->
<apiVersion>60.0</apiVersion>
<!-- schema refresh 2026-04-10 -->
```

Or just touch a version field if there is one. Committing a whitespace-only change to XML is legitimate in this context — note it in the PR description so reviewers understand why.

## Other Ordering Pitfalls

### Custom Fields Before Record Types

If you add a picklist field AND a record type that filters that picklist's values, the record type references the field. The platform usually resolves this, but if you see `Invalid field: X on Y` on the record type, split:

1. Deploy the field first.
2. Then deploy the record type.

### Custom Settings Before Classes That Read Them

Apex code that references a custom setting type (`Your_Setting__c`) will fail compile if the custom setting doesn't exist on the target. Deploy the custom setting metadata first.

### Translations and Profiles Last

`Profile` and `Translations` metadata reference almost everything else (fields, objects, tabs, apps, record types, permissions). Deploy these last, or use the `.forceignore` file to exclude them from the ticket's deploy if they aren't changing.

### Layouts After Fields

Page layouts reference fields. Deploying a layout that references a field not yet on the target fails. Deploy the field first.

### Flows That Reference Apex

Flows calling `InvocableMethod` or subflows reference other metadata. Deploy the callees (Apex classes, subflows) first, then the calling flow.

### Permission Sets After Everything

Permission sets reference fields, objects, tabs, apex classes, record types, and custom permissions. They should be deployed last, after everything they reference exists.

## `.forceignore` — When to Use It

The `.forceignore` file at the repo root tells `sf project deploy` which files to skip. Use it to:

- Exclude metadata that shouldn't be part of the ticket deploy (profiles, translations, etc.).
- Exclude managed package metadata that can't be modified.
- Exclude auto-generated files that shouldn't round-trip.

**Do not add `.forceignore` entries as a way to hide broken metadata.** If a file fails to deploy, fix it — don't ignore it. The `.forceignore` is for "this deploy doesn't touch X," not "X is broken and I'm hiding it."

## Debugging Ordering Failures

When you get an ordering failure, the error message tells you which type failed. The fix is always:

1. Identify the dependent type (the one that failed).
2. Identify what it depends on (read the error — it usually names the missing reference).
3. Deploy the dependency first, then the dependent type.
4. If you can't figure out the dependency from the error, retrieve the failing metadata's XML and search for references to other types.

## A Decision Tree for "Do I Need to Split the Deploy?"

```
Is this an Agentforce ticket (Bot, Plugin, Function, PlannerBundle)?
├── YES → Split the deploy. Use the 5-step order above. Always.
└── NO  → Try a single deploy first.
            ├── Passed → You're done, no split needed.
            └── Failed with dependency error
                ├── Read the error, identify the dependent/dependency pair.
                ├── Deploy the dependency alone first.
                ├── Then deploy the rest.
                └── If that still fails, there are multiple dependencies —
                    split into more steps in dependency order.
```

## Never Do This

- **Never deploy from `force-app/` with `testLevel: RunLocalTests` as your first attempt** on a fresh scratch org with metadata ordering problems. The deploy will run tests against a broken state and waste 10+ minutes before failing.
- **Never use `sf project deploy cancel`** mid-deploy unless you absolutely have to. Canceled deploys can leave the org in a weird state that's hard to recover from. Let the deploy finish (even if failing) and then fix forward.
- **Never disable dependency checks.** There's no such flag, but people sometimes try creative workarounds (editing files to remove references temporarily). Don't. The references exist because they're load-bearing.
