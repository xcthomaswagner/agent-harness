# Self-Learning Harness — Implementation Plan

**Status:** Draft v2 (2026-04-17) — incorporates review findings
**Scope:** Tier 1 of the three-tier self-learning roadmap (pattern-mined guidance, human-approved edits). Tiers 2 and 3 are sketched at the end for continuity but are not in scope for this build.
**Dependencies:** Existing L1 autonomy engine (`autonomy_store.py`, `autonomy_metrics.py`, `autonomy_sidecars.py`), trace observability stack (`tracer.py`, `tool_index.py`, `diagnostic.py`, `redaction.py`), runtime skill/agent injection (`scripts/inject_runtime.py`), existing `/dashboard` and `/autonomy` views.

**Changes from v1:**
- Rollout restructured as a single-detector vertical slice (Detector 2 first) instead of "6 detectors then UI."
- Evidence normalized out of JSON into a `lesson_evidence` join table.
- New hot-read index for dashboard queries.
- `tool_index.py` extension (`bash_verb_counts`) added as a prerequisite for Detector 1.
- Detector 3 spec corrected — reads `review_issues` directly, not `issue_matches`.
- Drafter split into markdown and YAML paths (Detector 6 needs ruamel.yaml round-trip).
- Added a consistency-check Claude call post-draft to catch over-specification.
- Added per-detector try/except isolation in the runner.
- **Human-edit detection** promoted to first-class in the outcomes job — detects whether a human re-edited the lesson's anchor after merge (direct signal of a bad lesson).
- **Detector 7 (plan-drift)** added to the detector catalogue as a seventh v1 detector, scheduled after the YAML detector.
- Schema naming convention documented so future detectors can be added without migration.

---

## 1. Goal

Turn the harness into a system that **learns from its own production runs** — surfacing repeatable failure patterns as concrete, reviewable edits to skill prompts, platform profiles, and client profile knobs. The human stays in the loop for every actual weight change; the machine does the pattern detection and drafting that you do by hand today.

### What "done" looks like (success criteria)

1. After 14+ days of live traces, the `/autonomy/learning` dashboard shows at least 3 proposed lessons, each with evidence links to ≥3 traces.
2. Approving a lesson opens a harness-repo PR (authored by `xcagentrockwell`) that applies the proposed edit to `runtime/skills/*` or `runtime/platform-profiles/*`, with lesson ID stamped in the diff's commit body.
3. The 2026-04-10 "agent shells out to `sf` CLI instead of MCP" pattern would have been surfaced automatically by this system after 3 traces — not hand-diagnosed.
4. A lesson that produced a regression can be identified by `lesson_id` in `git blame` and reverted cleanly.
5. Zero production prompts change without human approval on the PR.

### Non-goals

- No online RL. No shadow-branch traffic splitting. No skill autogeneration from scratch.
- No cross-client pattern leakage. Every lesson is scoped to `(client_profile, platform_profile)`.
- No auto-tuning of `auto_merge_enabled` or kill switches.

---

## 2. Architecture

```
┌──────────────────────┐
│  Existing signal     │     read-only
│  sources             │────────────────┐
│  • autonomy.db       │                │
│  • trace-archive/    │                ▼
│  • runtime/*.md      │     ┌──────────────────────┐      writes
│                      │     │  learning_miner       │──────────┐
└──────────────────────┘     │  (nightly job)        │          ▼
                             │                       │   ┌─────────────────┐
                             │  Pattern detectors    │   │  autonomy.db    │
                             │  → lesson_candidates  │   │  lesson_*       │
                             └──────────────────────┘   │  tables         │
                                        ▲               └─────────────────┘
                                        │                       ▲
                                        │ reads candidates       │ approves/rejects
                                        │                       │
                             ┌──────────────────────┐           │
                             │  /autonomy/learning  │───────────┘
                             │  dashboard panel     │
                             │                       │
                             │  [Approve] → drafts  │
                             │  PR via gh CLI       │──────────┐
                             └──────────────────────┘          │
                                                               ▼
                                                    ┌─────────────────────┐
                                                    │  harness repo PR    │
                                                    │  (normal review)    │
                                                    │  lesson_id stamp    │
                                                    └─────────────────────┘
                                                               │
                                                               ▼
                                                    ┌─────────────────────┐
                                                    │  merge → runtime/*  │
                                                    │  change → next      │
                                                    │  inject_runtime run │
                                                    │  picks it up        │
                                                    └─────────────────────┘
```

### Key design decisions

1. **L1-owned.** The miner runs inside `services/l1_preprocessing/` as a background task, not a separate service. Keeps ops simple and reuses the existing FastAPI/structlog/config setup. One more file, no new deployment surface.
2. **Read-only on runtime/.** The miner never writes `runtime/*.md` directly. It writes *proposed diffs* to the DB; humans click Approve; a separate job runs `gh pr create` with the diff applied. This preserves the "prompts only change via reviewed PR" invariant.
3. **SQLite, same DB.** Two new tables in `autonomy.db` via a v5 migration. No new storage.
4. **Deterministic detectors first.** Tier 1 uses hand-coded pattern detectors (SQL queries + regex). No LLM calls in the detection path — keeps the signal honest, the cost zero, and the system debuggable.
5. **LLM-assisted drafting, deterministic detection.** The *proposed prompt diff* gets drafted by a one-shot Claude call only after the human has opted into a candidate, so the LLM never influences which patterns count as lessons — only how they're phrased.
6. **Provenance everywhere.** Every lesson gets a `lesson_id`. It stamps the DB row, the commit message, a frontmatter field in the edited skill, and the PR description. Regression → `git log -S "lesson_id: LSN-123"` finds it.

