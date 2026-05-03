# operator-dashboard branch status

> Last updated: 2026-04-19 by Claude Code session.
> Delete this file at merge time.

## DO NOT MERGE — held open for more testing

Thomas asked to keep `feature/operator-dashboard` → PR #14 open for
further end-to-end testing before merging to `main`. No one-off commits
should auto-merge this branch. When resuming, stack new fixes on top
(branch off it, target the PR, or commit directly).

## How to run right now

1. Ensure L1 backend is up:
   ```
   cd services/l1_preprocessing && source .venv/bin/activate
   uvicorn main:app --port 8000
   ```
2. Rebuild the SPA if `src/` changed since last push:
   ```
   cd services/operator_ui && npm run build
   ```
3. Open in browser. The API key lives in
   `services/l1_preprocessing/.env` as `API_KEY=`:
   ```
   http://localhost:8000/operator/?api_key=<API_KEY>
   ```

## How to run the tests

| Suite | Command |
|---|---|
| L1 pytest | `cd services/l1_preprocessing && source .venv/bin/activate && pytest -q` |
| Vitest (primitives/router) | `cd services/operator_ui && npm test` |
| Playwright E2E (requires L1 running) | `cd services/operator_ui && npx playwright test` |
| Screenshot capture | `npx playwright test e2e/screenshot.spec.ts` → `/tmp/operator-screens/*.png` |

## Last green numbers

L1 pytest 1557 · L3 pytest 237 · root pytest 62 · Vitest 33 · Playwright 16.
Ruff + mypy + `tsc --noEmit` all clean.

## Open followups (non-blocking)

See `./README.md` "Branch status" section. Live in the PR description too.

## Key file pointers

- SPA entry: `src/main.tsx`
- Router: `src/router.ts`
- Settings popover: `src/chrome/Settings.tsx` + `src/theme.ts`
- Views: `src/views/*.tsx`
- Backend JSON: `../l1_preprocessing/operator_api_data.py`
- Backend shell/static: `../l1_preprocessing/operator_api.py`
- CSS regression guard: `e2e/operator.spec.ts` ("served CSS bundle includes every primitive class")
