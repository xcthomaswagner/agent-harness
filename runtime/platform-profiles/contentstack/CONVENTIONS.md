# ContentStack Next.js Conventions

The default client shape for this profile is a Next.js App Router,
TypeScript, pnpm project backed by Contentstack. These conventions are the
baseline for implementation, review, and QA unless the client repo gives a
stricter local rule.

## 1. Stack Inventory / Real Branch Context

Before changing code or schema, read the local stack context if present:

- `docs/contentstack-stack-context.md`
- `.harness/contentstack-stack-context.md`
- `.harness/contentstack-stack-context.json`

If no context file exists, build one from the live stack before making schema
decisions. Capture at least:

- `CONTENTSTACK_BRANCH` and `CONTENTSTACK_ENVIRONMENT`
- content type UIDs and display names
- global field UIDs and where they are used
- modular block field UIDs, block UIDs, and field paths
- known entry slugs/routes for QA
- locales and fallback behavior that matter to the ticket
- asset hostnames used by Contentstack images/files
- Visual Builder / Live Preview routes or preview tokens, if configured

Do not infer field paths from component names alone. Existing Contentstack UID
and branch state wins over a plausible generated name.

## 2. Content Modeling Conventions

Use the ticket, design shape, and current stack model to decide where content
belongs:

- Use **Modular Blocks** for author-composable page sections that editors add,
  remove, or reorder inside a page.
- Use **Global Fields** for repeated field groups such as SEO metadata, CTA
  shapes, image-with-alt structures, or shared merchandising metadata.
- Use **Reference Fields** for reusable entries or domain entities that should
  not be duplicated into every page entry.
- Use **Group Fields** for tightly related data that only exists inside one
  parent entry or block.
- Use **RTE fields** only for authored rich body copy. Do not put structured
  data into rich text when the frontend needs typed fields.
- Do not hard-code Entry UIDs in Next.js code. Prefer stable slugs, URL fields,
  product keys, or other immutable business identifiers.

Naming:

| Item | Convention | Example |
|------|------------|---------|
| Content type UID | snake_case noun | `landing_page` |
| Modular block UID | snake_case component role | `resource_download` |
| Contentstack field UID | snake_case | `featured_image` |
| React component | PascalCase | `ResourceDownload.tsx` |
| React prop | camelCase after route mapping | `featuredImage` |
| Route segment | kebab-case or dynamic segment | `app/blog/[slug]/page.tsx` |

The Contentstack UID stays snake_case. The mapping from `featured_image` to
`featuredImage` happens at the Next.js page/server boundary, not inside the
presentational component.

## 3. Hard Contentstack Platform Gate

Contentstack schema and content work is platform work, not generic frontend
work. If a ticket asks to add, remove, or alter content types, global fields,
modular blocks, validations, entries, assets, or environment-specific content,
the agent must:

- use the Contentstack MCP/CMA path for live writes;
- target the configured branch and environment explicitly;
- refetch the changed module after writing; and
- include verification evidence in the PR notes or QA matrix.

A migration markdown file is useful evidence, but it is not the implementation
unless the ticket explicitly asks only for a proposal or runbook.

## 4. MCP Readiness

A Contentstack implementation that needs live schema/content state requires a
working `contentstack` MCP server and populated environment values:

- `CONTENTSTACK_API_KEY`
- `CONTENTSTACK_DELIVERY_TOKEN`
- `CONTENTSTACK_MANAGEMENT_TOKEN` for CMA writes
- `CONTENTSTACK_REGION`
- `CONTENTSTACK_ENVIRONMENT`
- `CONTENTSTACK_BRANCH`

If the MCP is absent, failed, or missing credentials, block the platform portion
and report the exact failure. Do not silently downgrade to "human applies CMS
change later" unless the ticket is explicitly documentation-only.

## 5. Next.js Frontend Target

Preferred layout:

```text
src/
  app/                    # App Router routes, layouts, route handlers
  components/
    blocks/               # Contentstack modular block renderers
  lib/
    contentstack.ts       # single Delivery SDK / REST client construction point
    contentstack-types.ts # shared Contentstack payload types when useful
```

Next.js rules:

- App Router pages, layouts, route handlers, or server-side library functions
  own Contentstack reads. Presentational block components receive typed props.
- Keep Management Tokens, preview secrets, and BFF secrets on the server side.
  Prefer server-side Delivery Token use for App Router/BFF code too; only expose
  a Delivery Token or stack API key through `NEXT_PUBLIC_` when the existing
  repo intentionally uses browser-side CDA and the token is read-only.
- Client Components are for interactivity, browser APIs, and Live Preview
  initialization. They must not import a module that constructs a server-only
  Contentstack client or reads non-public env vars.
- Use the existing `src/lib/contentstack.ts` client. If it constructs the stack
  at module load and `next build` runs without env vars, either make the module
  build-safe or import it lazily from the server route that needs it.
- Choose caching deliberately. Published content can be cached or revalidated;
  preview/draft content must fetch dynamically.
- If rendering Contentstack assets with `next/image`, add exact
  `images.remotePatterns` for the stack's asset hostnames and pass width/height
  or `fill` with stable layout constraints.
- Live Preview for App Router belongs in a dedicated client initializer so it
  runs once and does not reset during server/client re-renders.
- Draft/preview route handlers must validate a shared secret and the target
  slug before enabling draft mode or redirecting.

## 6. QA Oracles

Every Contentstack ticket should name concrete oracles, not just "build passes":

- live content type/global field payload on `CONTENTSTACK_BRANCH`
- CDA fetch against `CONTENTSTACK_ENVIRONMENT`
- known entry UID or slug used for route verification
- affected Next.js route, including dynamic params
- expected empty/optional field behavior
- expected asset rendering behavior
- preview/draft behavior if Live Preview changed

For UI changes, run the dev server against a known route and capture a
screenshot. If no authored entry exists for the affected content type or block,
QA records SKIP with the reason; it is not a PASS.
