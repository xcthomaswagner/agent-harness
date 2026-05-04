# ContentStack Code Review Supplement

Review Contentstack tickets as cross-system changes: live Contentstack
state, Next.js code, tests, and PR evidence must agree. The default
frontend target is Next.js App Router + TypeScript.

## Blocking Review Rules

### 1. No doc-only schema implementation

If the ticket asks to add or modify content types, global fields,
modular blocks, validations, entries, assets, or environment content,
the PR must include evidence that the change was applied through
Contentstack CMA on `CONTENTSTACK_BRANCH`. A migration markdown file is
not enough unless the ticket explicitly requested a proposal or runbook.

### 2. Branch and environment must be explicit

Reject PRs that write or verify against an implicit/default branch.
Schema/content work must name the configured `CONTENTSTACK_BRANCH` and
`CONTENTSTACK_ENVIRONMENT` in the implementation notes or QA matrix.
Anything touching `main` or production requires explicit human approval.

### 3. Secrets must stay out of diffs and browser bundles

Reject any PR that commits actual values for:

- `CONTENTSTACK_API_KEY`
- `CONTENTSTACK_DELIVERY_TOKEN`
- `CONTENTSTACK_MANAGEMENT_TOKEN`
- preview/draft secrets
- Salesforce commerce tokens

Also reject management tokens, preview secrets, Salesforce tokens, or BFF
credentials exposed through `NEXT_PUBLIC_`. In Next.js, that prefix makes
the value available to browser code. Delivery Tokens and stack API keys
may be public only when the existing repo intentionally uses browser-side
CDA and the token is read-only.

### 4. Next.js server/client boundary must be respected

Contentstack Delivery SDK/REST reads belong in App Router pages,
layouts, route handlers, or server-side library functions. Presentational
components should receive typed props.

Reject a PR when:

- a Client Component imports a server-only Contentstack client;
- a presentational block component calls the SDK directly;
- a management SDK is added to frontend code;
- a route uses `any` or untyped spreads to hide payload drift; or
- preview/draft routes use cached published-content paths.

### 5. Schema and TypeScript types must converge

For each changed Contentstack field/block:

- raw payload shape uses the Contentstack UID;
- the Next.js route/server layer maps raw snake_case fields to component
  prop names;
- TypeScript types cover null/undefined for optional fields; and
- tests include present and absent field cases.

The reviewer should compare `contentstack-migration/<TICKET>.md`, CMA
verification evidence, route mapping, component props, and tests.

### 6. Content modeling should reuse existing structures

Question new content types, global fields, or blocks that duplicate
existing stack structures. Prefer:

- Modular Blocks for author-composable page sections;
- Global Fields for repeated field groups;
- Reference Fields for reusable entries/domain objects; and
- Group Fields for local structured data.

Reject hard-coded Entry UIDs in Next.js code unless the ticket explicitly
requires a fixed singleton and explains why a slug/key cannot be used.

### 7. Contentstack assets need Next.js image configuration

If the PR renders Contentstack image/file URLs through `next/image`, it
must add exact `images.remotePatterns` for the asset hostnames and use
stable sizing (`width`/`height` or `fill` with a constrained parent).
Reject broad wildcard host patterns that allow arbitrary remote images.

### 8. Live Preview and Visual Builder changes need preview evidence

If the ticket touches Visual Builder or Live Preview:

- initialization should live in a dedicated Client Component;
- draft/preview route handlers must validate a secret and slug;
- preview fetches must bypass published-content caching; and
- QA must record the route/slug used for preview verification.

## Reviewer checklist

- [ ] Ticket platform is Contentstack and frontend target is Next.js.
- [ ] Stack context or MCP inspection identifies the existing content
      model before the change.
- [ ] Live CMA write evidence exists for schema/content AC.
- [ ] CDA or route verification uses `CONTENTSTACK_BRANCH` and
      `CONTENTSTACK_ENVIRONMENT`.
- [ ] Next.js code keeps server-only credentials out of Client
      Components and browser bundles.
- [ ] Components receive typed props and do not construct Contentstack
      clients.
- [ ] Optional fields render no empty semantic wrappers.
- [ ] Tests cover present, absent, and listed edge cases.
- [ ] UI-affecting work has a dev-server screenshot or a justified SKIP.
- [ ] No actual tokens/secrets appear in diffs, logs, or migration docs.
