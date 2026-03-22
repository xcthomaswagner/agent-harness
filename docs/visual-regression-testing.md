# Visual Regression Testing

## Overview

For UI-heavy tickets, visual regression testing compares screenshots against a baseline to catch unintended visual changes. Integrate Percy or Chromatic into the QA validation step.

## Percy Integration

### Setup
```bash
npm install --save-dev @percy/cli @percy/playwright
```

### In Playwright Tests
```typescript
import { test } from '@playwright/test';
import percySnapshot from '@percy/playwright';

test('homepage visual', async ({ page }) => {
  await page.goto('/');
  await percySnapshot(page, 'Homepage');
});
```

### CI Configuration
```yaml
- name: Percy visual tests
  run: npx percy exec -- npx playwright test
  env:
    PERCY_TOKEN: ${{ secrets.PERCY_TOKEN }}
```

### QA Skill Integration

Add to the QA agent's prompt when `figma_design_spec` is present:

```
After running E2E tests, take screenshots of each modified page/component
and compare against the Figma design spec. Note any visual discrepancies
in the QA matrix under a "Design Compliance" section.
```

## Chromatic Integration (Storybook Projects)

### Setup
```bash
npm install --save-dev chromatic
```

### Run
```bash
npx chromatic --project-token=$CHROMATIC_TOKEN
```

### When to Use Which
- **Percy**: For full-page visual testing (any framework)
- **Chromatic**: For component-level testing (Storybook projects only)
- **Neither needed**: For API-only changes, utility changes, or when no `figma_design_spec` exists
