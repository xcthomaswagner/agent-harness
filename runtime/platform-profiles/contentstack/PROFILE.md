# ContentStack Platform Profile

## Activation Rules

This profile activates when any of these are detected:
- `package.json` contains `@contentstack/*` or `contentstack` dependency
- `package.json` contains `next` and `.mcp.json` contains a `contentstack` server
- `.env*` references `CONTENTSTACK_API_KEY` or `CONTENTSTACK_DELIVERY_TOKEN`
- Client Profile explicitly specifies `platform_profile: contentstack`

## Frontend Target

The default frontend target for this profile is **Next.js App Router +
TypeScript**. Treat Spartacus/Angular as out of scope unless the client profile
or ticket explicitly overrides this profile.

## Profile Contents

| File | Injected Into |
|------|--------------|
| `IMPLEMENT_SUPPLEMENT.md` | `/implement` skill |
| `CODE_REVIEW_SUPPLEMENT.md` | `/code-review` skill |
| `QA_SUPPLEMENT.md` | `/qa-validation` skill |
| `CONVENTIONS.md` | Copied to `/implement/CONVENTIONS.md` |
| `REFERENCE_URLS.md` | Appended to implement, review, and QA skills |
| `STACK_CONTEXT_TEMPLATE.md` | Reference template for stack inventory notes |
| `harness-mcp.json` | Merged into agent MCP config |

## ContentStack Surface Covered

- Headless CMS (entries, assets, content types, taxonomies, locales)
- CDA (Content Delivery API) and CMA (Content Management API)
- Visual Builder and Live Preview for Next.js
- Launch (deployment) and Personalize, when a ticket explicitly touches them
- Branching / aliases (Enterprise feature)
- Next.js App Router routes, Server Components, API route/BFF code, image
  configuration, and preview/draft-mode plumbing

## MCP

This profile bundles the official `@contentstack/mcp` server. See `harness-mcp.json`.
The agent gets full CRUD across the stack — destructive operations require explicit
human confirmation per the code-review supplement.

The generated worktree `.mcp.json` intentionally does not persist
`CONTENTSTACK_API_KEY`, `CONTENTSTACK_DELIVERY_TOKEN`, or
`CONTENTSTACK_MANAGEMENT_TOKEN`. `spawn_team.py` passes those values through the
Claude process environment for the MCP child process to inherit, and it fails
fast when the required API key, delivery token, or region is missing. Use
`scripts/smoke-test-contentstack-mcp.sh` as the full MCP doctor before dispatching
important ContentStack tickets.

## Runtime Posture

Contentstack tickets are not generic React tickets. A valid run needs all of:

- Stack context: target branch, environment, content type UIDs, field paths,
  sample entries/routes, locales, and asset domains.
- A live `contentstack` MCP session when schema or content state must change.
- A Next.js implementation that keeps Contentstack fetches and non-public
  credentials on the server side, then passes typed data into components.
- QA evidence from both the live stack and the affected Next.js route.

If the ticket asks for a schema/content change and the MCP is unavailable, the
agent should block and report the missing capability instead of landing a
documentation-only workaround.
