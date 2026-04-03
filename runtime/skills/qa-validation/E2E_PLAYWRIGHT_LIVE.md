# E2E Playwright Live Validation

> **Scope:** This guide covers E2E functional test flows only (navigate, interact, assert).
> For visual design verification (Figma compliance, pixel diffs, style checks), use `agent-browser` — see Step 3 in SKILL.md.

## When to Run

Run E2E validation when:
- The ticket involves UI changes
- Acceptance criteria describe user-visible behavior
- The plan includes e2e test scenarios

## Prerequisites

- Playwright MCP server available (configured in `.mcp.json`)
- Dev server running (start via project's dev command)

## Process

1. **Start the dev server:**
   ```bash
   npm run dev &
   # Wait for server to be ready
   sleep 5
   ```

2. **Navigate using Playwright MCP tools:**
   Use the accessibility tree for reliable element selection:
   - `browser_navigate` — go to a URL
   - `browser_snapshot` — get the accessibility tree
   - `browser_click` — click an element by accessibility ref
   - `browser_type` — type into an input field
   - `browser_screenshot` — capture visual evidence

3. **For each acceptance criterion with e2e test type:**
   - Navigate to the relevant page
   - Perform the user action described in the criterion
   - Verify the expected outcome via the accessibility tree
   - Take a screenshot as evidence
   - Record pass/fail

4. **Clean up:**
   - Close browser sessions
   - Stop dev server

## Headless Mode (CI)

In CI environments, Playwright runs headless:
```bash
# Playwright MCP automatically uses headless in non-interactive environments
# No special configuration needed
```

## What to Report

For each e2e test:
- Acceptance criterion being validated
- Steps performed
- Expected vs actual outcome
- Screenshot path (saved to `/.harness/screenshots/`)
- Pass/fail

## Phase 3: Persistent Test Generation

> In Phase 3, the QA teammate will also use Playwright Test Agents to generate
> persistent `.spec.ts` files using the Planner → Generator → Healer pattern.
> These files get committed alongside implementation code and run in CI.
> See E2E_PLAYWRIGHT_GENERATION.md (Phase 3).
