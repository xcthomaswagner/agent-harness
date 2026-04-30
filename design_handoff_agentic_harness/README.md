# Handoff: Agentic Harness — Operator Dashboard

## Overview

This is the design handoff for the **Agentic Harness operator dashboard** — a mission-control UI for a multi-agent developer harness. Operators watch live traces (runs), review PRs, read the autonomy report (first-pass acceptance, escape rate, auto-merge), and triage lessons the harness proposes after each run.

The bundle contains a full interactive HTML prototype plus its source design system. Your job is to re-implement it in the target codebase.

---

## About the design files

Everything in this bundle is a **design reference created in HTML** — an interactive prototype showing intended look, information architecture, and behaviour. **It is not production code.** Do not lift the CSS/JS verbatim.

Your task is to **recreate these designs in the target codebase's environment** (React, Vue, SwiftUI, etc.) using its established patterns, routing, and component libraries. If the project has no frontend yet, pick the framework most appropriate for it (React + Vite + TypeScript is a safe default) and implement the design there.

Start by opening `Design System.html` and `Operator Dashboard.html` side-by-side. The design system defines all tokens and primitives; the dashboard composes them into real screens.

---

## Fidelity

**High-fidelity.** Final colours, typography, spacing, pills, phase dots, pipeline rows, live-log styling, KPI tiles — all intentional. Recreate pixel-perfectly. The one piece of licence you have: swap HTML-specific tricks for your framework's idiomatic equivalents (e.g. a `<table>` might become a virtualised list; the `postMessage` Tweaks plumbing does not need to port).

---

## Views

The prototype has six views wired through a top-nav router:

| # | View          | Route (suggested) | Purpose |
|---|---------------|-------------------|---------|
| 1 | Dashboard     | `/`               | Home. Profile cards, lesson counts strip, recent runs table. |
| 2 | Tickets       | `/tickets`        | Kanban-ish pipeline table with detail expand, right-rail live log. |
| 3 | Traces        | `/traces`         | All runs across profiles. Filter chips by status + profile. |
| 4 | Trace Detail  | `/traces/:id`     | Phase timeline, session panels, raw event stream. |
| 5 | Autonomy      | `/autonomy`       | FPA / escape / catch / auto-merge by profile, trends, escaped defects, by-ticket-type. |
| 6 | Learning      | `/learning`       | Lesson triage queue (proposed → applied → rejected) plus unmatched issues. |
| 7 | PR drilldown  | `/pr/:id`         | CI checks, L3 review issues, lesson matches, auto-merge decision. |

All views share the same chrome: left **sidebar** (brand lockup, nav groups: Overview / Pipeline / Ops / Admin, operator card at the bottom) and **topbar** (route tabs, live SSE indicator, theme toggle).

---

## Design tokens (from `Design System.html`)

### Aesthetic

Warm near-black canvas, bone-white text. Amber-orange reserved for **in-flight/running state only**. Supports a light "paper" theme via `[data-theme="light"]` override.

### Ink scale (dark)

```
--ink-000  #0b0b0a   canvas
--ink-050  #121211   raised surface (cards, rail, kpi)
--ink-100  #1a1a18   sidebar, hover
--ink-200  #22221f   dim rules
--ink-300  #2e2e2a   rules
--ink-400  #3c3c37
--ink-500  #5a5a52   meta text
--ink-600  #7d7d73   muted body
--ink-700  #a8a89c   headings / section labels
--ink-800  #d4d4c6   body
--ink-900  #ece9dc   primary text (bone white)
--ink-950  #f7f5e8   paper highlights
```

Light theme flips these to a cool neutral paper palette — see `:root[data-theme="light"]` block in `dashboard.css`.

### Signal colours (same in both themes)

```
--signal-active   #ff7a1a   running / in-flight    (accent — only ever one thing is "active")
--signal-active-2 #ffae5c   accent gradient pair
--signal-ok       #9db48a   sage — passed / done
--signal-warn     #e8c46a   amber — stuck / clarify
--signal-err      #d26a5a   clay — failed
--signal-cool     #6c8fb0   slate — queued / info
```

### Typography

```
--font-serif  "Instrument Serif"     display, big numerics, view titles
--font-sans   "IBM Plex Sans"        UI body
--font-mono   "JetBrains Mono"       ticket IDs, timestamps, all data
```

Never show a data value in a proportional font. Ticket IDs, durations, paths, hashes, percentages, log lines — all mono.

Type scale (approx):
- Display (view title): serif 38px / 1.1 / -0.01em
- Big numeric (stats): serif 48px / 1
- Section header: mono 11px uppercase 0.14em tracking, `--ink-700`
- Body: sans 13–14px / 1.5
- Meta / label: mono 10.5px uppercase 0.12em tracking, `--ink-500`

### Rhythm

```
--unit   8px    base grid (everything multiple of 8 or 4)
--gutter 32px   section gutter
--rule       1px solid --ink-300
--rule-dim   1px solid --ink-200
--rule-strong 1px solid --ink-500
```

