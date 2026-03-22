# Figma Design Extraction

## Figma Link Detection

Scan the ticket's description, acceptance criteria, and attachments for Figma URLs matching these patterns:

- `https://www.figma.com/file/<file_key>/<file_name>?node-id=<node_id>`
- `https://www.figma.com/design/<file_key>/<file_name>?node-id=<node_id>`
- `https://figma.com/file/<file_key>/...`
- `https://www.figma.com/proto/<file_key>/...` (prototype links)

Extract:
- **file_key** — the unique file identifier
- **node_id** — the specific frame/component being referenced (if present)
- **file_name** — the human-readable name (from the URL slug)

## Extraction Process

When a Figma link is detected:

### Step 1: Fetch File Metadata

Use the Figma REST API (or Figma MCP if available):

```
GET https://api.figma.com/v1/files/{file_key}
```

Extract:
- Document name
- Page names and structure
- Component names used in the design

### Step 2: Fetch Specific Node (if node_id provided)

```
GET https://api.figma.com/v1/files/{file_key}/nodes?ids={node_id}
```

Extract from the node tree:
- **Components**: List of component instances with their names
- **Layout**: Flex/grid layout properties, spacing, padding
- **Colors**: Fill colors as hex values, mapped to design token names where possible
- **Typography**: Font family, size, weight, line height for each text element
- **Interactive States**: Identify hover/active/disabled variants if they exist
- **Responsive**: Frame constraints, auto-layout direction, breakpoint hints

### Step 3: Generate Image Exports (optional)

```
GET https://api.figma.com/v1/images/{file_key}?ids={node_id}&format=png&scale=2
```

Save exported images to `/.harness/design/` for reference during implementation.

### Step 4: Produce Design Spec

Output a structured `DesignSpec` object:

```json
{
  "figma_url": "https://www.figma.com/file/abc/...",
  "components": ["Button", "Card", "Avatar", "Header"],
  "layout_patterns": ["flex column", "12-column grid", "sidebar + content"],
  "color_tokens": {
    "primary": "#1B2A4A",
    "secondary": "#2E6CA4",
    "background": "#FFFFFF",
    "text": "#333333"
  },
  "typography": {
    "heading-1": "Inter 24px/32px Bold",
    "body": "Inter 14px/20px Regular",
    "caption": "Inter 12px/16px Regular"
  },
  "interactive_states": ["hover: darken 10%", "disabled: opacity 0.5"],
  "responsive_breakpoints": ["mobile: 375px", "tablet: 768px", "desktop: 1280px"],
  "raw_extraction": "<full node tree text for context>"
}
```

### Step 5: Cross-Reference with Acceptance Criteria

Compare the design against the ticket's acceptance criteria:

- Count screens/frames in the design vs. screens mentioned in the AC
- Identify gaps: "Figma shows 4 screens but AC only describes 2"
- Flag interactive states in the design that have no AC coverage
- Note any design elements that contradict the text requirements

Add findings to `analyst_notes`.

## Caching

The design spec is cached at `/.harness/design-spec.json` in the worktree.
Dev teammates read this cached spec instead of making repeated Figma API calls.

## When No Figma Link Exists

If no Figma link is found in the ticket:
- Set `figma_design_spec` to `null` in the enriched ticket
- Do NOT flag this as an issue — many tickets don't need designs
- Only note it if the ticket type is a UI story with no visual reference

## Figma API Authentication

The Figma API requires a personal access token set as `FIGMA_API_TOKEN` in the environment.
Generate one at: https://www.figma.com/developers/api#access-tokens
