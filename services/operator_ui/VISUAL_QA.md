# Visual QA

Playwright-based visual regression is deferred. Rationale: adding
Playwright to this repo means a second Node toolchain, headless-browser
binaries in CI, and a baseline PNG set that needs periodic regeneration
when fonts or browser defaults shift. For a read-mostly operator
dashboard with a ~50 kB bundle, the ROI isn't there for v1.

## How to manually diff against the prototype

The design prototype lives at `design_handoff_agentic_harness/Operator
Dashboard.html`. Until automated visual regression lands, run this
spot-check before merging anything that touches:

- `services/operator_ui/src/styles/tokens.css`
- `services/operator_ui/src/chrome/chrome.css`
- `services/operator_ui/src/primitives/primitives.css`
- `services/operator_ui/src/views/views.css`

### Steps

1. Start L1 locally: `cd services/l1_preprocessing && uvicorn main:app --reload --port 8000`
2. Rebuild the SPA if needed: `cd services/operator_ui && npm run build`
3. Open `http://localhost:8000/operator/?api_key=<YOUR_KEY>` (or leave off `api_key` if DASHBOARD_ALLOW_ANONYMOUS=true)
4. Open the prototype side-by-side: `open design_handoff_agentic_harness/Operator\ Dashboard.html`
5. At 1440×900, walk through each of the 7 views and compare.

### What to watch for

- Phase-dot glow halo and shimmer sweep on the active dot.
- Serif numerics — big metric values should be `Instrument Serif`, not
  `IBM Plex Sans`.
- Pill stroke + dot colours match the signal palette exactly.
- Right-rail live-log slide-in animation on new entries.
- Table row hover fill (`--ink-050`) and is-live amber gradient wash.
- Theme toggle swaps the sidebar/topbar background to paper without
  colour drift in signal pills.

## Future

If operator UX drift becomes a problem, a single Playwright test
wrapped around a mocked backend would catch most regressions. Keep
the deferral note in the PR description so the next maintainer can
pick this up when it's warranted.
