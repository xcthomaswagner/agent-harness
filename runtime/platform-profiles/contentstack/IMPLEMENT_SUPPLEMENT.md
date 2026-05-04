# ContentStack Implement Supplement

The official `@contentstack/mcp` server is loaded into your session as
the `contentstack` MCP. The harness enables the CMA and CDA groups only.
Treat it as the primary mechanism for any change that touches the live
stack.

This profile assumes **Next.js App Router + TypeScript** for frontend
work. Do not generate Spartacus/Angular code unless the client profile
or ticket explicitly overrides the platform target.

## Hard rules

### 0. Read stack context before changing schema or code

Before implementing a Contentstack ticket, read the stack inventory notes
if present:

- `docs/contentstack-stack-context.md`
- `.harness/contentstack-stack-context.md`
- `.harness/contentstack-stack-context.json`

If no inventory exists and the ticket requires schema/content state, use
the MCP to inspect the live stack first. Identify the target branch,
environment, content type UID, field path, sample entry/slug, locales,
and asset hostnames before choosing names or frontend routes.

### 1. Schema changes MUST be applied via CMA, not documented and skipped

When a ticket calls for a content-type schema change (add/remove/modify
a field, add a modular block, change a reference, change validation,
add/update a global field, etc.), you MUST apply it via the Contentstack
CMA MCP on the configured `CONTENTSTACK_BRANCH`. A migration markdown doc
alone is not sufficient; the live schema must converge before you open
the PR.

Concrete tools you'll typically reach for:

- `mcp__contentstack__get_a_single_content_type` - read current shape before you change it
- `mcp__contentstack__update_content_type` - apply field additions/modifications
- `mcp__contentstack__create_a_content_type` - only when the type genuinely does not exist and the ticket asks for it
- `mcp__contentstack__get_a_single_global_field` / update equivalent - inspect or update reusable field groups

The official `@contentstack/mcp` tool names do not include a `cma_`
prefix. If you cannot see tools such as `get_a_single_content_type` and
`update_content_type`, the MCP did not load correctly; stop and report
the blocker.

If the MCP call fails, that is a delivery blocker. Surface it to the
team lead with the exact error; do not paper over with a doc and call it
done.

A migration markdown file under `contentstack-migration/` is still
valuable as a record of what changed, but it is additional evidence, not
a substitute.

### 2. Confirm MCP readiness before platform writes

For schema/content tickets, verify the Contentstack MCP is connected and
has the required values:

- `CONTENTSTACK_API_KEY`
- `CONTENTSTACK_DELIVERY_TOKEN`
- `CONTENTSTACK_MANAGEMENT_TOKEN` for CMA writes
- `CONTENTSTACK_REGION`
- `CONTENTSTACK_ENVIRONMENT`
- `CONTENTSTACK_BRANCH`

If the MCP server is missing/failed or a required credential is empty,
block the platform portion and report the missing value. Do not produce
"human applies CMS change later" instructions unless the ticket is
explicitly a proposal/runbook ticket.

### 3. Always confirm the target branch before any CMA write

Every CMA write tool accepts a branch parameter. Pass the value of
`CONTENTSTACK_BRANCH` (typically `ai`) explicitly on every write call.
Never let it default. The harness writes to a non-production branch by
design; accidentally writing to `main` is a production incident.

### 4. Verify schema convergence after the write

After applying a schema change, immediately re-fetch the content type or
global field via the matching CMA get tool against the same branch and
confirm the field/block/validation is present with the expected type. If
it is not, the write did not actually take; escalate.

### 5. Frontend type and CMS schema names stay aligned

When you add a field to a Contentstack schema, the corresponding
frontend boundary type MUST represent the same field. Contentstack UIDs
stay snake_case in raw payloads. Next.js page/server code maps
`featured_image` to a presentational prop such as `featuredImage` before
passing it to a React component.

Never hide schema drift by using `any`, wide index signatures, or
untyped spread props at the page/component boundary.

### 6. Reuse the existing Contentstack delivery client

`src/lib/contentstack.ts` (or its equivalent in the client repo) is the
single point of Delivery SDK/REST construction. Do not create a second
stack instance, do not bypass it with raw fetches unless the repo already
uses a REST helper, and do not introduce `@contentstack/management` into
frontend code.

If the client repo does not yet have a centralized SDK module, create
one rather than scattering `contentstack.stack({...})` calls across
routes and components.

### 7. Components stay data-out; Next.js routes own Contentstack reads

