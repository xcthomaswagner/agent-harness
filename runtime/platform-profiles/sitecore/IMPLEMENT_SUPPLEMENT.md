# Sitecore Implementation Supplement

## JSS / Next.js Components

### Component Patterns
- Components live in `src/components/` organized by Sitecore rendering names
- Each component has a corresponding Sitecore rendering definition
- Use `ComponentRendering` or `ComponentParams` types from `@sitecore-jss/sitecore-jss-nextjs`
- Props come from Sitecore layout data, not from parent React components

### Field Rendering
- Always use JSS field helper components for editable fields:
  - `<Text field={fields.title} />` not `<p>{fields.title.value}</p>`
  - `<RichText field={fields.body} />`
  - `<Image field={fields.image} />`
  - `<Link field={fields.link} />`
- This ensures Experience Editor / Pages editing works correctly

### Data Fetching
- Use `getStaticProps` or `getServerSideProps` with `SitecorePagePropsFactory`
- GraphQL queries use the Sitecore GraphQL endpoint
- Content resolvers follow the pattern in `src/lib/page-props-factory/`
- Do NOT use client-side data fetching for content from Sitecore

### Layout Service
- Layout data comes from the Sitecore Layout Service
- Placeholders are defined in the layout response
- Dynamic placeholders use `{componentId}-{placeholder-name}` naming

## .NET + React Hybrid

When both .NET and React coexist:
- .NET controllers handle routing and server-side logic
- React components handle the interactive UI
- Communication is via API endpoints or serialized props
- Do NOT mix .NET view rendering with React component rendering

## Serialization

- **Sitecore Content Serialization (SCS):** Items in `.module.json` + YAML files under `serialization/`
- **Unicorn:** Legacy — items in `.yml` files under `serialization/`
- Always check which serialization tool the project uses before modifying items
- Never manually edit serialized item files — use Sitecore CLI or Unicorn sync