---

## 3. Data model

### 3.1 New tables (migration v5)

All three tables live in `autonomy.db`. Migration in `services/l1_preprocessing/autonomy_store.py::_migrate_to_v5`.

```sql
CREATE TABLE lesson_candidates (
    id INTEGER PRIMARY KEY,
    lesson_id TEXT NOT NULL UNIQUE,          -- 'LSN-' + 8-hex of (pattern_key|scope_key)
    detector_name TEXT NOT NULL,              -- e.g. 'mcp_drift', 'human_issue_cluster', 'plan_drift'
    detector_version INTEGER NOT NULL DEFAULT 1, -- bump when a detector's semantics change
    pattern_key TEXT NOT NULL,                -- detector-specific fingerprint (stable across runs)
    client_profile TEXT NOT NULL DEFAULT '',  -- '' means profile-agnostic (rare)
    platform_profile TEXT NOT NULL DEFAULT '',
    scope_key TEXT NOT NULL DEFAULT '',       -- e.g. 'salesforce|sf_apex_test' for MCP-drift
    frequency INTEGER NOT NULL DEFAULT 1,     -- how many traces hit this pattern in window
    severity TEXT NOT NULL DEFAULT 'info',    -- info|warn|critical
    detected_at TEXT NOT NULL,                -- first detection
    last_seen_at TEXT NOT NULL,               -- rolling update
    proposed_delta_json TEXT NOT NULL DEFAULT '{}', -- {target_path, edit_type, before, after, rationale}
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed|draft_ready|approved|rejected|applied|reverted|stale|snoozed
    status_reason TEXT NOT NULL DEFAULT '',
    next_review_at TEXT NOT NULL DEFAULT '',  -- set on snooze; empty otherwise
    pr_url TEXT NOT NULL DEFAULT '',          -- set when drafted
    merged_commit_sha TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (detector_name, pattern_key, scope_key)
);
CREATE INDEX idx_lesson_candidates_status ON lesson_candidates (status);
CREATE INDEX idx_lesson_candidates_profile ON lesson_candidates (client_profile, platform_profile);
CREATE INDEX idx_lesson_candidates_detector ON lesson_candidates (detector_name);
-- Hot read path for the dashboard: "proposed lessons for profile X, newest first"
CREATE INDEX idx_lesson_candidates_profile_status_seen
    ON lesson_candidates (client_profile, status, detected_at DESC);

CREATE TABLE lesson_evidence (
    id INTEGER PRIMARY KEY,
    lesson_id TEXT NOT NULL REFERENCES lesson_candidates(lesson_id) ON DELETE CASCADE,
    pr_run_id INTEGER REFERENCES pr_runs(id),  -- nullable: some detectors don't key off pr_runs
    trace_id TEXT NOT NULL DEFAULT '',         -- ticket id / trace archive key
    observed_at TEXT NOT NULL,                 -- when the evidence event happened (pattern instance time)
    source_ref TEXT NOT NULL DEFAULT '',       -- e.g. 'tool_index.json#tool_counts'
    snippet TEXT NOT NULL DEFAULT '',          -- redacted, ≤500 chars
    UNIQUE (lesson_id, trace_id, source_ref)
);
CREATE INDEX idx_lesson_evidence_lesson_id ON lesson_evidence (lesson_id);
CREATE INDEX idx_lesson_evidence_trace_id ON lesson_evidence (trace_id);
CREATE INDEX idx_lesson_evidence_pr_run_id ON lesson_evidence (pr_run_id);

CREATE TABLE lesson_outcomes (
    id INTEGER PRIMARY KEY,
    lesson_id TEXT NOT NULL REFERENCES lesson_candidates(lesson_id),
    measured_at TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    pre_fpa REAL,                             -- FPA for (client_profile, scope) before merge
    post_fpa REAL,                            -- FPA after merge
    pre_escape_rate REAL,
    post_escape_rate REAL,
    pre_catch_rate REAL,
    post_catch_rate REAL,
    pattern_recurrence_count INTEGER NOT NULL DEFAULT 0, -- did the pattern re-fire after merge?
    human_reedit_count INTEGER NOT NULL DEFAULT 0,       -- non-xcagentrockwell commits touching the lesson's anchor post-merge
    human_reedit_refs TEXT NOT NULL DEFAULT '[]',        -- JSON list of {sha, author, committed_at, message}
    verdict TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|no_change|regressed|human_reedit
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_lesson_outcomes_lesson_id ON lesson_outcomes (lesson_id);
```

**Why three tables?**
- `lesson_candidates` holds the lesson itself, reviewable as one row per pattern.
- `lesson_evidence` normalizes the "here's where we saw this" pointers out of JSON so we get referential integrity, reverse queries ("which lessons touched trace X"), and reuse of the existing `_chunked_in_query` helper in `autonomy_metrics.py` (which expects `list[int]` of pr_run_ids).
- `lesson_outcomes` is measured **after** merge — it's what lets us flag lessons that didn't help or actively regressed, and what drives the future auto-revert path.

**`detector_version` column.** Included from v1 so detector-logic changes don't require a migration. If Detector 2 v1 produces noisy candidates and we tighten the threshold in v2, the new version writes fresh rows; the old candidates stay visible for historical comparison.

### 3.2 Schema-naming convention (for future detectors)

