# ContentStack Stack Context Template

Create or update this file in the client repo as
`docs/contentstack-stack-context.md` or `.harness/contentstack-stack-context.md`
when a run needs real Contentstack structure.

## Runtime Scope

- Stack name:
- Region:
- Branch (`CONTENTSTACK_BRANCH`):
- Environment (`CONTENTSTACK_ENVIRONMENT`):
- Default locale:
- Additional locales / fallback behavior:
- API style used by frontend: Delivery SDK / REST / GraphQL
- Next.js version and router: App Router / Pages Router

## Next.js Integration

- Contentstack client module:
- Contentstack type module:
- App routes fed by Contentstack:
- Route params / slug source:
- Live Preview route:
- Draft mode enable route:
- Draft mode disable route:
- Visual Builder initializer component:
- Asset hostnames required by `next/image`:

## Content Types

| UID | Display Name | Single/Multiple | Route(s) | Key Fields | Notes |
|-----|--------------|-----------------|----------|------------|-------|
| `page` | Page | Multiple | `/[slug]` | `title`, `url`, `page_components` | |

## Global Fields

| UID | Display Name | Used By | Field Summary | Notes |
|-----|--------------|---------|---------------|-------|
| `seo` | SEO | `page`, `blog_post` | `meta_title`, `meta_description` | |

## Modular Blocks

| Parent Type | Modular Field UID | Block UID | React Component | Field Summary |
|-------------|-------------------|-----------|-----------------|---------------|
| `page` | `page_components` | `hero_banner` | `HeroBanner` | `heading`, `body`, `image`, `cta` |

## References / Domain Entities

| Field Path | References | Max Items | Frontend Lookup | Notes |
|------------|------------|-----------|-----------------|-------|
| `page.page_components.product_grid.products` | `product` | 4 | product key / slug | |

## Sample Entries For QA

| Content Type | Entry UID | Slug / URL | Locale | Published? | Route To Test |
|--------------|-----------|------------|--------|------------|---------------|
| `page` | | `/demo` | `en-us` | yes/no | `/demo` |

## Asset Examples

| Asset UID | URL Hostname | Used By | Width/Height Known? | Notes |
|-----------|--------------|---------|---------------------|-------|
| | `images.contentstack.io` | | yes/no | |

## Current Ticket Notes

- Ticket ID:
- Existing field/block being changed:
- New field/block being added:
- CMA verification command/tool:
- CDA verification entry/route:
- Next.js route/component touched:
- Edge cases:
