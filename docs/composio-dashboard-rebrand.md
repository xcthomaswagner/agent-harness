# Composio Dashboard Rebranding

## Overview

The Composio Agent Orchestrator includes a React SPA dashboard at `localhost:3000` that shows agent sessions, PR status, and CI results. It's fully rebrandable since it's MIT-licensed with no phone-home.

## Rebranding Approach

### Option A: CSS Override (Quick)

Create a custom CSS file that overrides Composio's default styles:

```css
/* custom-dashboard.css */
:root {
  --brand-primary: #1B2A4A;    /* XCentium navy */
  --brand-secondary: #2E6CA4;  /* XCentium blue */
  --brand-accent: #E8792F;     /* XCentium orange */
}

/* Logo replacement */
.dashboard-logo {
  background-image: url('/xcentium-logo.svg');
}

/* Title */
.dashboard-title::after {
  content: 'XCentium Agentic Harness';
}
```

### Option B: Fork Dashboard (Full Control)

1. Clone the Composio dashboard source:
   ```bash
   cp -r ~/.npm-global/lib/node_modules/@composio/ao/packages/web ./dashboard
   ```

2. Modify:
   - `src/components/Header.tsx` — logo and title
   - `src/styles/globals.css` — color palette
   - `public/favicon.ico` — icon
   - `next.config.js` — page title

3. Build and serve:
   ```bash
   cd dashboard && npm run build
   ao start --dashboard-dir ./dashboard/out
   ```

## Current Configuration

The `agent-orchestrator.yaml` configures the dashboard:

```yaml
port: 3000          # Dashboard port
terminalPort: 3001  # Terminal WebSocket
```

## When to Rebrand

Rebrand before client demos or production deployment. Not needed for internal development/testing.