So new detectors can be added without another migration:
- `detector_name` is free-form snake_case (`mcp_drift`, `human_issue_cluster`, `plan_drift`, future: `simplify_regression`, `security_scan_false_negative`).
- `pattern_key` is detector-owned: any string the detector generates deterministically from its inputs (typically `|`-joined field values). The uniqueness contract is `(detector_name, pattern_key, scope_key)`, so different detectors can freely use overlapping `pattern_key` spaces.
- `scope_key` format is consistent: `<client_profile>|<platform_profile>|<detector_specific>`. Empty parts become `''`. Examples: `xcsf30|salesforce|sf_apex_test`, `||code_review/SECURITY_CHECKS.md` (profile-agnostic, base-skill edit).
- Detectors that don't fit `(client_profile, platform_profile)` scoping (e.g., future base-skill detectors) use the empty-part convention. The UNIQUE index still works.

### 3.3 Lesson ID format

`LSN-` + 8 hex chars = first 4 bytes of `sha256(detector_name | pattern_key | scope_key)`.

Deterministic across re-detections. Same pattern on the same scope always produces the same `lesson_id` → the UNIQUE index on `(detector_name, pattern_key, scope_key)` is what lets us upsert `frequency` and `last_seen_at` without creating duplicates.

### 3.4 Evidence row shape

Each pattern instance produces one row in `lesson_evidence`. Capped at 20 rows per `lesson_id` — the runner trims oldest on insert once the cap is hit. Each `snippet` is ≤500 chars and runs through `redaction.redact()` before insert.

Example rows for one lesson:

| lesson_id | pr_run_id | trace_id | observed_at | source_ref | snippet |
|-----------|-----------|----------|-------------|------------|---------|
| LSN-a1b2c3d4 | 142 | XCSF30-88424 | 2026-04-10T02:08:00Z | tool_index.json#tool_counts | used Bash:sf 8×, mcp__salesforce__sf_apex_test: 0× |
| LSN-a1b2c3d4 | 156 | XCSF30-88425 | 2026-04-11T14:02:00Z | tool_index.json#tool_counts | used Bash:sf 6×, mcp__salesforce__sf_apex_test: 0× |

### 3.5 Proposed delta JSON shape

```json
{
  "target_path": "runtime/platform-profiles/salesforce/skills/salesforce-dev-loop/SKILL.md",
  "edit_type": "append_section",
  "anchor": "## Anti-patterns",
  "before": "",
  "after": "- Shelling out to `sf` via Bash when `mcp__salesforce__*` tools are available. See lesson LSN-a1b2c3d4.",
  "rationale_md": "Observed in 5 traces on client_profile=xcsf30 over 14 days. Agent reached for Bash `sf` in every case despite MCP availability.",
  "token_budget_delta": 28
}
```

`edit_type` ∈ `{append_section, insert_under_heading, replace_regex, add_frontmatter_field, patch_yaml_key}`.

**One edit type per lesson.** Harder edits (move sections, restructure headings) stay human. The miner does additions and targeted replacements — never structural surgery on a skill.

---

## 4. Detectors (Tier 1, v1)

### Activation threshold (global)

A candidate is only inserted if:
- `frequency >= 3` within the last 14 days **on the same `(client_profile, scope_key)`**, AND
- Pattern did not re-fire in the last 24 hours with the same `proposed_delta` already-applied (dedup).

Candidates below threshold are counted internally so we can surface "brewing" patterns for human inspection without filing them.

### Detector catalogue for v1

| # | Name | Reads | Pattern | Scope key | Proposed edit | Prereqs |
|---|------|-------|---------|-----------|---------------|---------|
| 1 | `mcp_drift` | `tool_index.json` per trace (extended) | `mcp_servers_available ⊃ mcp_servers_used` AND `bash_verb_counts[verb] ≥ N` for a verb that a known MCP tool could have handled | `<client>\|<platform>\|<bash_verb>` (e.g. `xcsf30\|salesforce\|sf`) | Append anti-pattern to platform supplement's relevant skill | `tool_index.py` extension (see §4.1) |
| 2 | `human_issue_cluster` | `review_issues` where `source='human_review' AND is_valid=1` | ≥3 human issues on the same `(category, normalized_file_pattern)` across traces | `<client>\|<platform>\|<category>\|<file_glob>` | Add check to `runtime/skills/code-review/SECURITY_CHECKS.md` or platform `CODE_REVIEW_SUPPLEMENT.md` | none — ships first |
| 3 | `judge_over_filters` | `review_issues` directly (see §4.2) | AI issue with `is_valid=0` (judge-rejected) on `pr_run_id` X, then human issue with matching `(file_path, category)` on same `pr_run_id` ≥3× | `<client>\|<platform>\|<category>` | Add carve-out to `runtime/agents/judge.md` for that category | none |
| 4 | `first_tool_error_recurrence` | `tool_index.first_tool_error` | Same `(tool_name, normalized error substring)` appears across ≥3 traces | `<client>\|<platform>\|<tool>\|<err_hash>` | Add known-gotcha to platform profile's PROFILE.md | none |
| 5 | `skipped_phase` | `diagnostic.json` checklist | Same check stays `yellow`/`red` across ≥3 traces on same profile | `<client>\|<platform>\|<check_name>` | Tighten skill Phase 1 preamble to make the missing step a hard gate | none |
| 6 | `autonomy_knob_drift` | `autonomy_metrics.compute_ticket_type_breakdown` rolling | A `ticket_type` on a profile hits `FPA ≥ 95%` with `sample_size ≥ 30` and is **not** in `low_risk_ticket_types` | `<client>\|<platform>\|<ticket_type>` | Edit `runtime/client-profiles/<client>.yaml` to add the type to `low_risk_ticket_types` | YAML drafter (see §6.1) |
| 7 | `plan_drift` | plan artifact (`plan.md`) + PR file list | ≥3 traces where actual PR file list diverges from planner's declared file list by > threshold (files added off-plan, files in plan not touched) | `<client>\|<platform>\|<ticket_type>` | Tighten planner skill's output contract — e.g. require explicit "exploratory: true" flag when file list is provisional | none (plan artifact is already in `trace-archive/<id>/plan.md`) |

