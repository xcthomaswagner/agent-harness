# Sitecore Code Review Supplement

## Critical Checks

### Experience Editor / Pages Compatibility
- All text fields use `<Text field={...} />` not raw `.value` access
- Rich text uses `<RichText field={...} />`
- Images use `<Image field={...} />`
- Links use `<Link field={...} />`
- Components must render correctly in both normal mode and editing mode

### Serialization Rules
- No manual edits to serialized `.yml` or `.module.json` files
- Serialized items match the expected structure for the project's serialization tool
- Item IDs are not hardcoded in code (use item paths or configured settings)

### Helix Architecture Compliance
- Foundation, Feature, and Project layer separation respected
- No upward dependencies (Foundation doesn't depend on Feature)
- Feature modules don't depend on each other directly
- Project layer aggregates Feature modules

### Security
- No direct database access — use Sitecore API
- No hardcoded Sitecore paths (use configuration or settings items)
- API keys and connection strings not in code (use config transforms or environment variables)

## Warning Checks

### Performance
- GraphQL queries are not overly broad (avoid `search` without proper filters)
- Layout Service responses are not over-fetched
- Images have proper `srcSet` or responsive handling
- No N+1 query patterns in content resolvers

### Conventions
- Component names match their Sitecore rendering names
- File names match component names (PascalCase for components)
- Placeholder keys follow the project's naming convention
