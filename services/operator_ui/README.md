# operator-ui

Preact SPA for the agent-harness operator dashboard. Built with esbuild into
`../l1_preprocessing/operator_static/`, served by FastAPI at `/operator`.

## Build

```
cd services/operator_ui
npm install
npm run build     # one-shot, minified, no sourcemaps
npm run watch     # rebuilds on src change; use alongside `uvicorn --reload`
```

The build output in `operator_static/` IS committed — see the repo-root `.gitattributes`
marking it as generated so PR diffs collapse it. Regenerate before pushing any
`src/` change; the QA gate enforces that `operator.js` was rebuilt.

## Dev loop

1. Run L1 normally: `uvicorn main:app --reload --port 8000` (from `services/l1_preprocessing/`)
2. In another shell: `npm run watch` (from `services/operator_ui/`)
3. Open `http://localhost:8000/operator/`

No standalone dev server — esbuild rebuilds the bundle, FastAPI serves it
directly. Hard-refresh the browser after each save (no HMR in v1; the SPA is
small enough that a full reload is sub-second).

## Layout

```
src/
  main.tsx              SPA entry, renders <App/>
  App.tsx               Chrome + router (commit 4)
  styles/tokens.css     Design-system tokens (commit 2)
  primitives/           Pill, PhaseDots, etc. (commit 3)
  views/                Home, Traces, TraceDetail, Autonomy, Learning, PR, Tickets
  hooks/useFeed.ts      SWR-style data-freshness hook (commit 4)
```

## Why Preact + esbuild, not React + Vite

Preact's API matches React but the bundle lands ~20 kB minified vs. React's
~150 kB. esbuild replaces Vite dev server with a plain `--watch` rebuild.
Rationale: the committed build artifact is small enough not to pollute diffs,
and the harness repo has no existing Node toolchain — adding Vite + its dev
server for a read-mostly dashboard isn't justified. See the plan-review
notes in the PR that introduced this package.