Each detector is a standalone module in `services/l1_preprocessing/learning_miner/detectors/<name>.py` implementing a single interface:

```python
class Detector(Protocol):
    name: str
    version: int  # bumped when semantics change

    def scan(self, conn: sqlite3.Connection, window_days: int) -> list[CandidateProposal]:
        ...
```

`CandidateProposal` is a dataclass of the fields above. The orchestrator upserts into `lesson_candidates` using the UNIQUE key and inserts corresponding rows in `lesson_evidence`.

### 4.1 Prereq for Detector 1: `tool_index.py` extension

`tool_index.py` currently records per-tool call counts but not Bash command text. To make Detector 1 actually scope-aware ("agent reached for `sf` 8× when `mcp__salesforce__*` was available"), extend the parser:

- On every `tool_use` event where `name == "Bash"`, extract the first whitespace-separated token from `input.command` (after stripping leading `env FOO=bar`, `cd /path &&`, and similar preambles — a short allowlist regex, not a full shell parser).
- Accumulate into `bash_verb_counts: dict[str, int]`.
- Include `bash_verb_counts` in the returned `ToolIndex` dataclass and in the cached `ARTIFACT_TOOL_INDEX` trace entry.

This is a ~30-line change with unit tests. Ship it in Phase A.1 before Detector 1's Phase C slot.

### 4.2 Note on Detector 3

**Does NOT read `issue_matches`.** The existing `issue_matches` table only links `is_valid=1` AI issues to human issues (autonomy_store.py enforces `ai.is_valid == 1` on insert). Judge-rejected AI issues live in `review_issues` with `source='ai_review'` and `is_valid=0`. Detector 3 queries `review_issues` directly — self-joining `pr_run_id` between the rejected-AI side and the human-review side, matching on `(normalized_file_path, category)`. No new table or parser needed.

### 4.3 Why these seven and not more

- **Detector 2 is the vertical-slice pick.** Data fully available, highest per-trace signal (external-reviewer findings are authoritative per project feedback), simplest edit shape (append to a markdown section).
- **Detectors 3, 4, 5 reuse Detector 2's plumbing** once the dashboard and PR path exist — essentially read-queries plus a `CandidateProposal` shape.
- **Detector 1 is highest-value but highest-friction** — it needs the `tool_index` extension first and only becomes detectable at Salesforce scale once more traces accumulate.
- **Detector 6 unlocks the YAML-edit path** and is the first proof that non-prompt configuration can self-tune.
- **Detector 7 (plan-drift)** catches a class of failure the others miss — correct tools, correct prompts, wrong scope. The planner says "I'll edit files A, B" and the developer ends up touching A, B, C, D. A tightening on the planner's output contract can eliminate a lot of downstream QA noise.

Detectors beyond these seven are deferred to v2. Examples: simplify-induced regressions, security-scan false negatives, cross-profile base-skill drift (with explicit "promote to base skill" human step), test-flakiness clusters.

---

## 5. Services and code layout

```
services/l1_preprocessing/
    learning_miner/                     # NEW package
        __init__.py
        runner.py                       # orchestrator: runs all detectors with per-detector isolation
        detectors/
            __init__.py
            base.py                     # Protocol + CandidateProposal dataclass
            human_issue_cluster.py      # Detector 2 — ships first
            judge_over_filters.py       # Detector 3
            first_tool_error_recurrence.py  # Detector 4
            skipped_phase.py            # Detector 5
            mcp_drift.py                # Detector 1 — needs tool_index extension
            autonomy_knob_drift.py      # Detector 6 — YAML drafter path
            plan_drift.py               # Detector 7
        drafter_markdown.py             # one-shot Claude call; append/insert/replace in .md files
        drafter_yaml.py                 # ruamel.yaml round-trip for client-profile edits
        drafter_consistency_check.py    # second Claude call — "does this contradict existing rules?"
        pr_opener.py                    # `gh` CLI wrapper that applies the diff and opens a PR
        outcomes.py                     # post-merge metric comparison + human-reedit detection
    learning_api.py                     # NEW FastAPI router mounted on L1 app
                                        # GET /api/learning/candidates
                                        # POST /api/learning/candidates/{id}/approve
                                        # POST /api/learning/candidates/{id}/reject
                                        # POST /api/learning/candidates/{id}/snooze
                                        # POST /api/learning/candidates/{id}/preview-diff
    learning_dashboard.py               # NEW HTML panel at /autonomy/learning
    tool_index.py                       # EXTENDED: bash_verb_counts field (Phase A.1)
    autonomy_store.py                   # EXTENDED: v5 migration, new helpers
    main.py                             # EXTENDED: mount learning_api router, add scheduler startup hook
tests/                                  # (or services/l1_preprocessing/tests/)
    test_learning_detectors_human_issue_cluster.py
    test_learning_detectors_judge_over_filters.py
    test_learning_detectors_first_tool_error.py
    test_learning_detectors_skipped_phase.py
    test_learning_detectors_mcp_drift.py
    test_learning_detectors_autonomy_knob_drift.py
    test_learning_detectors_plan_drift.py
    test_learning_runner_isolation.py    # per-detector try/except contract
    test_learning_drafter_markdown.py    # mocked Claude
    test_learning_drafter_yaml.py        # ruamel round-trip, schema validation
    test_learning_drafter_consistency.py # mocked Claude, contradiction detection
    test_learning_pr_opener.py           # mocked gh
    test_learning_outcomes.py            # pre/post metrics + human_reedit
    test_learning_api.py
    test_autonomy_store_v5.py
    test_tool_index_bash_verb_counts.py
```

