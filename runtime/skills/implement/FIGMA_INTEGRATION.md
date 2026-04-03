# Figma Design Integration

## When This Applies

This guide applies when the enriched ticket at `.harness/ticket.json` has a non-null `figma_design_spec` field. The design spec was extracted by the L1 analyst and cached — you do NOT need to call the Figma API.

## Reading the Design Spec

The design spec is embedded in the enriched ticket JSON:

```json
{
  "figma_design_spec": {
    "figma_url": "https://www.figma.com/file/...",
    "components": ["Button", "Card", "Avatar", "Header"],
    "layout_patterns": ["Header: horizontal layout", "Content: vertical layout"],
    "color_tokens": {
      "primary": "#1B2A4A",
      "text": "#333333"
    },
    "typography": {
      "heading-1": "Inter 24px/32px Bold",
      "body": "Inter 14px/20px Regular"
    },
    "interactive_states": ["hover: darken 10%", "disabled: opacity 0.5"],
    "responsive_breakpoints": ["mobile: 375px", "tablet: 768px", "desktop: 1280px"]
  }
}
```

## Implementation Guidelines

### 1. Map Design Components to Code Components

Check if the project already has components matching the design:
- Search for existing components: `glob src/components/**/*.{tsx,jsx,vue}`
- If a component exists with the same name, extend it — don't create a duplicate
- If it doesn't exist, create a new component following the project's conventions

### 2. Use Design Tokens

Map Figma color tokens to the project's design system:
- If the project uses CSS custom properties: `var(--color-primary)`
- If Tailwind: map to Tailwind classes or extend `tailwind.config`
- If CSS modules/styled-components: use the hex values directly
- **Never hardcode hex values inline** — always use the project's token system

### 3. Match Typography

Use the Figma typography spec to set:
- Font family (check if it's already imported in the project)
- Font size and line height
- Font weight
- Match to the closest Tailwind class or CSS variable available

### 4. Implement Layout Patterns

The `layout_patterns` field tells you the layout approach:
- `horizontal layout` → flexbox row (`display: flex`)
- `vertical layout` → flexbox column (`flex-direction: column`)
- Grid patterns → CSS Grid or the project's grid system
- Match the project's existing layout approach (Tailwind flex/grid, CSS Grid, etc.)

### 5. Handle Interactive States

If the design spec includes interactive states:
- `hover` → `:hover` pseudo-class or Tailwind `hover:` prefix
- `active` → `:active` pseudo-class
- `disabled` → `disabled` attribute + styles
- `focus` → `:focus-visible` for accessibility

### 6. Responsive Breakpoints

If breakpoints are specified:
- Map to the project's existing breakpoint system (Tailwind's `sm:`, `md:`, `lg:`)
- If custom breakpoints: add to the config or use media queries
- Implement mobile-first: start with the smallest breakpoint, add larger ones

## Code Connect Component Reuse

If the project uses Figma Code Connect (a `.figma.ts` or `.figma.tsx` file):
- Read the Code Connect mappings to understand which Figma components map to which code components
- Use the mapped components directly instead of creating new ones
- Respect the props interface defined in Code Connect

## Design Compliance Verification

After implementing, verify:
- [ ] Colors match the design tokens (no hardcoded values that differ)
- [ ] Typography matches the specified fonts, sizes, and weights
- [ ] Layout structure matches the design's component hierarchy
- [ ] Interactive states are implemented
- [ ] Responsive behavior follows the breakpoints

The QA teammate will run full design compliance checks using `agent-browser` — pixel diffs against Figma exports, computed style verification, and responsive viewport testing. See the QA validation skill (Step 3) for details.