Mirror the typed-props + optional-field idiom from
`src/components/blocks/ResourceDownload.tsx` or the existing reference
component. Components receive already-fetched data as typed props.

App Router pages, layouts, route handlers, or server-side library
functions own the Contentstack read. This keeps components testable
without mocking the SDK and keeps server-only credentials out of client
bundles.

### 8. Optional fields render nothing when absent

`subtitle ? <h2>{subtitle}</h2> : null` - not
`<h2>{subtitle ?? ""}</h2>`. The latter emits an empty heading element
which is a layout and accessibility problem. If a field is documented as
optional in the schema, the component must emit zero output when the
value is null, undefined, or empty string. If the ticket lists
"whitespace-only treated as empty" in edge cases, use a trimmed check.

## Next.js implementation notes

### Server/client boundary

Next.js pages and layouts are Server Components by default. Keep
Contentstack API calls, management tokens, preview secrets, Salesforce
tokens, and BFF secrets in server code. Prefer server-side Delivery Token
use for App Router/BFF code too. Only expose a Delivery Token or stack
API key through `NEXT_PUBLIC_` when the existing repo intentionally uses
browser-side CDA and the token is read-only.

Public env vars are bundled for the browser at build time, so never put
management tokens, preview secrets, Salesforce tokens, or BFF secrets
behind that prefix.

### Build-safe Contentstack client loading

Some demo repos construct the Contentstack stack at module load. That can
break `next build` when env vars are intentionally absent in CI or local
test contexts. If you hit that pattern, fix it one of these ways:

- validate env vars inside the server function that performs the fetch;
- lazy-import `src/lib/contentstack.ts` from the route/server function;
- or make the client factory return a clear typed error only when the
  data path is actually exercised.

Do not move the client into a Client Component to avoid the build error.

### Caching and preview

Choose caching behavior deliberately:

- Published marketing content can use stable caching/revalidation when
  the repo has a cache strategy.
- Draft, preview, or Live Preview routes must fetch dynamically so the
  editor sees current draft content.
- If the repo uses Next.js cache tags or `revalidatePath`, include the
  relevant content type UID, entry UID/slug, and route in the tag/path
  naming so content updates can invalidate the correct route.

### Images and assets

When rendering Contentstack assets with `next/image`, configure exact
`images.remotePatterns` entries for the stack's asset/image hostnames.
Pass stable `width` and `height`, or use `fill` inside a parent with a
defined aspect ratio/size, so CMS images do not create layout shift.

### Live Preview / Visual Builder

For Next.js App Router, Live Preview initialization belongs in a
dedicated Client Component and should run once. Route handlers that
enable preview/draft mode must validate a shared secret and resolve the
requested slug before redirecting.

## When NOT to use the CMA MCP

- **Asset writes (image uploads, file uploads).** Out of scope for most
  schema/component tickets. If the ticket genuinely needs a new asset
  uploaded, ask whether a human should do it via the Contentstack UI
  instead; automated asset uploads can produce duplicates.
- **Production environment publish.** The harness writes to the `ai`
  branch. Promoting changes to `main` or any production environment
  requires human review; never call publish-to-production tools.
- **Audit-log inspection.** Use read-oriented tools for state
  verification. Do not perform writes just to inspect state.

## When the schema work is genuinely just docs

A small number of tickets are genuinely documentation-only, for example
a ticket asking you to write a migration runbook for someone else to
execute. In that case the markdown doc IS the deliverable, and you
should NOT call CMA. The signal is in the ticket text: "write a migration
plan," "draft a schema proposal," "document the steps to add X." If the
ticket says "add field X" or "extend type Y," the schema write is part
of done.

When in doubt, default to applying the change on the configured branch.
A reverted CMA write is recoverable; a ticket marked done with the
schema unchanged is silent failure.

## Tool selection patterns

These are mined from real ticket runs and official platform guidance:

- Content type field/block change: get content type -> update schema on
  `CONTENTSTACK_BRANCH` -> refetch same content type -> update Next.js
  route/component/types -> build/test.
- Reusable field group change: get global field -> update global field
  on `CONTENTSTACK_BRANCH` -> refetch -> confirm consuming content types
  still resolve.
- Page rendering change: fetch known entry/slug from CDA -> map raw
  snake_case payload to component props in `src/app/**/page.tsx` ->
  component unit tests -> dev-server screenshot.
- Live Preview change: verify preview token/route config -> initialize
  Live Preview in a Client Component -> route handler validates secret
  and slug -> dynamic fetch path tested.