### Why not a separate service

The miner runs on the same data as the autonomy dashboard, reads the same DB, and has no independent scaling need. Splitting it into a second service doubles the deployment complexity for zero benefit at this scale.

### Scheduler

APScheduler or a simple `asyncio.create_task` loop started from `main.py`'s lifespan context. Config:

- `LEARNING_MINER_ENABLED` (env, default `false` for v1 rollout; flip to `true` per host after backfill validation)
- `LEARNING_MINER_INTERVAL_HOURS` (default 24)
- `LEARNING_MINER_WINDOW_DAYS` (default 14)
- `LEARNING_DRAFTER_MODEL` (default `claude-opus-4-7`)
- `LEARNING_AUTO_DRAFT` (default `false` — if true, run the Claude drafter on newly-detected candidates automatically; if false, human clicks "Draft diff")
- `LEARNING_CONSISTENCY_CHECK_ENABLED` (default `true`)
- `LEARNING_OUTCOMES_WINDOW_DAYS` (default 14 — pre/post measurement window)

Run only once per interval — if a run is already in flight (lock file in `LOGS_DIR`), skip the tick. Log a single structured event per run: `learning_miner_run` with detector counts, new/updated candidate counts, and duration.

### Per-detector isolation (runner contract)

`runner.py` wraps each detector call so a single corrupt trace or a single broken detector cannot kill the whole nightly run:

```python
for detector in detectors:
    try:
        proposals = detector.scan(conn, window_days)
        for p in proposals:
            upsert_candidate(conn, p)
    except Exception as exc:
        logger.exception(
            "learning_detector_failed",
            detector_name=detector.name,
            detector_version=detector.version,
            error=str(exc),
        )
        # keep going — other detectors still run
```

Detectors themselves also wrap each per-trace pattern match in try/except so one corrupt `tool_index.json` doesn't poison the detector's entire output.

---

## 6. Drafting proposed edits (the Claude step)

Each detector returns a `CandidateProposal` with a *mechanical* starter diff (append a fixed template string under a known anchor, for example). This lands in the `proposed_delta_json` column immediately. The Claude-assisted refinement happens post-approval-trigger.

### 6.1 Two drafters, one contract

Markdown and YAML get different code paths. Lumping them caused the v1 review finding — a YAML-list-add is not a markdown-append.

**`drafter_markdown.py`** — used by Detectors 1, 2, 3, 4, 5, 7. Target paths under `runtime/skills/**/*.md`, `runtime/platform-profiles/**/*.md`, `runtime/agents/**/*.md`.

1. Read the target file; extract the section identified by `proposed_delta.anchor`.
2. Read the evidence rows.
3. One Claude Opus call with a tight system prompt:
   - "You are editing `<target_path>`. The harness observed this pattern across N traces: `<evidence>`. Write the smallest possible edit that prevents recurrence. Do not add rules that are not strictly supported by evidence. Output a unified diff."
4. Validate:
   - `git apply --check` against a temp copy.
   - Added-line token count ≤ `token_budget_delta`.
   - No added line starts with "always," "never," "must," or case variants (per existing Agentforce guidance — absolute directives cause agents to get stuck). Enforced by regex.
5. Replace the mechanical starter with the Claude-drafted diff in `proposed_delta_json`.

**`drafter_yaml.py`** — used by Detector 6. Target paths under `runtime/client-profiles/*.yaml`.

1. Load target with `ruamel.yaml` round-trip loader (preserves comments and formatting).
2. Apply the structured edit from `proposed_delta.edit_op` (e.g. `{op: "list_add", path: "autonomy.low_risk_ticket_types", value: "bug"}`). No LLM call needed — the edit is deterministic from the detector's findings.
3. Dump; compare against `runtime/client-profiles/schema.yaml` (use existing validation path).
4. Store the unified diff in `proposed_delta_json`.

YAML edits do not need an LLM because their shape is fully determined by the detector. This keeps costs down and removes a class of drafter bug.

### 6.2 Consistency-check (`drafter_consistency_check.py`)

Gates every Markdown draft before it hits `status=draft_ready`. Catches the over-specification failure mode that the absolute-directive filter misses (a narrowly-scoped rule that silently contradicts an existing rule).

One Claude Opus call, different prompt:
- System: "You are reviewing a proposed addition to a Markdown skill file. You will receive the CURRENT file content and the PROPOSED unified diff. Return a JSON object: `{\"contradicts\": bool, \"contradicts_with\": str, \"reasoning\": str}`. Flag `contradicts=true` only when the new rule would make the agent unable to follow an existing rule, or vice versa. Do not flag stylistic overlap."
- If `contradicts=true`, block promotion to `draft_ready` and surface the reasoning on the dashboard so the human can either edit the draft or reject.

Cost: +$0.10 per Markdown lesson. Kill-switchable via `LEARNING_CONSISTENCY_CHECK_ENABLED=false`.