No border-radius except on tiny pills (2px) and chips (1px). Everything else is square. No drop shadows — depth comes from rules and ink layers.

---

## Primitives

### Status pill — `.pill`

A small inline badge: coloured dot + label.
- Variants: `.active` (amber, running), `.ok` (sage), `.warn` (yellow), `.err` (clay), `.cool` (slate).
- Structure: `<span class="pill active"><span class="d"></span>In-flight</span>`
- Active variant pulses the dot (1.6s ease-in-out).

### Phase dot row — `.phases`

A run has 5 phases: planning → scaffolding → implementing → reviewing → merging. Render as 5 dots in a row:
- done → filled sage
- active → filled amber with a 3px outer glow halo
- pending → hollow `--ink-400`
- fail → filled clay

Active phase may shimmer (linear-gradient sweep) — toggled by a design setting.

### Chip — `.chip`

Mono uppercase tag for filters. `.is-on` state swaps to accent fill. Tiny numerics trail in `.chip-n` with reduced opacity.

### KPI tile

Vertical stack: mono uppercase label → big serif number → sparkline (inline SVG polyline, `stroke-width:1.5`, `currentColor` so it inherits the tile's signal colour).

### Section block

Every content block opens with `<div class="sec-hd">` (section header rule + mono label) and ends before the next rule. Sections separated by 44px vertical rhythm.

### Table — `.tbl` / `.tbl-lg`

Ultra-flat. Header row: mono uppercase label, bottom rule. Body rows: 12px padding (14 for `.tbl-lg`), dim rule below, subtle hover fill. `.row-live` gets a left-to-right amber gradient wash.

### Button — `.btn`

Mono uppercase, 1px rule, square. Variants: default (outlined), `.primary` (amber fill, dark text), `.ghost` (no rule, text only). `.sm` reduces padding.

### Input — `.search`

Rule-bordered row with a leading glyph, a mono placeholder, and a trailing mono hotkey badge (e.g. `/`).

---

## Screens in detail

### 1. Dashboard (`Home`)

**Layout:** single column, 1600px max, 32/40px padding.

**Sections:**
1. **View head.** Left: `sup` eyebrow ("Overview · home") + serif title ("Mission control") + 13px subtitle. Right: big serif number + uppercase label ("8 · Runs · 24h"), separated by a left rule.
2. **Client profiles.** Grid of 4 cards (`auto-fit minmax(240px, 1fr)`). Each card: serif profile name + mono sample tag, 3-col metric row (FPA / Escape / Auto) in rule-bounded band, footer rows with in-flight + 24h-done chips. Hover: accent border + 1px lift. Click → autonomy view for that profile.
3. **Lessons strip.** Single-row grid of 6 cells (Proposed / Draft / Approved / Applied / Snoozed / Rejected) — each cell shows count in serif 32px over mono label. Click cell → learning view filtered to that state.
4. **Recent runs.** Traces table, compact variant, 6 rows, with a "All traces →" section link.

### 2. Tickets

Kanban-style pipeline with filter tabs (All / In-flight / Stuck / Queued / Done) + profile chips, then a 7-column list (Ticket / Title / Status / Pipeline / Elapsed / Author · created / ▸). Click a row to select → updates the right rail. Click the chevron to expand the row in-place into a detail block with full metadata, phase map, and actions.

Right rail — fixed, 360px. Shows selected ticket context, agent roster (5 agents with state dots), and a live log that streams new lines every ~4s for the active ticket. Log lines: timestamp mono · level token (info/warn/err) · message.

### 3. Traces

View head + filter chip row + one big table. Same columns as tickets minus the detail column. Row click → Trace Detail. `.row-live` class on currently-streaming rows.

### 4. Trace Detail

- View head variant: breadcrumb eyebrow with back-link to traces, serif title, meta row of chips (status pill + profile + branch + PR link + started/author). Right: "Open worktree" + "Stream live →" buttons.
- **Phase timeline** — 5 rows, grid columns `90px 48px 24px 180px 1fr 70px`: elapsed, index, phase dot, phase name, tool + event count, state label. Active row tinted accent.
- **Session panels table** — Role, Agent (mono), State (pill), Last action, At.
- **Raw events** — mono log, 3-column grid (time, event name amber, message). Max-height 320px, scrolls.

### 5. Autonomy

- View head: title `Autonomy report — <profile>`, right-side profile switcher (chips, click to change).
- **Metric row** — 4 cells in a single rule-bordered band: FPA, Escape, Catch, Auto-merge. Each: mono label → serif 36px number → one-line sans subtitle.
- **Two-col trend row** — FPA sparkline card (sage) next to Escape sparkline card (clay).
- **Auto-merge adoption** — wide sparkline card (amber).
- **By ticket type** — right-aligned numeric table (Type / Volume / FPA / Escape / Auto-merge). Escape column renders red when > 20%.
- **Escaped defects** — table of 30d escapes with link back to originating trace.

### 6. Learning

- View head: title "Lessons", right stat "N · Awaiting triage".
- **Filter chip row** — All / Proposed / Draft / Approved / Applied / Snoozed / Rejected with trailing counts.
- **Lessons table (large)** — ID / Lesson (title + muted body) / Profile / Source trace / Evidence / Confidence % / State pill / Actions.
  - Actions depend on state: proposed → Approve · Edit · Reject; draft → Publish · Edit; approved → Apply now; applied → Retire; snoozed → Unsnooze; rejected → Revisit.
- **Unmatched issues** — signals that had no matching lesson rule. Each row: ID / Trace / Signal (mono) / Matches count / Note / `Draft lesson` button.

### 7. PR drilldown

- View head variant: double-breadcrumb (Traces / trace-id / PR #xxxx), title, meta row (status + profile + branch → target + commits/files/diff stats).
- **CI checks** — 4-row table with state pills and duration.
- **Two-col:** Issues raised by L3 review (ID / Severity / Where + note / Matched lesson) next to Lesson matches (Lesson + name / Confidence / Applied pill).
- **Auto-merge decision card** — pill (HOLD / MERGE / BLOCK) + confidence + bulleted reasons.

---

## Interactions & behaviour

- **Routing:** top-nav tabs navigate between views. Deep links for `traces/:id` and `pr/:id`. Clicking a ticket row or trace row opens detail.
- **Pills & phase dots:** purely visual; derived from `status` / phase state.
- **Live log (right rail):** SSE-backed in production. Prototype mocks this with a setInterval pushing new lines for the selected ticket.
- **Tweaks panel:** demo-only overlay for adjusting accent hue, density, shimmer on/off. **Do not port this.**
- **Theme toggle:** top-right button swaps `data-theme` on `<html>`. Persist in localStorage.
- **Hover:** tables — subtle `--ink-050` fill; cards — accent border; chips — underline.
- **Animations:** active dot pulse (1.6s), phase shimmer (2s linear sweep), live-log line slide-in (180ms). Keep motion sparse — everything else static.

---

## State model

The live data model the frontend needs (exact fields in `data.js` and `views-data.js`):

- `Profile` — id, name, sample, in_flight, completed_24h, fpa, escape, catch, auto_merge
- `Ticket` / `Trace` — id, title, profile, status (`in-flight` | `stuck` | `queued` | `done`), phase index, elapsed, author, started, live flag
- `Lesson` — id, state, title, profile, source_trace, evidence count, confidence (0–1), body, created
- `EscapedDefect` — id, trace, severity, where, caught_in, note
- `UnmatchedIssue` — id, trace, signal, matches count, note
- `ByTypeRow` — type, volume, fpa, escape, auto_merge
- `TraceDetail` — id, title, profile, author, branch, pr, started, elapsed, status, phases[], sessions[], events[]
- `PRDetail` — id, title, trace, profile, author, branch, target, commits, files, +/− stats, checks[], issues[], matches[], auto_merge { decision, reasons[], confidence }

Wire these to the backend's SSE / REST endpoints; the prototype's JS files are useful as shape documentation only.

---

## Assets

- Fonts: Instrument Serif, IBM Plex Sans, JetBrains Mono — all Google Fonts. Preconnect + load as shown in the HTML.
- No raster or vector assets. Sparklines, phase dots, and the brand glyph are inline SVG / CSS.
- Brand glyph: a square with a 4px amber dot centered. Wordmark `AGENTICHARNESS` in mono, first word bold.

---

## Files in this bundle

| File | What it is |
|------|------------|
| `Design System.html` | Reference sheet for every token and primitive |
| `Operator Dashboard.html` | Root prototype — open this first |
| `dashboard.css` | Base chrome (sidebar, topbar, ticket layout, pills, phase dots, KPI tiles, rail, tweaks, theme tokens) |
| `views.css` | Styles for Home / Traces / Autonomy / Learning / Trace / PR views |
| `dashboard.js` | Ticket view logic + theme + tweaks |
| `views.js` | Router + renderers for the six non-ticket views |
| `data.js` | Seed data for tickets, agents, logs, KPIs, phases |
| `views-data.js` | Seed data for profiles, traces, lessons, escapes, unmatched, trace detail, PR detail |

---

## Implementation plan (suggested for Claude Code)

1. Scaffold the frontend in the chosen framework. Add routes for the seven views.
2. Port tokens from `Design System.html` → your styling solution (CSS vars, Tailwind config, theme object — whichever fits the codebase). Preserve variable names.
3. Build primitives in isolation: `Pill`, `PhaseDots`, `Chip`, `KPITile`, `Sparkline`, `SectionHeader`, `Table`, `Button`, `Search`. Match the prototype exactly.
4. Build the layout chrome (Sidebar + Topbar). Wire the nav and theme toggle.
5. Build each view one by one, in this order: Home → Traces → Trace Detail → Autonomy → Learning → PR → Tickets (most complex last).
6. Replace prototype seed data with real API/SSE calls. The shapes in `views-data.js` are the contract.
7. Skip the Tweaks panel entirely — prototype-only.
8. Run a visual diff against the HTML prototype at 1440×900 to catch drift.
