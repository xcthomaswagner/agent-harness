# Sitecore Code Conventions

## File Organization (Helix)

```
src/
  Foundation/          # Cross-cutting concerns (serialization, DI, extensions)
  Feature/             # Business features (navigation, search, content)
  Project/             # Site-specific aggregation and configuration
```

For JSS/Next.js projects:
```
src/
  components/          # React components (mapped to Sitecore renderings)
  lib/                 # Utilities, data fetching, content resolvers
  pages/               # Next.js pages (or app/ for App Router)
  graphql/             # GraphQL queries and fragments
  temp/                # Generated files (component factory, GraphQL types)
```

## Naming

| Item | Convention | Example |
|------|-----------|---------|
| React components | PascalCase | `HeroBanner.tsx` |
| Sitecore renderings | PascalCase, match component name | `HeroBanner` |
| Placeholder keys | kebab-case | `jss-hero-content` |
| GraphQL queries | PascalCase + `Query` suffix | `HeroBannerQuery` |
| Content resolvers | PascalCase + `ContentResolver` | `HeroBannerContentResolver` |
| .NET controllers | PascalCase + `Controller` | `NavigationController.cs` |

## Field Access

```tsx
// CORRECT — editable in Experience Editor
<Text field={props.fields.heading} tag="h1" />
<RichText field={props.fields.body} />
<Image field={props.fields.image} />

// INCORRECT — breaks editing
<h1>{props.fields.heading.value}</h1>
<div dangerouslySetInnerHTML={{__html: props.fields.body.value}} />
```

## Component Props Pattern

```tsx
import { ComponentRendering, ComponentFields } from '@sitecore-jss/sitecore-jss-nextjs';

interface HeroBannerFields extends ComponentFields {
  heading: Field<string>;
  body: Field<string>;
  image: ImageField;
}

type HeroBannerProps = {
  rendering: ComponentRendering;
  fields: HeroBannerFields;
};

const HeroBanner = ({ fields }: HeroBannerProps): JSX.Element => (
  // ...
);

export default HeroBanner;
```