### 6.3 Fallback behavior

If the Claude call in either drafter fails (API error, timeout, malformed output), the candidate stays at `status=proposed` with the mechanical starter in `proposed_delta_json`. The dashboard's "Draft diff" button shows an error and a retry affordance. The human can also approve the mechanical-starter diff as-is if the LLM is down — defence in depth.

### 6.4 Cost

- Markdown drafter: ~$0.10 per lesson (Opus call, cacheable target file).
- Consistency check: ~$0.10 per lesson.
- YAML drafter: $0 (deterministic).
- Outcomes job: $0 (SQL and git only).

At 50 lessons/week across Markdown detectors, total ≤$10/week. Billed against the Anthropic API key, not the agent-session Max subscription — same pattern as the ticket analyst.

---

## 7. Human approval flow

### `/autonomy/learning` panel

Added as a top-level card on the unified `/dashboard` landing page and as its own page.

Columns per candidate:
- Status pill (proposed / draft_ready / approved / applied / reverted)
- Detector name
- Scope (`client_profile` · `platform_profile` · scope_key)
- Frequency (N traces in last 14d)
- First seen / last seen
- Evidence (collapsed by default — expand to see trace links)
- Proposed edit (rendered as a unified diff, syntax-highlighted via `<pre>` and Pygments or similar)
- Actions: [Draft diff] / [Approve & open PR] / [Reject] / [Snooze 7d]

### Approve action

Hitting Approve does three things:
1. Marks candidate `status=approved` in DB.
2. Calls `pr_opener.py`:
   - Creates branch `learning/lesson-<lesson_id>` in the harness repo.
   - Applies `proposed_delta_json` via `git apply`.
   - Adds `lesson_id` frontmatter field to any edited Markdown skill.
   - Commits with message `chore(learning): LSN-a1b2c3d4 - mcp drift fix (salesforce|sf_apex_test)` authored by `xcagentrockwell`.
   - `gh pr create` with body including: detector name, scope, frequency, evidence trace links, rationale, "auto-revert window: 14 days."
3. Stores returned PR URL on the candidate row.

### Reject action

Marks `status=rejected`, records reason in `status_reason`, and suppresses re-proposal of the same `pattern_key+scope_key` for 30 days.

### Snooze action

Sets `status='proposed'` with a `next_review_at` field (needs a small v5 addition) pushed forward N days. For patterns that need more data.

### Applied → measured

When the PR merges (detected by polling or by a webhook — reuse L3's GitHub webhook path), the candidate flips to `status=applied` and `merged_commit_sha` is recorded. Fourteen days later, `outcomes.py` runs and writes to `lesson_outcomes`:
- Pre-window metrics (14 days before merge)
- Post-window metrics (14 days after merge)
- Pattern recurrence (did the detector re-fire in the post window?)
- Verdict: `confirmed` (metrics improved + no recurrence), `no_change` (metrics flat), or `regressed` (metrics worsened).

### Auto-revert (Tier 2 hook; v1 just flags)

If `verdict='regressed'`, the dashboard shows a "Revert" button with a pre-filled PR description citing the outcome row. Automated reversion is deferred — this is just the signal for a human to revert.

---

## 8. Guardrails

1. **Per-skill token budget.** Every skill file gets a frontmatter field `max_additional_tokens: N` (default 500). If a lesson would push the skill over budget, the drafter must propose a *replacement* of an existing rule, not an append. Prevents prompt bloat over 100+ lessons.
2. **Scope locking.** Lessons never cross `(client_profile, platform_profile)` boundaries. A Sitecore lesson cannot edit a Salesforce profile file. Enforced by matching `proposed_delta.target_path` against the candidate's scope.
3. **Absolute-directive filter.** Reject drafts whose added lines start with `always`, `never`, `must`, `NEVER`, `ALWAYS`, `MUST`.
4. **No edits to infrastructure.** Drafter has an allowlist of editable paths: `runtime/skills/**/*.md`, `runtime/platform-profiles/**/*.md`, `runtime/client-profiles/*.yaml`. Nothing else. No `services/`, no `scripts/`, no CI config.
5. **Prompt cache for drafter.** Skill file passed as cacheable context; evidence passed per-call. Keeps cost flat even as skills grow.
6. **Kill switch.** `LEARNING_MINER_ENABLED=false` stops detection; `LEARNING_AUTO_DRAFT=false` blocks LLM drafting; manually rejecting a candidate suppresses it for 30 days. Three layers, same pattern as auto-merge.
7. **Privacy/redaction.** Evidence snippets pulled into `evidence_json` run through the existing `redaction.py` before insert. The miner reads raw traces but never exports raw content into the DB — only redacted summaries. Same guarantee as the bundle endpoint.

---

## 9. Testing strategy

### Unit tests (per-detector)

For each detector, fixtures seed `autonomy.db` + synthetic `tool_index.json` / `diagnostic.json` files. Assertions:
- Detector returns 0 candidates below threshold.
- Detector returns 1 candidate at threshold with correct `pattern_key`, `scope_key`, evidence.
- Detector correctly deduplicates on repeated traces with the same pattern.
- Detector scopes correctly (Sitecore evidence does not produce a Salesforce candidate).

### Integration tests

- End-to-end miner run against a synthetic 14-day history covering all six detectors. Assert the correct number of candidates by detector/scope.
- Approve flow: mock `gh` CLI, assert PR branch name, commit message, and `lesson_id` frontmatter.
- Outcomes: time-travel fixtures (`freezegun` or parameter-injected `now()`) to test the pre/post measurement.

### Drafter tests

- Claude call mocked via `AnthropicStub`. Assert: diff validated by `git apply --check`, absolute-directive filter rejects "always foo", token budget rejects oversized diffs.
- Golden test: given the XCSF30-88424 pattern as input, drafter produces a diff that touches `salesforce-dev-loop/SKILL.md` and adds the anti-pattern entry.

### Regression suite

The six detectors + the XCSF30-88424 pattern become a permanent test — any future miner refactor that breaks them fails CI.

### What we can't easily test

- Prompt quality. We can test that the drafter emits *something valid*, but whether the emitted rule actually changes agent behavior requires a shadow run (Tier 2). For v1 we accept this: the human reviews every diff before merge.

---

## 10. Rollout plan

Vertical-slice approach: one detector end-to-end first (Detector 2 — human_issue_cluster), then broaden. Each phase ships a reviewable increment.

### Phase A — Schema + vertical slice detector (week 1)

- v5 migration (`lesson_candidates`, `lesson_evidence`, `lesson_outcomes`).
- Implement **Detector 2 (human_issue_cluster) only** as a pure function.
- Runner skeleton with per-detector isolation contract (only running Detector 2 for now).
- Backfill: run Detector 2 over existing 60-day autonomy history. Write candidates + evidence to DB.
- Manual SQL inspection to validate that the patterns we'd expect (the external-reviewer findings from 2026-04-06 ADO integration, the 2026-04-10 SF run's "agent skipped scratch-org" Phase-1 class of thing) surface as candidates.

