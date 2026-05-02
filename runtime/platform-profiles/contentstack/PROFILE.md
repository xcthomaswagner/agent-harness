# ContentStack Platform Profile

## Activation Rules

This profile activates when any of these are detected:
- `package.json` contains `@contentstack/*` or `contentstack` dependency
- `.env*` references `CONTENTSTACK_API_KEY` or `CONTENTSTACK_DELIVERY_TOKEN`
- Client Profile explicitly specifies `platform_profile: contentstack`

## Profile Contents

| File | Injected Into |
|------|--------------|
| `IMPLEMENT_SUPPLEMENT.md` | `/implement` skill |
| `CODE_REVIEW_SUPPLEMENT.md` | `/code-review` skill |
| `QA_SUPPLEMENT.md` | `/qa-validation` skill |
| `CONVENTIONS.md` | Copied to `/implement/CONVENTIONS.md` |
| `harness-mcp.json` | Merged into agent MCP config |

## ContentStack Surface Covered

- Headless CMS (entries, assets, content types, taxonomies, locales)
- CDA (Content Delivery API) and CMA (Content Management API)
- Launch (deployment) and Personalize
- Branching / aliases (Enterprise feature)
- Live Preview integrations (Next.js, Nuxt, Astro, Gatsby, Angular)

## MCP

This profile bundles the official `@contentstack/mcp` server. See `harness-mcp.json`.
The agent gets full CRUD across the stack — destructive operations require explicit
human confirmation per the code-review supplement.

## Day-One Posture

The supplements (`CONVENTIONS.md`, `IMPLEMENT_SUPPLEMENT.md`,
`CODE_REVIEW_SUPPLEMENT.md`, `QA_SUPPLEMENT.md`) ship as stubs by design. The
official MCP carries the API surface so we don't pre-write rules from
ContentStack docs — wait until real ticket runs surface real gaps, then mine
those into rules. See `runtime/platform-profiles/salesforce/` for the eventual
target shape.
