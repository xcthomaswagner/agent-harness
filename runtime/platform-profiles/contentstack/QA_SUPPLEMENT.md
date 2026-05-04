# ContentStack QA Supplement

QA on a Contentstack ticket means verifying both halves: the live stack
matches what the schema/content migration claimed it would change, AND
the Next.js frontend actually renders the new shape correctly. Without
both, you have either a doc-only PR or a frontend-only PR. Both are
silent failures.

The default frontend target is Next.js App Router + TypeScript.

## Hard rules

### 1. MCP readiness is a QA gate

For any ticket whose AC includes live Contentstack schema/content state,
QA must verify the run had a working `contentstack` MCP and non-empty
required credentials. If implementation skipped CMA/CDA because the MCP
failed, the platform AC is FAIL/BLOCKED, not PASS.

### 2. Schema convergence: verify live shape matches the migration

For any ticket whose AC includes a content-type or global-field schema
change, verify against the live stack on `CONTENTSTACK_BRANCH`:

1. Call `mcp__contentstack__get_a_single_content_type` or the matching global
   field read tool against the configured branch.
2. Confirm every field/block/validation the migration claimed to add or
   modify is present with the documented type, mandatory flag, and
   validation constraints.
3. Confirm no fields were unintentionally removed or renamed.

Diff the result against the `contentstack-migration/<TICKET>.md` file in
the PR. Any mismatch is a QA failure.

### 3. CDA round-trip: confirm a real entry can be fetched

After a schema change, fetch at least one entry of that content type via
the CDA on the configured branch + environment and confirm:

- the entry is retrievable;
- the new field appears on the payload, even if `null` for a new optional
  field;
- the field type matches the Next.js boundary type; and
- the branch/environment in the response or request matches the client
  profile.

If no entries exist in the type yet, that is a SKIP with rationale, not a
PASS.

### 4. Next.js render: exercise the affected route

For tickets that change rendering, do not rely on unit tests alone:

- Run the configured commands, normally `pnpm typecheck`, `pnpm lint`,
  `pnpm test`, and `pnpm build`.
- Run `pnpm dev` when feasible and hit the affected App Router path with
  a known entry slug.
- Capture a screenshot via the agent-browser tool for UI-affecting
  changes.
- Verify optional/empty-field behavior in both unit tests and the live
  route if authored data exists.

The retrospective from RND-89147 flagged "no final screenshot" as a real
QA gap on UI-affecting changes.

### 5. Next.js server/client boundary

QA should reject a PR if:

- a Client Component imports the Contentstack delivery client and that
  client reads non-public env vars;
- a management token, preview secret, Salesforce token, or BFF secret is
  exposed via `NEXT_PUBLIC_`;
- `next/image` renders Contentstack assets without corresponding
  `images.remotePatterns`; or
- preview/draft mode fetches cached published data instead of draft/live
  preview data.

### 6. Branch isolation: never QA against production

QA actions (CMA reads, CDA fetches, dev server runs) MUST target the
branch configured in `CONTENTSTACK_BRANCH` (typically `ai`) and the
environment in `CONTENTSTACK_ENVIRONMENT` (typically `development`).
Never QA against `main` or `production`.

### 7. Edge-case ownership

Every entry in the ticket's `edge_cases` list must end one of two ways:

- **PASS** - a test or manual verification exercised the case and passed.
- **REJECTED** - the QA matrix says why the case is out of scope, with
  reference to the ticket text or AC.

A SKIP without justification is a QA failure.

## Standard checklist for Contentstack Next.js tickets

Run through this checklist before recording a QA verdict:

- [ ] Stack context file or live MCP inspection identifies branch,
      environment, content type UID, field path, sample entry/slug, and
      affected route.
- [ ] CMA read against `CONTENTSTACK_BRANCH` shows the schema as
      documented in `contentstack-migration/<TICKET>.md`.
- [ ] CDA fetch of at least one entry succeeds, or SKIP is recorded with
      "no entries authored yet" reason.
- [ ] `pnpm typecheck` succeeds.
- [ ] `pnpm lint` succeeds.
- [ ] `pnpm test` covers the new render states, including optional/empty
      fields and listed edge cases.
- [ ] `pnpm build` succeeds.
- [ ] Live render verified via dev server + screenshot captured for
      UI-affecting changes.
- [ ] `next/image` remote asset configuration is correct when
      Contentstack assets render through `Image`.
- [ ] Live Preview / draft mode is verified when the ticket touches
      Visual Builder or preview behavior.
- [ ] Every `edge_cases` entry ends in PASS or REJECTED.
- [ ] `pnpm-lock.yaml` change is intentional.

## What to record in the QA matrix

For each functional AC, record the verification path you actually ran,
not just PASS/FAIL. Future debugging depends on knowing whether PASS
meant "ran a CDA fetch and the field was there" or "trusted the unit
test." The retrospective parser reads this output, so be specific:

- **AC-001 (CMA write succeeded):** PASS via
  `get_a_single_content_type` showing `subtitle` on `ai` with
  `data_type: text`, `mandatory: false`.
- **AC-002 (CDA returns new field):** PASS via `get_a_single_entry_cdn` on entry
  `<uid>` returning `subtitle: null`.
- **AC-003 (Next.js route renders field):** PASS via dev-server check at
  `/blog/<slug>` plus screenshot in `.harness/qa/`.
- **AC-004 (empty field emits no wrapper):** PASS via unit test
  `renders_no_subtitle_element_when_undefined` and visual check.
- **AC-005 (preview path):** PASS via draft/preview route with secret
  validation and dynamic Contentstack fetch.

## Patterns mined from real runs

- RND-89147 showed that a green component test is not enough when schema
  application remains manual. QA must prove live Contentstack convergence.
- RND-89147 also showed that UI work needs a final browser screenshot,
  not just a build/test transcript.