**Exit criteria:** Detector 2 produces ≥2 candidates on the backfill. Zero false positives by manual inspection. Evidence rows link back to real trace IDs.

### Phase B — Dashboard for the one detector (week 1–2)

- `/autonomy/learning` view. Shows Detector-2 candidates only.
- Approve, Reject, Snooze buttons wired to DB state changes only (no PR opening yet).
- Evidence rows render as links opening the existing trace detail page.

**Exit criteria:** Thomas triages the Detector-2 backfill in the dashboard. ≥1 Approve, ≥1 Reject exercised.

### Phase C — Markdown drafter + consistency check (week 2)

- `drafter_markdown.py` with Claude integration.
- `drafter_consistency_check.py` gating promotion to `draft_ready`.
- "Draft diff" button on the dashboard (manual trigger only — no auto-draft yet).

**Exit criteria:** ≥2 approved Detector-2 candidates have Claude-drafted diffs that pass `git apply --check`, absolute-directive filter, and the consistency check.

### Phase D — PR opener + first merged lesson (week 3)

- `pr_opener.py` wired to the Approve button.
- First real PR against the harness repo from an approved Detector-2 lesson.
- PR goes through normal review (Thomas or external reviewer).
- Merge → `merged_commit_sha` recorded on the candidate.
- Edited skill has `lesson_id` frontmatter.

**Exit criteria:** First lesson merged via this flow end-to-end. Vertical slice complete.

### Phase E — Scheduler + outcomes job + human-reedit detection (week 3–4)

- Nightly scheduler enabled (`LEARNING_MINER_ENABLED=true` on the host).
- `outcomes.py` runs for merged lessons after 14-day window:
  - Pre/post FPA, escape rate, catch rate per `(client_profile, scope_key)`.
  - Pattern recurrence (did Detector 2 re-fire on the scope post-merge?).
  - **Human-reedit detection** — `git log` on the edited skill file for commits after `merged_commit_sha` whose author is NOT `xcagentrockwell` AND whose diff touches the lesson's anchor. Records count, SHAs, and authors in `lesson_outcomes`. Verdict flips to `human_reedit` if any found.
- Dashboard surfaces `lesson_outcomes` verdict on each applied candidate.

**Exit criteria:** At least one merged lesson has a `lesson_outcomes` row with non-null pre/post metrics. Dashboard shows outcome verdict. Human-reedit detection verified against a synthetic test commit.

### Phase F — Broaden to Detectors 3, 4, 5 (week 4–5)

- Implement Detectors 3 (judge_over_filters), 4 (first_tool_error_recurrence), 5 (skipped_phase).
- All three reuse existing plumbing — each is a `scan()` function plus unit tests.
- Backfill each and triage via existing dashboard.

**Exit criteria:** Each detector produces ≥1 reviewable candidate on the backfill. ≥1 from each merges as a lesson.

### Phase G — `tool_index.py` extension + Detector 1 (week 5–6)

- Extend `tool_index.py` with `bash_verb_counts` (see §4.1).
- Re-run tool-index over existing traces to backfill the new field on archived `ARTIFACT_TOOL_INDEX` entries.
- Implement Detector 1 (mcp_drift).
- Backfill and triage.

**Exit criteria:** Detector 1 produces ≥1 candidate scoped to `salesforce|sf` on the backfill (the 2026-04-10 pattern).

### Phase H — YAML drafter + Detector 6 (week 6)

- `drafter_yaml.py` with ruamel round-trip and schema validation.
- Implement Detector 6 (autonomy_knob_drift).
- First YAML lesson PR against `runtime/client-profiles/*.yaml`.

**Exit criteria:** First YAML-edit lesson merged. Schema validation enforced on draft.

### Phase I — Detector 7 (plan_drift) (week 7)

- Implement plan-drift detector. Reads `trace-archive/<id>/plan.md` + PR file list (already captured in `pr_runs`).
- Markdown edit path, reuses existing drafter.

**Exit criteria:** Detector 7 produces ≥1 candidate on the backfill.

