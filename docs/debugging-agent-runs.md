# Debugging Agent Runs — Investigation Runbook

A ticket failed. You want to know why and what to change. Follow this page.

## The three-minute version

1. Open the trace detail page: `http://localhost:8000/traces/<ticket-id>`
2. Look at the **Diagnostic Checklist** panel at the top. Red checks = the problem. Yellow = ambiguous, worth investigating.
3. Look at the **Tool Usage** panel underneath. Unused MCP servers, high Bash counts for a platform that has MCP tools, or a first-tool-error pointer will usually identify the root cause in one glance.
4. If the two panels don't explain it, click the **🔍 Open in Claude for investigation** disclosure and follow the three-step command.

That's the whole flow. The rest of this doc explains what you're looking at, why it matters, and what to do when the two panels don't tell the whole story.

---

## What's on the trace detail page (in order)

When you open `/traces/<ticket-id>` you see, from top to bottom:

1. **Title and status badge** — the ticket ID and current pipeline status (Complete, Failed, Dispatched, etc.)
2. **Diagnostic Checklist** — six auto-computed checks: platform detected correctly, expected skill invoked, MCP tools preferred over shell, first deviation point, scratch org correct, review/QA verdict. Red checks float to the top, yellow next, green dimmed.
3. **Summary bar** — mode (simple/full), review verdict, QA result, token counts, PR link
4. **Investigate / Discuss boxes** — two collapsible `<details>` elements:
   - **Copy investigation command** — cheap, no auth, copies a shell snippet that curls the bundle and launches `claude -p`
   - **Open in Claude for investigation** — audited, requests a session token from `/traces/<id>/discuss` (writes to `discuss-audit.jsonl`), returns a pre-filled command
5. **Phase duration bar** — colored bar showing time spent in each pipeline phase
6. **Failure box** (if any errors occurred)
7. **Session panels** — Tool Usage (always visible) + four collapsible panels (Agent Instructions, Reasoning Narrative, Tool Calls Timeline, Raw Downloads)
8. **L1 / L2 / L3 span tree** — the existing Langfuse-style tree view
9. **Raw events** — flat NDJSON view of every trace entry

## Diagnostic checklist — what each check means

| Check | Green means | Yellow means | Red means |
|---|---|---|---|
| Platform detected correctly | The platform block was emitted AND matches `platform_profile` from the client profile | Only one signal present (either block or profile) | Signals disagree (e.g. block says `generic` but repo has `sfdx-project.json`) |
| Expected skill invoked | `Skill` tool was called AND supporting skill files were read | `Skill` was called OR files were read, but not both | Neither — the skill doc may have been injected but the agent never read it |
| MCP tools preferred over shell | `mcp__*` count > 0 AND `Bash` count for known-CLI commands is low | Mixed — some MCP, some shell | All shell, no MCP (typically means the agent ignored the MCP config) |
| First deviation point | No tool errors AND no pipeline error events | Either a tool error OR a pipeline error, but not both | Both — the pipeline errored after a tool error |
| Scratch org / environment correct | Salesforce + `sf_scratch_create` succeeded + alias verified | Partial (scratch create called but activation/verification can't be confirmed) | Not applicable or no scratch org created |
| Review / QA verdict | APPROVED + PASS | PASS_WITH_NOTES, or one approved and one partial | FAIL or REJECTED |

**The two-signal-green rule:** a green check REQUIRES two independent pieces of evidence. Single-signal findings stay yellow by construction so a transient or noisy signal can't falsely clear the check. If the checklist says "all green," you can reasonably trust the run. If anything is yellow or red, open the session panels and drill in.

## Tool Usage panel — the highest-leverage signal

Always visible at the top of the session panels. Shows:

- Total tool calls / assistant turns (e.g. `40 tool calls across 23 assistant turns`)
- Per-tool counts, sorted by frequency, color-coded by category (Bash red, Read/Glob/Grep blue, mcp__* green)
- Error counts next to tools that had at least one failed call
- **Warning rows** for anomalies:
  - `⚠ MCP server "playwright" connected but never used` — tells you the server was available but the agent chose not to use it
  - `⚠ First tool error: Bash at line 47 — <error message>` — tells you where things started going wrong

**What to look for:**
- Zero `mcp__salesforce__*` calls on a Salesforce ticket → skill injection is broken or the agent ignored it
- >10 Bash calls with no MCP calls → the agent is shelling out instead of using MCP tools
- High `Read` count with no `Skill` calls → the agent explored the codebase but never invoked the expected skill
- `first_tool_error.tool = Bash, line = X` → investigate around line X in the session stream to see what command failed

## Opening the four collapsible panels

All four default to collapsed. Open them in order as needed:

- **Agent Instructions** — renders the effective `CLAUDE.md` the agent saw (merged client + harness instructions + injected skill supplements). Open this when the question is "why did the agent think X was allowed?" — the answer often jumps out from reading the prompt it actually got.
- **Reasoning Narrative** — renders `session.log`, the extracted assistant text blocks from the session stream. Open this when you want to follow the agent's thinking across the whole run.
- **Tool Calls Timeline** — renders the first 100 events from `session-stream.jsonl` with per-row truncation and show-full toggles. Open this when you need to see the specific tool calls and results that led to a particular outcome.
- **Raw Downloads** — links to full `session.log`, `session-stream.jsonl`, and `effective-CLAUDE.md` files. Open this when you need to grep / diff / attach to a bug report.

## The two investigation command paths

Both boxes are `<details>` disclosures above the session panels. Click to reveal the shell command to copy.

**Cheap path (Copy investigation command):**
- No auth, no audit log entry, no API key
- Good for "I want to poke at this trace quickly"
- One command: `curl` the bundle, extract, `cd`, launch `claude -p`
- The bundle is gzipped, redacted, and safe to share

**Audited path (Open in Claude for investigation):**
- Requires your API key (`X-API-Key` header)
- Writes a line to `<LOGS_DIR>/discuss-audit.jsonl` with timestamp + ticket_id + source IP + user agent
- Good for "I want a record of what I investigated when"
- Three-step command: POST to `/traces/<id>/discuss` → run the returned investigate command → feed transcript to `capture_discuss_output.py`

Both launch a local `claude -p` session with the post-mortem-analyst skill pre-loaded (`runtime/skills/post-mortem-analyst/SKILL.md`). The skill's system prompt requires the analyst to cite line numbers from `session-stream.jsonl` for every claim, push back on leading questions, and produce output in a specific three-section format.

## Feeding Claude's output back

When your `claude -p` session produces a proposed fix, save the transcript and run:

```bash
python scripts/capture_discuss_output.py --transcript /tmp/transcript.md
```

Options:
- `--apply-fix` — if the proposed fix is a unified diff, run `git apply --check` to verify it applies cleanly (does NOT actually apply — that's your decision)
- `--save-memory` — write the memory entry to a file for you to review and move into your memory directory

The script expects the transcript to contain exactly three markdown sections, in this order:

```
## Root cause

<one-line description>

## Proposed fix

<unified diff or skill-doc edit>

## Memory entry

<markdown for ~/.claude/projects/.../memory/>
```

Missing or misordered sections cause the parser to fail loudly. This is intentional — a half-formed transcript should not silently produce a wrong root cause.

## Forensic escape hatches (when the dashboard isn't enough)

- **Raw session-stream on disk:** `<client_repo.parent>/trace-archive/<ticket-id>/session-stream.jsonl` (for completed runs) or `<client_repo.parent>/worktrees/<branch>/.harness/logs/session-stream.jsonl` (for failed/escalated runs). NOT redacted — use only locally.
- **Trace store:** `<L1_LOGS_DIR>/<ticket-id>.jsonl`. IS redacted at consolidation time.
- **Bundle export:** `GET /traces/<id>/bundle` returns a redacted tarball of everything needed for offline investigation.
- **Individual artifacts:** `GET /traces/<id>/artifact/<session_log|session_stream|effective_claude_md>` serves single files.

## When something's wrong with the observability itself

If the dashboard panels or checklist are misbehaving (showing wrong data, crashing, failing to render):

1. Check L1 service logs: `tail -f /tmp/l1-service.log`
2. Run the test suite: `python -m pytest services/l1_preprocessing/tests/ -q`
3. If redaction patterns aren't catching a new credential shape, add the pattern to `services/l1_preprocessing/redaction.py::_LINE_PATTERNS` and run `curl -X POST http://localhost:8000/admin/re-redact -H 'X-API-Key: ...'` to retroactively redact existing traces.
4. Reset trace state: the trace JSONL files are at `<L1_LOGS_DIR>/`. Safe to delete individual files to clear bad state.

## Known limitations (Tier 1)

- **Skill invocation verification is yellow-only.** The tool index captures tool NAMES but not per-call arguments, so the diagnostic can tell you `Skill` was called but not WHICH skill was invoked. Green requires argument-level inspection which would double the LOC of commit 1.
- **Platform-marker regex is functionally unreachable.** The literal `PLATFORM: X` string the check looks for isn't written into structured trace entries anywhere in the current codebase. The platform check falls back to the `platform_profile` signal alone, so it stays yellow even on runs where the platform was correctly detected. Fixing this requires writing the marker from `harness-CLAUDE.md` — separate change.
- **Tool Calls Timeline pagination is first-100-only.** The "load more" link in the timeline panel is static text, not a working endpoint. For traces with >100 events, use the Raw Downloads panel to fetch the full stream.
- **Bundle size on large traces.** `_build_bundle` is in-memory. Runs with multi-MB session streams will eat RAM briefly during bundle generation. Fine for solo use; would need streaming for production.
- **Multi-tenancy / shared harness is out of scope.** Each developer runs their own L1 locally. The dashboard has no authn beyond the `X-API-Key` gate on write endpoints. Do not expose the dashboard beyond localhost.
