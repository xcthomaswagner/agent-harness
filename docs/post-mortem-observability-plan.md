# Post-Mortem Observability Plan — Debugging Agent Runs on a Per-Dev Harness

**Status:** Draft v3, reframed for per-dev-instance deployment
**Author:** Thomas Wagner (with Claude)
**Date:** 2026-04-11
**Branch:** `feature/ado-mcp-integration` (plan lives here; implementation will fork a feature branch)
**Scope:** L1 Pre-Processing Service — trace dashboard + artifact consolidation + per-trace analysis workflow

**Revision notes (v3):** Reframed from a shared-harness model to a per-dev-instance model. Each developer runs their own L1 locally and only sees their own traces. Multi-tenancy dropped as out of scope (no shared infrastructure). Tier 1a and Tier 1b collapsed into a single unified Tier 1 that ships redaction alongside everything else — the v2 rollout-gate distinction only made sense when there was a "second dev onboarding" trigger event, which there isn't. Week-long dogfood gap removed. Tier 1.5 (discuss-with-claude) folded into the single Tier 1 stretch as its final commits. Ops ownership simplified — no rotation, future-you owns pattern triage.

**Revision notes (v2, retained for history):** Reviewer pass tightened scope. LOC and time estimates recalibrated against existing dashboard code density. Three risks added (multi-line secrets, Bash stderr, multi-tenancy). Operational ownership section added. Trace archive path descriptions fixed — path is derived from `client_repo.parent`, not hardcoded to `/tmp`.

---

## TL;DR

The harness captures enough data to diagnose almost any agent failure — but the data is invisible unless you navigate to worktree directories and know which files to grep. Even for a single developer investigating their own runs, that's 10+ minutes of friction per post-mortem. This plan surfaces the captured data in the existing trace dashboard, adds a tool-call index and a structured diagnostic checklist so anomalies are one-glance visible, ships redaction as baseline hygiene (cheap insurance against leaks even when you're the only audience), and adds a Claude-assisted discussion flow with structured output capture so investigation insights become durable artifacts instead of disappearing when the chat session ends.

**Operating model assumption:** each developer runs their own L1 instance locally. No shared dashboard, no shared trace store, no network exposure. Each dev only ever sees their own traces. This plan is scoped to that model — multi-tenancy, per-client ACLs, and any risk that only exists in a shared-infrastructure world are explicitly out of scope.

**What ships — one unified Tier 1 in a single stretch:**

- **Surface captured data** — consolidate `session.log`, `session-stream.jsonl`, and the effective `CLAUDE.md` into the trace store. Four collapsible dashboard panels to view them.
- **Tool-call index** — compact panel at the top of every trace showing tool counts, error counts, unused MCP servers, first tool error. The single highest-leverage piece.
- **Diagnostic checklist** — six auto-computed checks (platform detected? skill invoked? MCP preferred? first deviation? scratch org correct? QA verdict?). Each check requires two independent signals to turn green — single-signal greens stay yellow by construction.
- **Redaction module** — regex + block patterns + entropy fallback + file-type awareness. Runs at consolidation time so every trace in the store is already safe to share (PR attachment, Slack paste, bug report) without a separate action. Not gated on "second dev arrives"; shipped as baseline hygiene.
- **Bundle endpoint** — `GET /traces/<id>/bundle` returns a gzipped tar suitable for attaching to a PR or bug report. Runs redaction on the way out (belt and suspenders). Useful for a single dev who wants to share a trace with a peer or attach it to a ticket.
- **Discuss-with-Claude workflow** — `POST /traces/<id>/discuss` endpoint + post-mortem-analyst skill + audit log + output-capture hook that extracts (a) one-line root cause, (b) proposed skill/code edit as a diff preview, (c) memory entry, and offers each to the dev for approval. The capture hook is what turns a chat session into a durable artifact.
- **Debugging runbook** — one-page decision tree at `docs/debugging-agent-runs.md`.

**Out of scope (Tier 2/3 — not in this plan):** trace import/replay, cross-run analysis, failure taxonomy, runs-index filtering, regression comparison, per-profile diagnostic modules. Deferred until a specific use case demands them.

**Total effort:** ~1650 LOC across 10 commits, **5 days of focused work**. Ships as a single stretch — no dogfood gap, no phased rollout. Every commit is independently revertable. No new dependencies, no schema migrations, no service restarts required for most commits.

**Why no dogfood gap:** v2 of this plan had a week-long gap between shipping the bundle endpoint and shipping the discuss-with-claude skill, on the theory that we'd learn what devs ask Claude before tuning the skill prompt. In the per-dev model, that gap is theater — you're the dev, you know what you ask, and you can tune the skill prompt based on today's post-mortems (there have been several this session alone). Ship it all at once and iterate on the skill prompt in place if it needs adjustment.

---

## Problem

### What "debugging an agent run" actually looks like today

A ticket fails. A developer wants to answer: *why did the agent do what it did, and what should we change so it doesn't happen again?*

To answer, they need to reconstruct five things:

| # | Question | Where the data lives today |
|---|---|---|
| 1 | What did the agent see? (prompt, tool list, skills, ticket) | `<worktree>/CLAUDE.md`, `.claude/skills/*`, `.harness/ticket.json` |
| 2 | What did the agent decide? (reasoning narrative) | `<worktree>/.harness/logs/session.log` |
| 3 | What did the agent do? (tool calls + results) | `<worktree>/.harness/logs/session-stream.jsonl` |
| 4 | What happened to the code? (diff, commits, PR) | Client git repo + GitHub/ADO |
| 5 | What did downstream judges say? (review, QA, simplify) | `.harness/logs/code-review.md`, `qa-matrix.md`, `simplify.md`, etc. |