### Total: 7 weeks calendar, ~3 weeks of focused engineering time

The vertical slice (Phases A–E) is 3–4 weeks and delivers a fully functioning system with one detector. Subsequent detectors are 3–5 days each on top of that foundation.

---

## 11. Metrics — how we know the system is working

Computed weekly, surfaced on the `/autonomy/learning` dashboard header:

- **Detection rate.** Candidates opened per week. Expect ~3–8 in steady state.
- **Approval rate.** Approved / (approved + rejected). Below 40% → detectors are too noisy, tighten thresholds. Above 90% → detectors are too conservative, loosen.
- **Time-to-merge.** Median days from candidate creation to lesson merged. Target: < 7 days.
- **Confirmed-impact rate.** `lesson_outcomes.verdict='confirmed' / total_applied`. Target: > 50%. Below 30% → detectors are finding patterns but the drafted rules aren't changing agent behavior; move to Tier 2.
- **Regression rate.** `verdict='regressed' / total_applied`. Target: < 10%. Above that → human review is not catching bad edits; add pre-merge simulation (Tier 2).

---

## 12. Open questions

1. **Where does the harness repo live on the L1 host?** `pr_opener.py` needs a working clone. Option A: clone on demand into `/tmp/harness-<lesson>`, apply, push, delete. Option B: maintain a long-lived mirror in `LOGS_DIR/harness-mirror/`. Option A is simpler and has no stale-state risk; favoring it unless cold-clone cost becomes painful.
2. **Do we read trace bundles from the live `trace-archive/` dir or from the bundle endpoint?** The endpoint redacts; direct reads don't (but the miner redacts separately). Direct reads avoid an HTTP round-trip per trace. **Proposed: direct reads for mining, redaction inline before DB insert.** Bundle endpoint stays for the post-mortem/discuss workflow.
3. **Handling Jira/ADO ticket types that don't map cleanly.** Detector 6 (`autonomy_knob_drift`) assumes `ticket_type` is a useful dimension. If a client's tickets are mostly untyped, the detector will over-group under `(unspecified)`. **Proposed: skip the detector when `(unspecified)` is > 50% of the profile's ticket_types.**
4. **Cross-profile lessons.** A pattern observed on all three client profiles is probably a *base*-skill issue, not a platform issue. v1 scopes all lessons to a single `(client_profile, platform_profile)`. v2 should add a "promote to base skill" flow — but with explicit human judgment, not automatic.
5. **Concurrency.** What if a PR is open for lesson L1 when lesson L2 proposes an edit to the same file? **Proposed: dashboard shows a "conflicts with open PR" warning; approval is still allowed but the drafter must rebase on the other PR's branch.**
6. **Handling rejected detector outputs.** Should a rejected candidate's pattern ever re-surface? **Proposed: yes, after 30 days, because evidence can change (e.g. the same pattern now has 10× the frequency, suggesting higher priority).**

---

## 13. Tier 2 and Tier 3 sketch (out of scope for v1)

Listed only to preserve continuity so the v1 schema accommodates them without migration.

### Tier 2 — bandit-style knob tuning (after 3 months of v1 data)

- Replace `_recommend_mode()` in `autonomy_metrics.py` with a Beta-Bernoulli posterior per `(client_profile, ticket_type, pipeline_mode)` cell.
- `auto_merge_enabled` still requires explicit human flip.
- The current `recommended_mode` becomes a *lower bound* — the posterior can never recommend a mode higher than what `_recommend_mode` would allow. Keeps the safety floor.

### Tier 3 — shadow-branch evaluation (after Tier 2 is trusted)

- Approved lessons go to a `runtime-shadow/` branch first. L1 routes 10–20% of incoming tickets through the shadow runtime.
- Compare cohort FPA/escape at 14-day window.
- Auto-promote to `runtime/` only if (a) ≥30 shadow samples, (b) metrics non-inferior at 90% CI, and (c) human has approved the underlying lesson PR.
- `lesson_outcomes` table already has `pre_/post_` fields — just needs a `cohort` column added.

---

## 14. What this unlocks

- The 2026-04-10 "MCP drift" pattern that took a full live run + hand-diagnosis + skill-doc rewrite would instead have surfaced in `/autonomy/learning` within hours of the third trace exhibiting it. A Claude-drafted diff would be one click away.
- The `docs/sidecar-rollout-validation.md` playbook — which currently requires humans to manually watch metrics after rollouts — would have `lesson_outcomes` rows doing the same measurement automatically, with a regression flag.
- The `feedback_patterns.md` memory entries (external-reviewer findings are authoritative, real E2E tests, cost awareness) would become *detectable patterns* in future sessions: if the harness systematically gets a type of finding wrong and a human has to correct it 3+ times, `human_issue_cluster` surfaces it.
- Onboarding a new client profile becomes faster: their first 2 weeks of traces produce a tailored stack of lessons that adjust skills for their codebase's quirks.

---

## 15. Next actions

1. Review this plan; mark open questions 12.1–12.6 as resolved or flag concerns.
2. If approved, Phase A is a ~3–4 day build (v5 migration + Detector 2 + runner skeleton + backfill validation). That's the first shippable milestone.
3. Ship Phase A to main behind `LEARNING_MINER_ENABLED=false` by default. Run backfill locally, iterate on Detector 2 thresholds before building the UI. Phase B (the dashboard) is the commit where the system becomes human-interactive.
4. The vertical slice (Phases A–E) delivers a working single-detector self-learning system in 3–4 weeks. That's the milestone to evaluate before committing to the remaining detectors.
