# ContentStack Implement Supplement

The official `@contentstack/mcp` server is loaded into your session as
the `contentstack` MCP. It carries ~126 tools across CMA (writes), CDA
(reads), Launch, and Personalize. You should treat it as your primary
mechanism for any change that touches the live stack.

## Hard rules

### 1. Schema changes MUST be applied via CMA — not documented and skipped

When a ticket calls for a content-type schema change (add/remove/modify
a field, add a modular block, change a reference, etc.), you MUST apply
it via the Contentstack CMA MCP on the configured `CONTENTSTACK_BRANCH`.
A migration markdown doc alone is not sufficient — the live schema must
converge before you open the PR.

Concrete tools you'll typically reach for:

- `mcp__contentstack__cma_get_content_type` — read current shape before you change it
- `mcp__contentstack__cma_update_content_type` — apply field additions / modifications
- `mcp__contentstack__cma_create_content_type` — only when the type genuinely doesn't exist (and the ticket is explicit about creating it)

If the MCP call fails, that is a delivery blocker — surface it to the
team lead with the exact error, do not paper over with a doc and call
it done.

A migration markdown file under `contentstack-migration/` is still
valuable as a record of what changed, but it is *additional* — never a
substitute.

### 2. Always confirm the target branch before any CMA write

Every CMA write tool accepts a branch parameter. You MUST pass the value
of `CONTENTSTACK_BRANCH` (typically `ai`) explicitly on every write
call. Never let it default. The harness writes to a non-production
branch by design — accidentally writing to `main` is a production
incident.

### 3. Verify schema convergence after the write

After applying a schema change, immediately re-fetch the content type
via `mcp__contentstack__cma_get_content_type` against the same branch
and confirm the field is present with the expected type. If it isn't,
the write didn't actually take — escalate.

### 4. Frontend type and CMS schema names stay aligned

When you add a field to a Contentstack schema, the corresponding
TypeScript prop on the frontend component MUST use the same name in
the same case. If the schema field is `subtitle` (snake_case-equivalent
since it's a single word), the prop is `subtitle`. If the schema field
is `featured_image` (snake_case), the boundary in `src/app/.../page.tsx`
maps `featured_image` → `featuredImage` for the component, but the
schema name itself stays as authored.

### 5. Reuse the existing `@contentstack/delivery-sdk` client

`src/lib/contentstack.ts` (or its equivalent in the client repo) is the
single point of SDK construction. Do not create a second stack
instance, do not bypass it with raw fetches, do not introduce
`@contentstack/management` SDK on the frontend (it's CMS-only and
should never end up in a browser bundle).

If the client repo doesn't yet have a centralized SDK module, create
one rather than scattering `contentstack.stack({...})` calls across
components.

### 6. Components stay data-out — no SDK calls inside

Mirror the typed-props + optional-field idiom from
`src/components/blocks/ResourceDownload.tsx` (or whatever the existing
reference component is). Components receive already-fetched data as
typed props. Page-level routes (Next.js App Router pages, etc.) own
the SDK calls. This keeps components testable without mocking the SDK.

### 7. Optional fields render nothing when absent

`subtitle ? <h2>{subtitle}</h2> : null` — not `<h2>{subtitle ?? ""}</h2>`.
The latter emits an empty heading element which is a layout and a11y
problem. If a field is documented as optional in the schema, the
component must emit zero output when the value is null, undefined, or
empty string. If the ticket lists "whitespace-only treated as empty"
in edge cases, use `subtitle?.trim() ? ... : null`.

## When NOT to use the CMA MCP

- **Asset writes (image uploads, file uploads).** Out of scope for
  most schema/component tickets. If the ticket genuinely needs a new
  asset uploaded, ask whether a human should do it via the Contentstack
  UI instead — automated asset uploads can produce duplicates.
- **Production environment publish.** The harness writes to the `ai`
  branch. Promoting changes to `main` (or any production environment)
  requires human review — never call publish-to-production tools.
- **Audit-log inspection.** The CDA MCP is for reads when a ticket
  needs to verify state. Do not call CMA "read" tools just to inspect —
  that's slower than CDA and unnecessary.

## When the schema work is genuinely "just docs"

A small number of tickets are genuinely documentation-only — for
example, a ticket asking you to write a migration runbook for someone
else to execute. In that case the markdown doc IS the deliverable,
and you should NOT call CMA. The signal is in the ticket text: "write
a migration plan," "draft a schema proposal," "document the steps to
add X." If the ticket says "add field X" or "extend type Y," the
schema write is part of done.

When in doubt, default to applying the change. A reverted CMA write
is recoverable; a ticket marked done with the schema unchanged is
silent failure.

## Tool selection patterns

These are mined from real ticket runs. Add new ones as evidence
accumulates.

(none yet — first real CMA-using ticket runs ahead)