### What we show in the dashboard today

`services/l1_preprocessing/tracer.py:700` (`consolidate_worktree_logs`) imports two things into the trace store when the agent finishes:

- **`pipeline.jsonl`** — the structured phase events the agent explicitly writes (plan_complete, qa_complete, etc.)
- **An allowlist of artifact markdown files** — code-review.md, qa-matrix.md, judge-verdict.md, merge-report.md, plan-review.md, blocked-units.md, simplify.md, escalation.md

The trace detail page at `/traces/<ticket-id>` renders these as a span tree with inline artifact panels. **It does not render #2 or #3 above.** `session.log` and `session-stream.jsonl` are archived to `<client_repo.parent>/trace-archive/<ticket-id>/` (on this dev's machine that resolves to `/tmp/ado-repo-init/trace-archive/...`, but the path is derived from the client repo's location, not hardcoded) but nothing in L1 reads them back.

### Why this is broken even for one developer

Every post-mortem done during today's session (and I did several) looked like this:

1. Notice something off in the trace dashboard
2. Open a terminal, navigate to the archive directory
3. `cat <client_repo.parent>/trace-archive/<ticket>/session-stream.jsonl | grep ...`
4. Form hypothesis from raw NDJSON
5. `git log`, `git diff` against the client repo to confirm
6. Edit skill doc / test / main.py
7. Run tests
8. Commit

The first 10 minutes of every post-mortem this session were spent on steps 2–4 — *finding the data*, not *analyzing it*. This isn't a multi-dev problem; it's a me-two-hours-from-now-having-forgotten-where-the-files-live problem. The archive path `<client_repo.parent>/trace-archive/<ticket-id>/` is non-obvious: you have to know where the client repo was cloned to even start looking. Every time I do a post-mortem, the first thing I do is re-derive that path.

Concretely, two findings from today's session that would have been obvious from a tool-call index but cost 15+ minutes each of grepping to confirm:

- **XCSF30-88424 shelled out to `sf` CLI instead of using `mcp__salesforce__*` tools.** Required reading the 125-line session-stream.jsonl and counting tool names by hand.
- **XCSF30-88424 skipped Phase 1 scratch org bootstrap entirely.** Required noticing DevHub in the QA matrix evidence field and cross-referencing with the session stream.

Both of those findings should have been a one-glance observation at the top of the trace detail page.

---

## Design

One unified Tier 1 that ships all of the below in a single stretch. Each sub-section maps to one bundled commit in the implementation plan. Everything is in scope for a per-dev-instance harness — no features gated on "multi-dev readiness" or "shared-host onboarding," because there is no shared host.

### 1.1 Extend the consolidation allowlist

`tracer.py:755` currently has:

```python
artifact_files = {
    "code-review.md": "code_review_artifact",
    "qa-matrix.md": "qa_matrix_artifact",
    # ...
}
```

Add three entries:

```python
    "session.log": "session_log_artifact",          # assistant narrative
    "session-stream.jsonl": "session_stream_artifact",  # full tool calls
    "CLAUDE.md": "effective_claude_md_artifact",     # the merged prompt
```

Special handling:
- `session-stream.jsonl` is potentially megabytes, not truncatable to 5000 chars like the current artifacts. Keep the raw file and store only a reference (`artifact_path`) that the dashboard fetches on demand. This is the answer to reviewer open question #3 — keep the 5000-char truncation for small human-readable artifacts (code-review.md, qa-matrix.md) but store session-stream.jsonl by reference instead.
- `CLAUDE.md` in a worktree is the *merged* instructions (client's CLAUDE.md + the injected harness-CLAUDE.md). That's the right file to capture — it's what the agent actually saw. This one is small enough that the 5000-char truncation is fine as a preview, with a "full file" link to the reference on disk.

**Cost:** ~20 lines in `tracer.py` plus the reference-path storage mechanism (~30 lines).

### 1.2 Tool-call index

During consolidation, parse `session-stream.jsonl` once and compute:

```python
{
    "tool_counts": {"Bash": 20, "Read": 13, "mcp__salesforce__sf_deploy": 3, ...},
    "tool_errors": {"Bash": 1, "mcp__salesforce__sf_scratch_create": 1},  # is_error=True
    "mcp_servers_used": ["salesforce", "github"],
    "mcp_servers_available": ["salesforce", "github", "playwright", ...],
    "mcp_servers_unused": ["playwright"],  # connected but never called
    "first_tool_error": {"tool": "Bash", "line": 47, "message": "..."},
    "assistant_turns": 23,
    "tool_call_count": 40,
}
```

Store as a single JSON blob on the trace summary. Rendered at the top of the trace detail page as a compact panel:

```
┌─ Tool Usage ────────────────────────────────────────┐
│  40 tool calls across 23 assistant turns            │
│                                                      │
│  Bash:  20   Read: 13   Edit: 2   TodoWrite: 2      │
│  mcp__salesforce__sf_deploy:   3                    │
│  mcp__salesforce__sf_apex_test: 3                   │
│  mcp__salesforce__sf_scratch_create: 2 (1 error)   │
│                                                      │
│  ⚠ MCP server "playwright" connected but never used │
│  ⚠ First tool error: Bash at line 47                │
└──────────────────────────────────────────────────────┘
```

**This is the single highest-leverage piece of the whole plan.** Every session's post-mortem today would have been faster with this one panel. Computed once at consolidation and cached on the trace summary so it doesn't re-parse session-stream.jsonl on every render.

**Key design call:** the index is *declarative evidence*, not interpretation. We count things and flag obvious anomalies (unused MCP server, tool error). We do **not** try to write English explanations here — that's the job of the diagnostic checklist (1.4) and the discuss-with-Claude workflow (1.7).

**Cost:** ~80 lines of index computation in `tracer.py` or a new `services/l1_preprocessing/tool_index.py`, plus ~40 lines of dashboard rendering.

### 1.3 New dashboard panels

Add four collapsible panels to `trace_dashboard.py::_render_detail`:

1. **Agent Instructions** — renders the captured CLAUDE.md. Useful when the question is "why did the agent think X was allowed?" Answer often jumps out from reading what the prompt actually said.
2. **Reasoning Narrative** — renders `session.log` (the extracted assistant text blocks). Useful for "what was the agent thinking when it made this call?"
3. **Tool Calls Timeline** — renders a scrollable list from `session-stream.jsonl`. Each row: timestamp + tool name + truncated args (200 chars) + truncated result (500 chars — individual row truncation, not just the first-100-rows limit) + a "show full" toggle. Paginate at 100 rows with a "load more" button. Color-coded by tool category (bash red, read blue, mcp green, etc.) for at-a-glance pattern recognition.
4. **Raw Downloads** — three links: full session.log, full session-stream.jsonl, effective CLAUDE.md. Served from the redacted trace store (not the raw archive files), so a dev can copy-paste from the download output into a PR comment without worrying about leaking a token. A "🔒 redacted" badge on the panel makes the safety guarantee explicit.

All four panels default to collapsed. The goal is: skim the span tree and tool-call index first, open the panels only if the top-level signals don't explain the failure.

**Cost:** ~280 lines of new rendering code in `trace_dashboard.py` (recalibrated upward from the v1 estimate — existing `_section` helper is 25 lines per panel, plus timeline row rendering, plus pagination). Zero JavaScript — keep the pattern of server-rendered HTML that already exists. If a trace has >500 tool calls, pagination handles it; JS is not needed.

### 1.4 Diagnostic checklist panel

This is the behavioral change that matters most for a new developer who has never seen the harness before.

Render a six-item checklist at the top of the detail page, each item computed from the trace data at render time:

| Item | How computed | Green / Yellow / Red condition |
|---|---|---|
| Platform detected correctly? | Check for "PLATFORM: SALESFORCE" / "PLATFORM: GENERIC" / etc. in the first few session-stream messages | Green if platform block present **and** it matches repo signals (e.g., `sfdx-project.json` exists for salesforce). Single-signal greens stay yellow. Red if mismatch detected. |
| Expected skill(s) invoked? | Look for `Skill(...)` tool uses OR Read calls to `.claude/skills/<expected>/SKILL.md` | Green if the skill matching the platform was invoked **and** its supporting files (SCRATCH_ORG_LIFECYCLE.md etc.) were also read. Yellow if only SKILL.md was read. Red if neither. |
| MCP tools preferred over shell? | Ratio of `mcp__*` tool uses vs `Bash` tool uses containing known CLI commands (`sf `, `gh `, etc.) | Green if ratio > 1 **and** shell CLI count is zero for commands with an available MCP equivalent. Yellow if mixed. Red if all-shell. |
| First deviation point | Find the first `tool_result` with `is_error=True` OR the first QA/review finding | A green "no deviations" or a yellow/red with timestamp + line reference |
| Scratch org / environment correct? | Check for `sf_scratch_create` call for SF, appropriate env var for other platforms | Platform-specific check. Green only if the call succeeded AND the resulting org alias starts with `ai-`. Yellow if scratch was skipped but platform is not SF. |
| Review / QA verdict | Read from the trace summary | Green/yellow/red from APPROVED / PASS_WITH_NOTES / FAIL |

This is the part that encodes tribal knowledge. Every check represents a "thing an experienced dev would know to look at first." New devs don't need to remember the list — the page shows it to them.

**Cost:** ~200 lines of analyzer code in a new `services/l1_preprocessing/diagnostic.py` (recalibrated upward — session-stream parsing doubles the LOC from the v1 estimate) + ~80 lines of dashboard rendering. Tests: one unit test per checklist item with a fixture trace that should produce each outcome.

**Design constraint (hardened after reviewer pushback):** the checklist must **never** be wrong in a way that wastes the dev's time. False green is the worst failure; false red is tolerable.

Bias mechanisms, not just principle:
- **Every green check requires two independent signals.** Single-signal greens stay yellow. This is the rule that prevents a single flaky check from decaying to "just mark it green, too noisy." The reviewer's point: "Bias to yellow when unsure" is a principle, not a mechanism. Require structural evidence.
- **Any red check highlights the Discuss-with-Claude button** — a red finding pushes the button to the top of the page as "suggested next step."
- **Yellow checks show the relevant data inline** so the dev can judge for themselves without drilling into panels.

### 1.5 Bundle endpoint

`GET /traces/<ticket-id>/bundle` returns a gzipped tar containing: trace JSONL + session-stream.jsonl + session.log + effective CLAUDE.md + relevant skill files + qa-matrix.md + code-review.md + ticket.json + tool-call index JSON. All content pulled from the (redacted) trace store, not the raw archive files — so the bundle is safe to attach to a PR, paste into Slack, or hand to a peer without a separate redaction step.

Use cases even for a single developer:
- Attaching a trace to a PR description so the reviewer can see exactly what the agent did
- Including a trace in a bug report against the harness itself
- Handing a failed run to a peer who runs their own harness instance and wants to see why yours broke
- Archiving interesting runs outside the rolling `trace-archive/` directory for long-term reference

The bundle endpoint runs the redactor on the way out even though the trace store is already redacted — belt and suspenders. If a pattern update lands between consolidation and export, the export catches it.

**Cost:** ~100 lines for the endpoint (mostly tar+gzip streaming).

### 1.6 Redaction module

Runs at consolidation time, so every trace entry in the store is already safe to share from the moment it lands. Redaction is not gated on a rollout event — it ships in the same stretch as everything else as baseline hygiene. Even in a single-developer setup, redaction matters because (a) copy-paste mistakes leak tokens from dashboard panels into external places, (b) the bundle endpoint enables sharing and the safe-by-default guarantee should hold the moment the bundle is generated, not require a separate action, and (c) I already accidentally leaked two live tokens into conversation transcripts during this session alone — redaction is cheap insurance against future-me making the same mistake at 11pm.

A new module `services/l1_preprocessing/redaction.py` with:

```python
# High-signal patterns for known credential shapes. False positives are
# acceptable — we'd rather redact a hex hash than leak a Bearer token.
_PATTERNS = [
    (re.compile(r"sk-ant-[\w-]{30,}"), "sk-ant-[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "ghp_[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "github_pat_[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9+/._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"https://[^@\s]+:[^@\s]+@"), "https://[REDACTED]@"),  # git URLs
    (re.compile(r'"access_?[Tt]oken"\s*:\s*"[^"]+"'), '"access_token":"[REDACTED]"'),
    (re.compile(r'"password"\s*:\s*"[^"]+"'), '"password":"[REDACTED]"'),
    (re.compile(r'00D[A-Za-z0-9]{12}![\w.]+'), "[SF_TOKEN_REDACTED]"),  # Salesforce
    # ... one per known credential type, extensible
]

# Multi-line secret patterns — run before line-by-line patterns.
_BLOCK_PATTERNS = [
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----",
                re.DOTALL), "[PRIVATE_KEY_REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[JWT_REDACTED]"),
]

def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, redaction_count). Applies block patterns first
    (multi-line secrets) then per-line patterns."""
```

`consolidate_worktree_logs` calls `redact()` on every line of session-stream.jsonl, every line of session.log, and every artifact markdown before writing them into the trace store. Redaction count is stored on the trace summary so the dashboard can show a "🔒 Secrets were redacted from this trace" warning badge.

**Changes from v1 after reviewer pushback:**
- **Added block patterns** for multi-line secrets (RSA keys, JWTs, OpenSSH private keys). The reviewer correctly flagged that line-regex redaction misses multi-line credentials — a single RSA private key spans dozens of lines and the existing patterns would catch none of them.
- **Added Bash stderr handling.** The redactor must also run on stderr captured from Bash tool calls, not just stdout. The reviewer's point: `sf` CLI prints its full argv to stderr on failures, which often includes secrets via `--target-org <alias>` expansion. Enforced by running redaction on the whole tool-result content blob, which includes both streams.
- **Added entropy-based flagging.** A line that doesn't match any known pattern but contains a high-entropy substring >40 chars (Shannon entropy >4.5 bits/char is a reasonable threshold) is flagged `possibly_contains_secret=true` and rendered in the UI as `[FLAGGED — click to reveal]`. Reveal requires confirmation. This is the mechanism, not just the aspiration the v1 doc referenced.
- **Idempotent by design.** Running the redactor on already-redacted text is a no-op. This is load-bearing for the "re-redact all traces" admin button when patterns are updated.

Tests: for each pattern, one positive test with a fake credential and one negative test to confirm the surrounding text is preserved. Goal is >15 test cases covering the patterns we actually care about including the multi-line RSA/JWT cases.

**Cost:** ~250 lines for the redaction module + ~50 lines wiring it into `consolidate_worktree_logs` + ~100 lines of tests.

**Additional pieces wired in:**

- **`POST /admin/re-redact`** — re-runs the redactor over all existing trace entries when patterns are updated. Idempotent.
- The bundle endpoint runs the redactor again on every file on the way out, even though the trace store is already redacted. Belt and suspenders.

### 1.7 Discuss-with-Claude workflow

**What ships:**

1. A dedicated post-mortem-analyst skill at `runtime/skills/post-mortem-analyst/SKILL.md` (~100 lines, matching existing skill size). System prompt requires diagnostic-checklist-first analysis with line-number citations from session-stream.jsonl, skeptical pushback on leading questions, and structured output (root cause + proposed edit + memory entry).
2. A `POST /traces/<ticket-id>/discuss` endpoint that wraps the bundle endpoint (1.5) with a short-lived session token.
3. **Audit log** at `<trace_store>/discuss-audit.jsonl` — one line per invocation recording when + which trace. Useful even in a one-dev setup as a cheap record of "what did I investigate when," like git log for investigations.
4. An output-capture hook that reads the final Claude session transcript and extracts: (a) one-line root cause, (b) proposed skill-doc edit as a diff preview, (c) memory entry file write. Each is offered to the dev for approval before applying. **This is the piece that turns chat sessions into durable artifacts** — without it, insights evaporate when the chat closes and you have to re-remember them next week.
5. A "Discuss with Claude" button on the trace detail page as the primary entry point to the workflow.

**System prompt for the post-mortem-analyst skill** (shorter than v1 because the checklist logic lives in `diagnostic.py` from 1.4 — the skill reads the precomputed output instead of re-deriving it):

> You are a post-mortem analyst for the agent harness. The failed trace bundle is attached at `./trace-bundle/`. Your job:
>
> 1. **Read the diagnostic checklist output first** (`./trace-bundle/diagnostic.json`). It was pre-computed by the dashboard. Do not re-derive the findings — read them and extend them with line-level evidence from `session-stream.jsonl`.
> 2. **Cite the data, always.** Every claim you make must reference a specific line number in `session-stream.jsonl`, a phase in `pipeline.jsonl`, or a section of the artifact markdown. If you can't cite it, say "I don't have evidence for that yet" and ask for more data.
> 3. **Push back on leading questions.** If the developer asks "the agent ignored the skill, right?" verify against the data before agreeing. The developer is often correct, but if they are wrong you must say so.
> 4. **Propose concrete follow-ups.** When the conversation reaches a conclusion, output three sections: (a) `## Root cause` — one line, (b) `## Proposed fix` — a code diff or skill-doc edit, (c) `## Memory entry` — text the dev should save to persistent memory.
> 5. **Never invent data.** If a file is missing or a field is absent, say so. Do not hallucinate counts, timestamps, or tool names.

**Cost:** ~100 lines for the endpoint + token + audit log + ~80 lines for the output-capture hook + ~100 lines for the skill doc. Total ~280 lines. The skill prompt is iterated in place against real traces as they come up — there's no dogfood gap, because you're the dev and the post-mortems you do during the build week are themselves the iteration signal.

**Alternative considered and rejected:** embedding the chat directly in the dashboard as an iframe or a WebSocket-backed chat widget. Rejected because (a) it would require us to bring our own LLM auth into the dashboard, (b) it would duplicate what Claude Code already does well, (c) it would prevent the dev from using their existing Claude Code session context / memory / skills. Handing off to a local `claude` session is both cheaper to build and more useful.

---

### What this plan does NOT include (deferred)

These are valuable but out of scope. Listed here so reviewers see the arc.

#### Deferred (ship when a specific use case demands it)

- **Trace import / replay.** `POST /traces/import` loads a bundle from another dev's L1 into yours. Useful for "can you see why this broke on my machine" peer debugging. The 1.5 bundle endpoint is the export side; the import side waits until a real peer-debug request happens.
- **Re-run button.** One-click re-dispatch of the same ticket with the same inputs into a fresh worktree. Useful for fix iteration loops.
- **Trace comments.** Annotate traces with notes, link related traces, mark findings.
- **Retention policy.** `trace-archive/` grows forever. JSONL files are small; a single dev's machine can hold 6 months of runs. Revisit when total disk use hits 1GB.
- **Runs index with filters.** Platform/source/outcome/coverage filters on the list view. Useful only when a single dev has hundreds of runs to sift through.
- **Failure-mode taxonomy.** Pattern-match common failure shapes and tag traces automatically. Requires a corpus of failures first.
- **Regression comparison view.** Diff two traces side-by-side.
- **Cross-run metrics.** "How often does the SF dev-loop skill get followed?" Requires taxonomy as input.
- **Per-profile diagnostic checklist modules.** If 4+ platforms diverge significantly, the conditional-rendering approach in 1.4 becomes per-profile modules. For the current 2 platforms (sitecore, salesforce) conditional rendering is correct.

**Explicitly NOT on this list:** multi-tenancy, per-client dashboards, per-trace ACLs, shared-host authz, network-exposed dashboard. Out of scope by operating model — each dev runs their own L1 locally and only sees their own traces. If that changes (shared infrastructure, client engagements sharing a dashboard), a new plan is needed; retrofitting this one would be the wrong move.

---

## Risks and pitfalls

These are the things most likely to cause Tier 1 to ship broken or to make the team's debugging experience worse, not better. Listed so reviewers can push back on whichever ones they think I'm underweighting.

### 1. Redaction false negatives (HIGH)

The redaction pass is regex-based. Regex will miss novel credential shapes — a client-specific token format we haven't seen, a new kind of API key, a base64-encoded secret that doesn't match any pattern. **The failure mode is quietly rendering a live credential in the dashboard, then copy-pasting it somewhere shareable without realizing.** Even in a single-developer setup this matters — copy-paste mistakes from dashboard panels into PR comments, Slack messages, or conversation transcripts are exactly how secrets leak in practice.

Mitigations (baked into the design, not aspirational):
- **Entropy-based fallback flagging is built in, not hand-waved.** Any line containing a high-entropy substring (>40 chars, Shannon entropy >4.5 bits/char) that doesn't match a known pattern is flagged `possibly_contains_secret=true` and rendered as `[FLAGGED — click to reveal]`. Reveal requires confirmation.
- **Block-pattern pass runs first** for multi-line secrets (RSA keys, JWT triplets, OpenSSH private keys). Line-regex alone misses a dozens-of-lines RSA key entirely.
- **Bash stderr is redacted too**, not just stdout. `sf` CLI error messages include full argv with expanded env vars. The redactor runs on the whole `tool_result` content blob.
- **"Report a redaction miss" button per trace.** Each report becomes a new regex pattern in the next deploy. Ownership: future-me, since future-me is the only dev. Reports batched into `<trace_store>/redaction-reports.jsonl` for weekly triage, not fire-drilled.
- **Log every redaction hit count to L1's stdout** so anomalous runs are visible (zero redactions on a run that touched `.env` = bug; 1000 redactions on a run that touched nothing = false positive).
- **Never render the raw archive file from disk in the dashboard.** All content shown comes from the redacted trace store. The on-disk archive files remain the escape hatch if future-me genuinely needs an unredacted value (one filesystem `cat` away, no UI path).

### 2. Dashboard becomes slow on large traces (MEDIUM)

A verbose agent run can produce a session-stream.jsonl in the low megabytes. A single `tool_result` payload can be 10KB+. Rendering a 10,000-entry tool-call timeline in server-side HTML will make the detail page take several seconds to load, and the meta-refresh (currently 5s for live traces) will compound the cost.

Mitigations:
- **Paginate the tool-call timeline** at 100 rows with "load more" button.
- **Truncate individual tool-result payloads** to ~2KB with a "show full" expansion toggle. The reviewer was right that pagination alone is insufficient — the first 100 rows can still ship megabytes if a single payload is large.
- **Compute the tool-call index once at consolidation time** and cache it as a summary JSON. Don't recompute on every render.
- **For the raw-downloads panel, serve files as static resources** with range requests, not through the dashboard render path.
- **Meta-refresh already stops on terminal state** (shipped earlier this session) — no additional interval scaling needed for now.

### 3. Checklist false-greens (HIGH)

Covered above but worth repeating: a checklist that says "looks fine" when something is subtly broken is much worse than no checklist at all, because it trains the dev to skip investigation.

The reviewer pushed back on "bias to yellow when unsure" as principle-not-mechanism. That's fair. The **mechanism** is:

- **Every green check requires two independent signals.** A single check that observes one piece of evidence stays yellow by construction. Only a check that observes both the positive signal AND the corroborating signal (e.g., "platform block present" AND "matches repo signals") can turn green.
- **Red checks block hiding the bundle/discuss button.** If anything is red, the next-step button is highlighted and suggested as the natural next action.
- **Yellow checks show relevant data inline** so the dev can judge for themselves without drilling into panels.

These are structural, not aspirational. A future contributor can't tune a check from yellow to green without breaking the two-signal rule.

### 4. Developers don't know the workflow exists (MEDIUM)

Classic observability pitfall. Tooling is built, nobody uses it, original author gets asked the same questions anyway.

Mitigations:
- A one-page **investigation runbook** at `docs/debugging-agent-runs.md` with screenshots and a decision tree. Highest-value doc to write at the same time as the feature, not after.
- Link to the runbook from the trace detail page header ("First time debugging a run? Start here.").
- Add an onboarding entry to `docs/client-onboarding-guide.md`.
- When a dev asks the author for help, respond with the runbook URL first, then offer to pair.

### 5. Secrets leaking through paths the redactor doesn't cover (HIGH) — NEW from reviewer

**Reviewer catch.** The v1 redactor was line-regex only. Three specific blind spots to close:

- **Multi-line secrets** — RSA private keys, OpenSSH private keys, multi-line JWT payloads. Line-by-line regex catches none of these. Fix: block-pattern pass runs before line-pattern pass, using `re.DOTALL` against the full artifact before splitting into lines.
- **Bash stderr** — `sf org display --json` dumps access tokens, `sf` CLI errors include expanded `--target-org` argv, `curl -v` dumps Authorization headers. The redactor must run on the full `tool_result` content blob (stdout + stderr combined), not just stdout.
- **File contents the agent read** — if the agent ran `Read` on a `.env`, a `credentials.json`, or a private key file, the raw content is in the `tool_result` payload. File-type-aware handling: redactor checks the tool-call `input.file_path` and applies stricter rules when the path matches sensitive-file patterns (`*.env`, `*.pem`, `*/secrets/*`, `id_rsa*`, etc.). When matched, the whole file content is replaced with `[REDACTED — sensitive file: <path>]` rather than attempting pattern-by-pattern matching.

Mitigations baked into the redaction module (commit 5). Tests must cover all three cases with at least one positive + one negative per pattern type.

### 6. Ops burden of running the redactor long-term (MEDIUM)

Every false-positive redaction report is triage work, and every missed-secret report is a fire drill that ships a new pattern. On a solo harness, that work falls on future-me forever.

Mitigations:
- **Reports land in a structured file** at `<trace_store>/redaction-reports.jsonl` so triage can be batched weekly rather than fire-drilled.
- **Pattern updates ship via the existing `_PATTERNS` list**, not a database. Adding a pattern is a 1-line code change + a test; deploy is the normal L1 restart. No bespoke admin UI needed.
- **Automation escape hatch:** if pattern-list churn exceeds ~1 update/week, that's a signal the regex approach is breaking down and we should invest in a proper secret-scanning library (`trufflehog` or `gitleaks` as a subprocess). Named here so the future-me who trips over it has a documented pivot.

### 7. Scope creep into Tier 2/3 (MEDIUM)

Writing a plan that mentions Tier 2 and Tier 3 tempts the implementer (me) to build them anyway. That's how 2-day features become 2-week features.

Mitigation: every PR landed from this plan must close at least one explicit Tier 1 item (1.1 through 1.7) and add zero Tier 2 or Tier 3 code. Deferred features do not exist until a specific use case demonstrates they are needed.

---

## Operational ownership

Single-developer operating model. Ownership exists only to make future-me's job explicit so it doesn't get forgotten:

- **Redaction false-positive triage:** future-me. Reports batched weekly from `<trace_store>/redaction-reports.jsonl`.
- **Redaction pattern updates:** future-me. 1-line code change + test + L1 restart.
- **Dashboard bug reports:** future-me. Routine harness maintenance.
- **Skill-doc regression from post-mortem findings:** future-me. Same workflow as today's memory-update loop.
- **Escalation path when something's wrong:** no peer to escalate to. Instead: disable feature via env flag, revert the commit, repro the problem, fix forward. Document the fix in memory so future-me sees it next session.

If redaction pattern churn ever exceeds ~1 update/week, pivot to a secret-scanning library (`trufflehog` or `gitleaks` as a subprocess) rather than adding more regex patterns. Named here as a documented escape hatch.

---

## What the workflow looks like after shipping

This is the test for whether the plan is worth shipping. Walk through a concrete failure scenario and see if the new workflow is actually better.

**Scenario:** A new developer joins the team. They fire their first test ticket at the harness. It fails. They've never seen the codebase, never read any of the skill docs, never grep'd a session-stream in their life.

**Today:**

1. They see "failed" in their email notification.
2. They open the trace dashboard, see the trace, see that QA failed.
3. They don't know why. The trace shows "QA complete: FAIL" and nothing more useful.
4. They Slack the team lead: "My ticket failed, why?"
5. Team lead SSHes to the host, greps the archive, finds the problem in 10 minutes.
6. Reports back: "The skill didn't load because X."
7. Dev asks: "Why didn't the skill load?" Team lead debugs further.
8. **Total time:** 30+ minutes of one senior dev's attention.

**Scenario:** A ticket fails. You (single dev) want to know why and fix it.

**Today:**

1. You get notification of the failed run.
2. You open the trace dashboard and see "QA: FAIL" — nothing more.
3. You `cd` to `/tmp/ado-repo-init/worktrees/ai/<ticket-id>/.harness/logs/` (or the archive path if it was cleaned up).
4. You grep `session-stream.jsonl` for tool uses, count them by hand, cross-reference with the QA matrix, form a hypothesis.
5. You edit a skill doc, run tests, re-fire the ticket.
6. **Total time:** 25–40 minutes depending on how deep the hypothesis chase goes and how recently you touched the codebase.

**After shipping this plan:**

1. You click the trace URL from the notification.
2. Dashboard shows:
   - ✅ Platform detected
   - 🟡 Expected skill read but never invoked  ← **the problem, one glance**
   - 🔴 All SF operations went through shell instead of MCP
   - 🔴 Scratch org never created — deploys targeted DevHub
   - 🔴 QA: FAIL (coverage 67% — below gate)
   - Tool-call index panel at the top shows `Bash: 20`, `mcp__salesforce__*: 0`, `MCP server "salesforce" connected but never used`
3. You click "Discuss with Claude." A local `claude` session opens with the post-mortem-analyst skill pre-loaded, the diagnostic output already computed, and the trace bundle as context.
4. You ask: "Why did it shell out instead of using the MCP?" Claude cites `session-stream.jsonl:47` — the agent read `salesforce-dev-loop/SKILL.md` but never invoked the skill as a slash command. Proposes a specific edit to `harness-CLAUDE.md` that forces the invocation.
5. Output-capture hook offers the edit as a diff preview. You accept, and the edit is applied. Offers a memory entry so next session sees the fix. You accept that too.
6. You re-fire the ticket.
7. **Total time:** 5–10 minutes.

**3–6x productivity multiplier per post-mortem**, with the added benefit that the fix is codified in a skill doc instead of lost to chat history. Across a weekly cadence of post-mortems, the plan pays for itself inside the first month.

---

## Implementation plan (after approval)

Each row is one commit. Land them in order, each independently revertable. **Single stretch — no phases, no dogfood gap, no rollout events.**

| # | Commit | Est. LOC | Files |
|---|---|---|---|
| 1 | feat(l1): consolidate session streams + tool-call index | ~260 | `tracer.py`, `tool_index.py` (new), `test_tracer.py`, `test_tool_index.py` (new) |
| 2 | feat(l1): dashboard panels for context & session | ~280 | `trace_dashboard.py`, `test_trace_dashboard.py` |
| 3 | feat(l1): diagnostic checklist analyzer + panel | ~280 | `diagnostic.py` (new), `trace_dashboard.py`, `test_diagnostic.py` (new) |
| 4 | feat(l1): trace bundle endpoint | ~100 | `main.py`, `trace_dashboard.py`, `test_webhooks.py` |
| 5 | feat(l1): secret redaction module with block patterns + entropy fallback | ~300 | `redaction.py` (new), `test_redaction.py` (new) |
| 6 | feat(l1): wire redactor into consolidation + bundle + re-redact admin endpoint | ~100 | `tracer.py`, `main.py`, `test_tracer.py`, `test_webhooks.py` |
| 7 | feat(l1): discuss-with-claude endpoint + audit log | ~130 | `main.py`, `test_webhooks.py` |
| 8 | feat(runtime): post-mortem-analyst skill | ~100 | `runtime/skills/post-mortem-analyst/SKILL.md` (new) |
| 9 | feat(l1): output-capture hook for Claude session fixes + Discuss button wiring | ~100 | `main.py`, `scripts/capture_discuss_output.py` (new), `trace_dashboard.py` |
| 10 | docs: debugging-agent-runs runbook | ~0 code | `docs/debugging-agent-runs.md` (new) |

**Total:** ~1650 LOC across 10 commits, **5 days of focused work**. Every commit is independently revertable. No new dependencies, no schema migrations, no service restarts required for most commits (dashboard is hot-reloadable).

**Sequencing rationale:**
- Commits 1-4 are the "surface and diagnose" layer. After commit 4 lands, a single dev can diagnose a failed run from the dashboard without SSH or shell access. Redaction hasn't shipped yet, but the dashboard is localhost-only and this is the single-dev operating model — not a security regression.
- Commits 5-6 ship redaction. After commit 6 lands, every trace in the store is already safe to share from a PR comment or Slack paste without a separate action. This is baseline hygiene, not a rollout gate.
- Commits 7-9 ship the discuss-with-Claude workflow. By the time commit 9 lands, the skill prompt has been iterated in place against real post-mortems done during the build week (commits 1-6).
- Commit 10 is docs. Runbook written last so it reflects what actually shipped, not what I thought would ship.

**LOC estimates** are grounded in existing `trace_dashboard.py` code density (833 lines, ~25 LOC per `_section` helper) rather than wishful thinking. A 10% overrun on ~1650 LOC is still a manageable ~5.5-day stretch, not a re-plan event.

---

## Resolved decisions (from reviewer pass and v3 reframing)

The v1 doc ended with six open questions. The v2 reviewer pass answered them. The v3 reframing (to per-dev-instance operating model) revisited several. Current state:

1. **Discuss-with-Claude: integrated or bundle-export-only?** → **Integrated workflow ships in the same stretch as everything else.** v2 deferred this behind a dogfood gap on the theory we'd learn what devs ask. In the per-dev model that theory collapses — I'm the dev, the post-mortems from this week's sessions are the iteration signal, and there's no gap worth waiting out. The full discuss workflow (endpoint + skill + audit log + output-capture hook) ships as commits 7-9.

2. **Redaction at consolidation time or render time?** → **Consolidation time.** Write once, read many. Render-time would be CPU waste and leave the trace store raw on disk (worst of both worlds). Admin `POST /admin/re-redact` endpoint handles pattern-update backfills. Decision unchanged from v2.

3. **5000-char artifact truncation?** → **Keep it for small artifacts (code-review.md, qa-matrix.md, CLAUDE.md preview). Replace with reference storage for session-stream.jsonl.** Don't raise the global limit — it would inflate every trace entry without helping. Decision unchanged from v2.

4. **Diagnostic checklist platform-configurability?** → **Conditional rendering in a single `diagnostic.py` module, not per-profile modules.** Today there are 2 platform profiles and maybe 1-2 of the 6 checklist items are platform-specific. Per-profile modules are the right architecture when 4+ platforms diverge; that's deferred. Decision unchanged from v2.

5. **Retention policy?** → **No, deferred.** JSONL files are small; a single dev's machine can hold 6 months of runs. Revisit when disk use hits 1GB.

6. **Audit log for discuss flow?** → **Yes, ships with commit 7.** Useful even in a one-dev setup as a cheap record of "what did I investigate when." Decision unchanged from v2.

7. **Ship everything in one stretch, or phase it?** → **One stretch.** v2 phased the work into Tier 1a (safe for single-dev) + Tier 1b (gated on multi-dev) + Tier 1.5 (after dogfood gap). The per-dev operating model collapses all three gates: there's no multi-dev rollout event, there's no need to ship redaction separately, and the dogfood gap is moot when the dev running it is also the dev iterating it. One unified Tier 1 in five days of focused work.

8. **Multi-tenancy?** → **Out of scope.** Each dev runs their own local L1 and only sees their own traces. If that operating model changes in the future, a new plan is needed; retrofitting this one would be the wrong move.

---

## Decision requested

**Approve the plan as-is, approve with modifications, or reject.**

If approved, I'll start on commit #1 (consolidate session streams + tool-call index) and ship the 10 commits in order over 5 focused days. If modifications, list them and I'll revise this doc before coding.

The two pieces I want explicit sign-off on before touching code:

1. **The single-stretch sequencing** — everything ships in one pass, no dogfood gap, no rollout events. Reversing this after the first commit lands is expensive.
2. **Multi-tenancy is out of scope and the operating model is per-dev-instance** — if you want any hedging toward a future shared-harness world baked into the design (per-trace client tagging, tenant-aware storage, etc.), say so now. The current plan makes zero concessions to that future because YAGNI, and I don't want to rediscover a concession I should have made while commit 5 is in-flight.

Everything else I'm confident I can iterate on if the first pass isn't quite right.
