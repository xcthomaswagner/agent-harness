# Agentic Developer Harness — Architecture Plan

**Jira / Azure DevOps to Pull Request**
**Autonomous Multi-Agent Pipeline with Claude Code Agent Teams**

XCentium GenAI Practice | March 2026 | V2 — Confidential

**Revision History**

| Version | Date | Changes |
|---------|------|---------|
| V1 DRAFT | 2026-03-21 | Initial architecture plan |
| V2 | 2026-03-21 | Incorporated architecture review: failure mode analysis, Claude Max throughput model, merge coordinator redesign, ticket-splitting logic, platform profiles, observability, client onboarding configuration, concurrent ticket isolation, revised timeline |
| V2.1 | 2026-03-28 | Visual verification overhaul: replaced Playwright MCP with agent-browser CLI for Figma design compliance (pixel diffs, computed styles, responsive viewports). Playwright MCP retained for E2E functional test flows. L1 Figma frame PNGs now serve as pixel-diff baselines. |

---

## 1. Executive Summary

This document defines the architecture for an autonomous agentic developer harness that transforms Jira or Azure DevOps tickets into reviewed, tested, merge-ready Pull Requests with minimal human intervention. The system uses Claude Code Agent Teams as the execution engine, with role-separated AI teammates that plan, review, implement, test, and coordinate work in parallel.

The architecture is organized into three layers: a Pre-Processing Service that enriches tickets before any code is written, an Agent Team Execution layer where specialized AI teammates collaborate on implementation, and a PR Review & Feedback Loop that handles automated code review, CI failure recovery, and human review routing.

The system assumes a Claude Max subscription (flat-rate unlimited), which shifts the optimization target from cost management to throughput maximization and reasoning quality.

> **Key insight from industry research:** 59% of developers now run 3+ AI tools in parallel. The industry is converging on the "orchestrator" developer role (Osmani/O'Reilly, Anthropic 2026 Trends Report). This harness operationalizes that pattern for enterprise delivery teams.

---

## 2. Industry Context & Research Findings

This architecture is informed by extensive research into existing open-source implementations, commercial products, and practitioner experience reports from Q1 2026.

### 2.1 Existing Solutions Evaluated

| Solution | Stars / Status | Fit Assessment |
|----------|---------------|----------------|
| **Open SWE** (LangChain) | 7.4k stars | Closest OSS. No Jira, no role separation, no ticket enrichment. Uses Linear/GitHub Issues. |
| **Composio Agent Orchestrator** | MIT, 43k LOC TS | Strong execution layer (worktree isolation, CI feedback, dashboard). Missing pre-processing, plan review gates, ADO support. |
| **deepsense.ai AI Teammate** | Commercial | Full Jira-to-PR. FastAPI + aider + Claude. Not open source. Architecture documented in detail. |
| **GitHub Copilot for Jira** | Public Preview | Single-shot agent. No self-review, no feedback loop, no sub-ticket creation. Black box. |
| **OpenAI Codex Cookbook** | Reference impl. | Clean Jira-to-GitHub Action skeleton. Steps 1, 2, 6 only. No test generation or review. |
| **OpenHands** | 68k stars | Agent platform/SDK, not a pipeline. Model-agnostic. Would require building full orchestration on top. |
| **SWE-agent** (Princeton) | NeurIPS 2024 | Research-focused. GitHub issue to fix. Not a full SDLC pipeline. |

### 2.2 Key Industry Signals

- **Anthropic 2026 Trends Report:** Developers use AI in ~60% of work but can only fully delegate 0-20% of tasks. Multi-agent architectures with orchestrators coordinating specialists are the emerging pattern.
- **Addy Osmani (Google, O'Reilly):** Defines the shift from "Conductor" (single agent) to "Orchestrator" (multi-agent parallel). Internal experiments at tech companies already have agents creating PRs that other agent reviewers critique.
- **Simon Willison:** Advocates for sandboxed execution. Notes "parallel agent psychosis" as a real operational risk. Calls for cloud-hosted persistent agents.
- **Claude Code Revenue:** Run-rate revenue exceeded $2.5B, doubled since January 2026. Weekly active users also doubled in 6 weeks.
- **Agent Teams:** Shipped February 2026 (experimental). Enables direct inter-teammate messaging, solving the context transfer problem that made previous role-separated approaches impractical.
- **Context Window:** Claude Opus 4.6, Sonnet 4.6, and Claude Code all support 1M token context on Max/Team/Enterprise plans, substantially reducing context saturation concerns.

---

## 3. Architecture Overview

The system operates as a three-layer pipeline connected by events (webhooks in, API calls out). Each layer is independently deployable and serves a distinct function.

| Layer | Function | Trigger | Technology |
|-------|----------|---------|------------|
| **L1: Pre-Processing** | Ticket intake, enrichment, completeness gating, size assessment, conflict detection | Jira/ADO webhook on status change | FastAPI service + Claude API + Jira/ADO MCP |
| **L2: Agent Team Execution** | Planning, review, parallel implementation, testing, merge | Enriched ticket handoff from L1 | Claude Code Agent Teams + Skills + Figma/Playwright MCP |
| **L3: PR Review & Feedback** | AI PR review, CI auto-fix, human comment routing, sub-ticket creation | GitHub/ADO webhook on PR events | Claude Code headless + GitHub MCP + /pr-review skill |

### 3.1 Design Principles

- **Role separation with enforced constraints:** Each Agent Team teammate has specific skills loaded and specific tool restrictions. A review teammate cannot write code files. A QA teammate cannot modify production code. Constraints produce different cognitive behavior than prompting alone.
- **Parallelism at two levels:** Across tickets (N independent Agent Teams) and within tickets (N dev teammates per plan, based on the plan's dependency graph).
- **Skills for expertise, MCP for integration:** Skills are role-specific knowledge loaded just-in-time into each teammate's context. MCP servers are shared integration infrastructure (Jira, ADO, GitHub, Figma, Playwright).
- **Platform Profiles for portability:** Platform-specific knowledge (Sitecore, Salesforce, etc.) is injected into skills via a pluggable configuration layer, keeping the core architecture platform-agnostic while enabling platform-specific quality.
- **Event-driven, not polling:** Webhooks trigger each layer. No scheduled polling of Jira or GitHub. The system reacts to events within minutes.
- **Human-in-the-loop at the PR boundary:** The PR is the hard checkpoint. Everything before it is automated. The human reviews a PR that has already been planned, implemented, self-reviewed, and QA-validated.
- **Graduated autonomy:** Start with human review of every PR. As first-pass acceptance rates improve (measured over rolling 30-day windows), expand auto-merge to low-risk changes.
- **Fail fast with circuit breakers:** Every phase transition has a defined max retry count, escalation path, and partial success handling. Cascading failures are caught by circuit breakers, not discovered at the PR stage.

---

## 4. Layer 1: Pre-Processing Service

### 4.1 Trigger Mechanism

**Jira:** Automation rule fires webhook POST when ticket transitions to "Ready for AI" or receives the `ai-implement` label. Payload includes key, type, summary, description, acceptance criteria, attachments, linked issues.

**Azure DevOps:** Service Hook fires on work item state change. Same trigger pattern, different payload format. ADO MCP server (gap to build) normalizes the payload.

A thin adapter layer normalizes both into a common TicketPayload structure: source, id, type (story/task/bug), title, description, acceptance_criteria, attachments, linked_items, and a callback object for writing back to the source system.

### 4.2 Ticket Analyst Agent

The analyst runs as a single Claude Opus API call (not a Claude Code session) with the `/ticket-analyst` skill loaded. It performs five operations:

1. **Classifies the ticket type** and verifies metadata consistency (e.g., a ticket typed as "bug" but written as a feature request gets flagged).
2. **Evaluates completeness** against a rubric specific to the ticket type. Each type has its own rubric file (RUBRIC_STORY.md, RUBRIC_BUG.md, RUBRIC_TASK.md).
3. **Assesses ticket size and complexity.** Estimates the number of independent implementation units. Tickets exceeding the 10-teammate Agent Team limit (typically 5+ independent units requiring 4+ parallel devs) are flagged for decomposition (see Section 4.6).
4. **Detects conflicts with in-progress tickets.** Checks the plan's likely affected files against currently-in-progress tickets to identify overlapping scopes. Conflicting tickets are either sequenced (the new ticket waits) or flagged for human coordination.
5. **Produces one of three outputs:**
   - **(A) Enriched ticket** — generated acceptance criteria, test scenarios, and edge cases written back to the source system. Ticket promoted to Layer 2.
   - **(B) Information request** — targeted questions posted as a comment. Ticket set to "Needs Clarification." Processing halted until the human responds.
   - **(C) Decomposed tickets** — for oversized tickets, the analyst creates linked sub-tickets in Jira/ADO, each scoped to fit within a single Agent Team. Each sub-ticket enters the pipeline independently. A parent-level coordinator (see Section 5.7) reassembles the results.

### 4.3 Figma Link Detection & Frame Export

When the analyst detects a Figma link in the ticket, it calls the Figma REST API to extract the design context before evaluating completeness. The extraction produces:

1. **Structured design specification:** Components, layout patterns, color tokens, typography, interactive states, and responsive breakpoints — cached in the enriched ticket JSON.
2. **Rendered frame PNGs:** Up to 5 top-level frames exported at 2x scale via the Figma Image API. These are saved as attachments (e.g., `figma-Header.png`, `figma-Footer.png`) and copied into the agent worktree at `.harness/attachments/`. They serve as **pixel-diff baselines** for the QA skill's visual verification step using `agent-browser`.

The analyst cross-references the design against the ticket's text acceptance criteria, identifying gaps such as "the Figma shows 4 screens but the acceptance criteria only mention 2 of them."

### 4.4 Skill: /ticket-analyst

| File | Purpose |
|------|---------|
| SKILL.md | Analysis rubric, enrichment logic, decision gates, size assessment criteria |
| RUBRIC_STORY.md | Completeness criteria for user stories/use cases |
| RUBRIC_BUG.md | Completeness criteria for bugs (repro steps, expected vs. actual) |
| RUBRIC_TASK.md | Completeness criteria for tasks (scope, definition of done) |
| FIGMA_EXTRACTION.md | How to read Figma links, extract design specs, cross-reference with AC |
| SIZE_ASSESSMENT.md | Criteria for small/medium/large classification and decomposition triggers |
| CONFLICT_DETECTION.md | How to check for overlapping file scopes with in-progress tickets |
| TEMPLATES/acceptance_criteria.md | Template for generating acceptance criteria |
| TEMPLATES/test_scenarios.md | Template for generating test case outlines |
| TEMPLATES/info_request.md | Template for requesting missing information |
| TEMPLATES/design_spec.md | Template for structured design specification output |
| TEMPLATES/sub_ticket.md | Template for decomposed sub-ticket creation |

### 4.5 Handoff to Layer 2

Once the ticket is enriched and approved, the service triggers Layer 2. Three options are supported:

- **Direct spawn:** Service runs Claude Code in headless mode (`claude -p`) with the enriched ticket as initial context. Suitable for single-server deployments.
- **GitHub Action trigger:** Service calls GitHub's `workflow_dispatch` API. The Action checks out the repo, installs Claude Code, and runs the Agent Team in CI. Provides sandbox isolation.
- **Queue-based:** Service drops the enriched ticket onto a queue (Redis/SQS/database). Worker processes pick up tickets and spawn Agent Team sessions. Best for production with multiple concurrent tickets.

### 4.6 Ticket Decomposition for Oversized Work

When the analyst assesses a ticket as too large for a single Agent Team (5+ independent implementation units), it decomposes the ticket into linked sub-tickets. Each sub-ticket:

- Is scoped to fit within a single Agent Team (1-3 dev teammates)
- Has its own acceptance criteria and test scenarios
- References the parent ticket for traceability
- Includes explicit dependency ordering (sub-ticket B depends on sub-ticket A)

Sub-tickets enter the pipeline independently. A lightweight **Cross-Ticket Coordinator** (a scheduled Claude Code session, not an Agent Team) monitors the completion of all sub-tickets for a parent, then triggers the integration merge across their PRs. This coordinator operates outside the 10-teammate limit since it's a separate session.

> **Phasing note:** In Phases 1-3, oversized tickets are flagged by the L1 analyst for manual splitting by the PM. Automated decomposition and the Cross-Ticket Coordinator are implemented in Phase 4 once the core pipeline is proven.

---

## 5. Layer 2: Agent Team Execution

Each enriched ticket spawns a Claude Code Agent Team instance. The team lead receives the enriched ticket (including acceptance criteria, test scenarios, design spec if Figma-linked, and the original description) and orchestrates the full implementation pipeline.

### 5.1 Agent Teams Abstraction Layer

Because Agent Teams is experimental (February 2026), the system communicates between teammates via a **message abstraction** rather than directly coupling to the Agent Teams API. Each inter-teammate communication is a structured message object containing: sender role, recipient role, message type (plan, review, correction, diff, test results), and payload.

When Agent Teams is available, these messages route via direct teammate messaging. If Agent Teams becomes unavailable or the API changes, the same messages route via shared file artifacts in the worktree (`/.harness/messages/`). The team lead reads and dispatches file-based messages in a sequential loop. This degrades performance (no parallelism) but preserves correctness.

This abstraction is the primary insurance against the Agent Teams experimental risk.

### 5.2 Team Composition

| Teammate | Model | Tool Access | Skill | Role |
|----------|-------|-------------|-------|------|
| **Team Lead** | Opus | Full coordination | (orchestration) | Receives ticket, creates task list, spawns teammates, coordinates merge |
| **Planner** | Opus | Read-only + write plan artifacts | /plan-implementation | Decomposes ticket into implementation plan with dependency graph |
| **Plan Reviewer** | Opus | Read-only | /review-plan | Critiques plan for gaps, incorrect dependencies, missing edge cases |
| **Dev Teammate(s)** | Opus (complex) / Sonnet (straightforward) | Full (bash, edit, write) + Figma MCP | /implement + Platform Profile | Implements assigned units, writes tests, runs tests, commits |
| **Code Reviewer** | Opus | Read-only + run scripts | /code-review + Platform Profile | Reviews diffs for correctness, style, security. Cannot write code files. |
| **QA Teammate** | Sonnet | Read + test runners + Playwright MCP + agent-browser CLI | /qa-validation + Platform Profile | Validates against AC via unit, integration, E2E tests, and visual design compliance |
| **Merge Coordinator** | Sonnet | Git operations | (built-in) | Integrates branches in dependency order, resolves conflicts |

**Model selection rationale (Claude Max):** With flat-rate unlimited pricing, the optimization target is quality, not cost. Opus is used for all reasoning-heavy roles (analyst, planner, reviewer, complex devs, code review, PR review). Sonnet is used only where speed matters more than reasoning depth (QA test execution, merge coordination, CI fix agents). The quality improvement in planning and review phases reduces downstream failures, meaning fewer retry loops and faster end-to-end throughput.

### 5.3 Pipeline Flow Within the Agent Team

#### Phase 1: Planning

The Planner teammate reads the enriched ticket and produces a structured implementation plan containing: a task decomposition into atomic implementation units, each with description, affected files, test criteria, and dependencies; a test strategy specifying coverage type per unit; an architecture note on how the work fits the existing codebase; and a sizing estimate (small: 1 dev, medium: 2-3, large: 4+) that determines the fan-out.

**Failure mode:** If the planner cannot produce a coherent plan after 2 attempts, the ticket is escalated to human with the planner's analysis of why decomposition failed (typically: ambiguous requirements, contradictory acceptance criteria, or unfamiliar architectural territory).

#### Phase 2: Plan Review

The Plan Reviewer teammate receives the plan via direct message. Operating with read-only access, it evaluates for missing edge cases, incorrect dependency ordering, overly aggressive parallelization (two tasks touching the same file should not be parallel), and alignment with the original acceptance criteria. It sends corrections directly back to the Planner, who revises.

**Failure mode:** Maximum 2 review-correction cycles. If the plan reviewer rejects a third time, the ticket is escalated to human with the plan, the review findings, and a summary of what couldn't be resolved. The most common cause: the ticket's requirements are internally contradictory.

#### Phase 3: Parallel Implementation

Based on the approved plan's sizing, the team lead spawns N dev teammates, each assigned independent implementation units. Each dev teammate operates in its own git worktree on its own branch. For UI-related units on Figma-linked tickets, dev teammates query the Figma MCP for specific design nodes and use Code Connect mappings for component reuse where available.

Each dev teammate follows the `/implement` skill (augmented with the active Platform Profile): implement the code, write the tests specified in the plan, run the tests, and commit only when tests pass.

**Failure mode:** If tests fail, the dev gets 3 self-correction attempts. After 3 failures, the unit is marked as `BLOCKED` with the failure details. Other dev teammates continue their work independently — a single blocked unit does not halt the team. When the team lead assembles results, blocked units are either routed to human or retried with additional context from the successful units.

#### Phase 4: Code Review

The Code Review teammate receives diffs from each dev teammate via direct message. Operating with read-only access to code files (plus the ability to run linting and coverage scripts), it evaluates for correctness against the plan, style/convention compliance, test coverage completeness, and security anti-patterns. Issues are sent back to the dev teammate as structured change requests. The dev applies corrections and resubmits.

**Failure mode:** Maximum 2 correction cycles per unit. If unresolved after 2 cycles, the unit is flagged for human review with the code, the review findings, and the correction attempts. The remaining units proceed to QA independently.

#### Phase 5: QA Validation

The QA teammate runs a five-step validation process:

1. **Unit test validation:** Runs the full unit test suite, verifies coverage meets plan requirements, flags gaps.
2. **Integration/API test validation:** Starts services in the worktree, runs integration tests, verifies contract compliance.
3. **E2E live browser validation via Playwright MCP:** Starts the dev server, navigates the app via browser automation using the accessibility tree (34 MCP tools), walks through each acceptance criterion interactively, captures screenshots as evidence.
4. **Figma design compliance via `agent-browser`:** For Figma-linked tickets, the QA teammate uses `agent-browser` (CLI) — not Playwright MCP — for visual design verification:
   - **Pixel diff:** Compares rendered pages against Figma frame PNGs exported by L1 (`agent-browser diff screenshot --baseline .harness/attachments/figma-<Frame>.png`)
   - **Computed style checks:** Verifies color tokens and typography via `agent-browser get styles` against `figma_design_spec`
   - **Component presence:** Parses `agent-browser snapshot` (accessibility tree) for expected components
   - **Responsive testing:** Iterates breakpoints via `agent-browser set viewport` and captures screenshots at each
5. **Persistent test suite generation via Playwright Test Agents (Phase 3+):** Runs the Planner agent to produce Markdown test plans from acceptance criteria, the Generator to create `.spec.ts` files validated against the live DOM, and the Healer to auto-repair any selector drift. Generated test files are committed alongside implementation code.

The QA teammate outputs a pass/fail matrix mapping each acceptance criterion to test evidence (including screenshots for UI work). For Figma-linked tickets, the matrix includes a design compliance section with pixel diff results, style comparisons, and responsive screenshots.

**Tool separation:** Playwright MCP handles E2E functional test flows (navigate, interact, assert behavior). `agent-browser` handles visual design verification (pixel diffs, computed styles, responsive viewports). This separation exists because `agent-browser` provides pixel-level image diffing and computed CSS style inspection that Playwright MCP's accessibility tree cannot.

**Failure mode:** If QA finds implementation bugs, it routes the specific failure back to the dev teammate that owns the affected unit (identified via the plan's unit-to-dev mapping). If that dev session has ended, the team lead spawns a new focused dev session with the failure context and the original unit's code. Maximum 2 QA-dev round trips per unit before human escalation.

#### Phase 6: Merge Coordination (P1)

The Merge Coordinator handles integration of parallel branches. This is a critical phase where most multi-agent systems fail.

**Merge Strategy:**

1. **Ordering:** Branches merge in topological order based on the plan's dependency graph. Units that others depend on merge first.
2. **Merge method:** `git merge --no-commit` followed by full test suite run. If tests pass, commit the merge. If tests fail, the merge is aborted.
3. **Conflict detection:** Git-level conflicts (same line changed) are detected automatically. Semantic conflicts (e.g., two devs add the same import, or incompatible interface changes) are detected by the post-merge test run.
4. **Conflict resolution:** Route the conflict to the dev teammate with the most context on the conflicting files (determined by which dev's plan unit lists those files as primary). The dev resolves in a focused session.
5. **Fallback:** If conflict resolution fails after 2 attempts, squash all branches into one and spawn a single dev session to resolve all conflicts manually with full context of every branch.
6. **Final validation:** After all branches are merged, the full test suite runs one final time. Only on green does the coordinator open the draft PR.

**Failure mode:** If the final merged branch cannot achieve green tests after 2 full resolution cycles, the PR is opened as draft with a `needs-human-merge` label and a detailed conflict report in the PR description.

### 5.4 Cross-Ticket Coordination

For decomposed tickets (see Section 4.6), a Cross-Ticket Coordinator monitors completion of all sub-ticket PRs for a parent ticket. When all sub-PRs are merged, it:

1. Creates an integration branch
2. Merges all sub-ticket branches in dependency order
3. Runs the full test suite
4. Opens a final integration PR that references all sub-tickets
5. Updates the parent Jira/ADO ticket with the integration PR link

This coordinator runs as a separate Claude Code session (not an Agent Team), avoiding the 10-teammate limit.

### 5.5 Skills Map

| Skill | Layer / Role | Key Contents |
|-------|-------------|--------------|
| `/ticket-analyst` | L1 / Analyst | Rubrics per ticket type, Figma extraction guide, enrichment templates, size assessment, conflict detection |
| `/plan-implementation` | L2 / Planner | Plan schema (JSON/YAML), decomposition patterns, example plans (small/medium/large) |
| `/review-plan` | L2 / Plan Reviewer | Anti-pattern checklist, mandatory verification points, correction format |
| `/implement` | L2 / Devs | Coding standards, Figma integration section, test patterns + project-level CLAUDE.md + Platform Profile |
| `/code-review` | L2 / Code Reviewer | Security checks, style guide, review format template, coverage check scripts + Platform Profile |
| `/qa-validation` | L2 / QA | Unit/integration/E2E guides, Playwright live + generation docs, agent-browser visual verification, QA matrix template, helper scripts + Platform Profile |
| `/pr-review` | L3 / PR Reviewer | Architecture review checklist, security review, PR-level review template |

### 5.6 MCP Server Map

> **Design principle:** Use CLI as the default for all tools where the model has training-data
> fluency (git, gh, npm, sf). Reserve MCP for tools with no CLI equivalent or for progressive
> discovery of unfamiliar APIs. Custom skills serve as the composability layer.

| Tool | Access Method | Rationale |
|------|--------------|-----------|
| **git** | CLI (training-data fluency) | Branch, merge, commit, diff, blame — native to all models |
| **gh** | CLI (training-data fluency) | PR creation, reviews, comments — well-documented, fast |
| **npm/npx** | CLI | Test runners, dev servers, package management |
| **sf** (Salesforce) | CLI + SF MCP Server (planned) | CLI for basic ops; MCP server adds structured tool access with production guard |
| **Playwright** | **MCP** (`@playwright/mcp@latest`) | E2E functional test flows — navigate, click, type, assert via accessibility tree |
| **agent-browser** | **CLI** (`agent-browser`) | Visual design verification — pixel diffs, computed style inspection, responsive viewports, annotated screenshots. Used by QA skill for Figma design compliance. |
| **Figma** | L1 REST API (extractor) | MCP blocked on OAuth for headless sessions (see `docs/future-enhancements.md`). REST API exports frames as PNGs for pixel-diff baselines. |
| **Jira** | L1 REST API (adapter) | httpx wrapper in Python service — MCP unnecessary for server-side calls |
| **ADO** | L1 REST API (adapter) | Same pattern as Jira |

**Not using MCP for:** GitHub (gh CLI is better), Jira (REST adapter in L1), ADO (REST adapter in L1), agent-browser (CLI-native design, no MCP needed).
These were listed as MCP servers in the original architecture plan but CLI/REST proved simpler and more reliable.

---

## 6. Layer 3: PR Review & Feedback Loop

This layer is event-driven, reacting to GitHub (or ADO Repos) webhook events on the PR after the Agent Team has completed its work.

### 6.1 Event: PR Opened (Draft)

The AI PR reviewer runs as a separate Claude Code headless session (Opus) with the `/pr-review` skill loaded. It receives the full diff and the enriched ticket (including acceptance criteria). It evaluates cross-cutting architectural concerns, naming consistency across files, API contract alignment, test coverage comprehensiveness, and security/performance/maintainability issues visible at the PR level. The reviewer posts findings as a GitHub PR review with inline comments, appearing natively in GitHub's PR interface.

### 6.2 Event: CI Check Failure

When CI fails, the service pulls failure logs, classifies the failure type (test/build/lint/security scan), and spawns a targeted Claude Code session (Sonnet, for speed) for the fix. The fix agent commits to the same branch, the PR updates, and CI re-runs. Maximum 3 fix attempts before escalating to human.

### 6.3 Event: Human Review Comment

- **Approvals:** Service marks the Jira/ADO ticket as "Review Approved." Based on merge policy, either auto-merges or notifies.
- **Change requests:** Comments are parsed into actionable items. Small fixes spawn targeted Claude Code sessions on the same branch. Substantial changes spawn a new Agent Team session. Each blocking issue is auto-filed as a Jira/ADO sub-task linked to the parent ticket.
- **Questions:** A lightweight Claude session reads the relevant code and plan artifacts, then posts a reply on the PR thread explaining the architectural decision.

### 6.4 Event: Approved + Green CI

- **Conservative mode:** Notifies the team lead (human) to merge manually.
- **Semi-autonomous mode:** Auto-merges and updates the Jira/ADO ticket to "Done."
- **Full autonomous mode:** Auto-merges, closes the ticket, and promotes dependent tickets to "Ready for AI" to trigger the pipeline again.

---

## 7. Platform Profiles

Platform Profiles are a pluggable configuration layer that injects platform-specific knowledge into skills without changing the core architecture. Each profile is a directory of supplementary skill content that gets loaded alongside the base skills.

### 7.1 Profile Structure

```
.claude/platform-profiles/
├── sitecore/
│   ├── PROFILE.md              # Profile metadata and activation rules
│   ├── IMPLEMENT_SUPPLEMENT.md  # Sitecore component patterns (JSS/Next.js, .NET + React coexistence)
│   ├── CODE_REVIEW_SUPPLEMENT.md # Sitecore-specific anti-patterns, serialization rules
│   ├── QA_SUPPLEMENT.md         # Testing against Experience Editor/Pages, content resolver patterns
│   └── CONVENTIONS.md           # Sitecore code conventions, naming patterns
├── salesforce/
│   ├── PROFILE.md
│   ├── IMPLEMENT_SUPPLEMENT.md  # Apex patterns, LWC conventions, governor limits
│   ├── CODE_REVIEW_SUPPLEMENT.md # Security review rules, SOQL injection, sharing model
│   ├── QA_SUPPLEMENT.md         # Salesforce test patterns, @IsTest conventions
│   └── CONVENTIONS.md
└── [future profiles]/
```

### 7.2 Profile Activation

The L1 Pre-Processing Service detects the target platform from:
- Explicit ticket metadata (project labels, custom fields)
- Repository detection (presence of `sitecore.json`, `sfdx-project.json`, etc.)
- Client Profile configuration (see Section 10)

The active profile's supplement files are injected into the relevant skills for all Agent Team teammates. For example, a Sitecore ticket causes the `/implement` skill to load both its base `SKILL.md` and the `sitecore/IMPLEMENT_SUPPLEMENT.md`.

---

## 8. Observability & Debugging

### 8.1 Structured Logging

Every teammate in every Agent Team session produces structured log entries containing:

- **Phase timestamps:** When each phase started, ended, and how long it took
- **Token usage:** Input/output token counts per turn, context window utilization percentage
- **Decision summaries:** Key decisions made (e.g., "chose 3 dev teammates because plan identified 3 independent units," "routed conflict to Dev B because they own auth.ts")
- **Tool call traces:** Every MCP tool invocation with inputs and outputs
- **Message traces:** Every inter-teammate message (via Agent Teams or file-based fallback)

Logs are written to a structured format (JSON Lines) in the worktree at `/.harness/logs/` and optionally streamed to LangSmith for visualization and querying.

### 8.2 Per-Ticket Dashboard Metrics

The Composio dashboard (or custom wrapper) displays per-ticket:

- Current phase and progress through the pipeline
- Per-teammate status (active, waiting, completed, failed)
- Time-in-phase breakdown (which phase is the bottleneck?)
- Retry/escalation count per phase
- Token consumption per teammate
- Final outcome (PR created, escalated, partially completed)

### 8.3 Aggregate Analytics

Across all tickets processed:

- Average time-to-PR by ticket type and size
- Phase-level bottleneck identification (e.g., "plan review takes 40% of total time")
- Failure distribution by phase (where do tickets most commonly escalate?)
- Model utilization (Opus vs. Sonnet breakdown by role)
- First-pass acceptance rate trend over time (for graduated autonomy decisions)

---

## 9. Failure Modes & Recovery

Every phase transition has defined failure handling. The system fails fast and escalates explicitly rather than retrying silently.

### 9.1 Phase Failure Summary

| Phase | Max Retries | On Failure | Partial Success Handling |
|-------|-------------|------------|------------------------|
| L1: Ticket Analysis | 1 re-evaluation after info request | Escalate to human with analysis notes | N/A — gate is binary |
| Planning | 2 plan attempts | Escalate with planner's analysis of why decomposition failed | N/A — gate is binary |
| Plan Review | 2 review-correction cycles | Escalate with plan + review findings + unresolved issues | N/A — gate is binary |
| Dev Implementation | 3 self-corrections per unit | Mark unit as BLOCKED; other units continue | Successful units proceed to review; blocked units escalated separately |
| Code Review | 2 correction cycles per unit | Flag unit for human review; other units proceed | Reviewed units proceed to QA; flagged units included in PR with `needs-review` label |
| QA Validation | 2 QA-dev round trips per failing criterion | Include failure details in PR description | Passing criteria documented; failing criteria flagged |
| Merge Coordination | 2 conflict resolution attempts | Squash fallback; if that fails, open PR with `needs-human-merge` label | N/A |
| L3: CI Fix | 3 fix attempts | Escalate to human | N/A |

### 9.2 Circuit Breaker: Cascading Failure Detection

If the QA teammate finds that >50% of acceptance criteria fail, this indicates a systemic issue (typically: poor L1 enrichment leading to a bad plan leading to incorrect implementation). Rather than routing individual failures back to devs, the circuit breaker triggers:

1. The team lead halts all in-progress work
2. A diagnostic summary is generated: what the ticket asked for, what the plan specified, what was implemented, and where the misalignment occurred
3. The ticket is escalated to human with the diagnostic, tagged as `pipeline-misalignment`
4. The enriched ticket and plan artifacts are preserved for post-mortem analysis

### 9.3 Partial Success Handling

When some dev units succeed and others fail, the system does not discard successful work. Instead:

1. Successful, reviewed, QA-passed units are merged to the feature branch
2. Failed units are documented in the PR description with their failure details
3. The PR is opened as draft with clear labels: `partial-implementation` and `N-of-M-units-complete`
4. Failed units are auto-filed as Jira/ADO sub-tasks linked to the parent ticket
5. These sub-tasks re-enter the pipeline as independent tickets

---

## 10. Client Onboarding Configuration

Every client deployment requires a Client Profile that maps their specific tooling and workflows to the harness.

> **Phasing note:** In Phases 1-3, client-specific configuration is hardcoded for the first client. The Client Profile configuration system is extracted as a formal YAML schema in Phase 4 when onboarding client #2. The architecture below defines the target state.

### 10.1 Client Profile Structure

```yaml
# client-profile.yaml
client: "Acme Corp"
platform_profile: "sitecore"  # or "salesforce", "custom"

ticket_source:
  type: "jira"  # or "ado"
  instance: "https://acme.atlassian.net"
  project_key: "ACME"
  ready_status: "Ready for AI"         # Status that triggers the pipeline
  clarification_status: "Needs Info"    # Status set when analyst needs more info
  done_status: "Done"                   # Status set on merge
  ai_label: "ai-implement"             # Alternative trigger via label
  custom_fields:
    acceptance_criteria: "customfield_10429"
    story_points: "customfield_10040"

source_control:
  type: "github"  # or "azure-repos"
  org: "acme-corp"
  default_branch: "main"
  branching_strategy: "trunk-based"  # or "gitflow", "github-flow"
  branch_prefix: "ai/"              # AI-created branches prefixed for visibility
  pr_reviewers: ["@acme-corp/dev-leads"]
  require_approval_count: 1

ci_pipeline:
  type: "github-actions"  # or "azure-pipelines", "jenkins"
  test_command: "npm test"
  lint_command: "npm run lint"
  build_command: "npm run build"
  e2e_command: "npx playwright test"

test_framework:
  unit: "jest"              # or "pytest", "nunit", "xunit"
  integration: "supertest"  # or "pytest", "custom"
  e2e: "playwright"         # or "cypress"

credentials:
  vault_path: "secret/clients/acme"  # or env file reference
```

### 10.2 Profile Effects

The Client Profile configures:

- **L1 triggers:** Which Jira/ADO status changes and labels activate the pipeline
- **L1 adapter:** How to read and write back to the client's specific Jira/ADO field configuration
- **L2 git behavior:** Branch naming, commit message format, PR template, reviewer assignment
- **L2 test execution:** Which test commands to run, which frameworks to expect
- **L3 feedback loop:** How to map CI failure types to the client's specific pipeline
- **Platform Profile activation:** Which platform-specific skill supplements to load

---

## 11. Execution Platform & Infrastructure

### 11.1 Composio Agent Orchestrator as Execution Layer

The Composio Agent Orchestrator (MIT-licensed, 43k LOC TypeScript, 17 plugins across 8 architecture slots) handles the operational infrastructure: parallel agent session management, git worktree isolation per agent, CI failure feedback routing, PR event handling, and a web dashboard for monitoring. It is agent-agnostic (Claude Code, Codex, Aider) and runtime-agnostic (tmux, Docker).

The orchestrator is fully rebrandable. The web dashboard is a React SPA at `localhost:3000` that can be forked and customized with XCentium branding. The CLI (`ao`) can be renamed. There is no phone-home to Composio's servers.

### 11.2 Custom Wrapper Services

Two custom services sit outside Composio:

- **L1 Pre-Processing Service:** Webhook receiver + ticket normalization + Claude API call for analyst. Lightweight FastAPI or Express app. Writes back to Jira/ADO, triggers Layer 2.
- **L3 PR Review Service:** GitHub/ADO webhook receiver + event classifier + Claude Code headless spawner for reviews and fixes. Routes feedback to agent sessions or creates new ones.

### 11.3 Throughput & Model Strategy (Claude Max)

With Claude Max (flat-rate unlimited), the optimization target shifts from cost to throughput and quality.

| Role | Model | Rationale |
|------|-------|-----------|
| Ticket Analyst | Opus | Deep reasoning for completeness evaluation and Figma cross-referencing |
| Planner | Opus | Architectural decomposition is the highest-leverage reasoning task |
| Plan Reviewer | Opus | Critical evaluation requires matching the planner's reasoning depth |
| Dev Teammates | Opus (complex units) / Sonnet (straightforward) | No cost penalty for Opus; use it where reasoning helps |
| Code Reviewer | Opus | Better pattern recognition for security and architectural issues |
| QA Teammate | Sonnet | Test execution is more mechanical; speed matters more |
| Merge Coordinator | Sonnet | Git operations are procedural |
| PR Reviewer (L3) | Opus | Whole-PR architectural review benefits from strongest reasoning |
| CI Fix Agent (L3) | Sonnet | Targeted fixes from failure logs; quick turnaround matters |

**Throughput analysis (to be validated empirically):**

- **Key unknown:** How many concurrent Opus/Sonnet sessions can a single Max subscription sustain? Rate limits for headless/API usage on Max need empirical testing.
- **Estimated bottleneck:** Not model cost, but CI pipeline speed (test execution) and Figma MCP rate limits (for design-heavy tickets).
- **Target:** Process 3-5 tickets/hour on a single Max subscription. Validate in Phase 1.

### 11.4 Scaling Constraints

- **Agent Teams limit:** Maximum 10 simultaneous teammates per team. A medium ticket (lead + planner + plan reviewer + 3 devs + code reviewer + QA + merge coordinator) = 9 teammates. Oversized tickets are decomposed in L1 (Section 4.6).
- **Figma MCP rate limits:** Dev seat required. Per-minute limits apply. Mitigation: extract design context once in L1, pass cached artifact to L2.
- **Playwright MCP in CI:** Headless mode (`--headless --no-sandbox`) for GitHub Actions. Browser binaries must be installed in the CI environment. Playwright is used for E2E test flows only.
- **agent-browser:** CLI tool for visual design verification (pixel diffs, style inspection, responsive testing). Must be installed on the host: `npx @anthropic-ai/agent-browser install`. Used by the QA skill for Figma design compliance checks.
- **1M token context window:** Available on Max/Team/Enterprise plans. Substantially reduces context saturation concerns for role-separated Agent Teams. Each teammate has ample room for skills, MCP schemas, plan artifacts, code diffs, and conversation history.

### 11.5 Credential & Secrets Management

For multi-client enterprise deployment:

- **Development/single-client:** Encrypted `.env` files per client profile, stored outside the repository.
- **Production/multi-client:** HashiCorp Vault or AWS Secrets Manager. Each client profile references a vault path. Credentials are injected at session start and never written to disk in worktrees.
- **Isolation:** Each Agent Team session receives only the credentials for its target client. No cross-client credential leakage is possible because each session is scoped to a single client profile.
- **Rotation:** Jira/ADO/GitHub tokens have defined rotation schedules. The secrets manager handles rotation; agent sessions always fetch fresh credentials at start.

---

## 12. Claude Code Developer Setup & File Structure

This section describes the physical file layout on a developer's machine and how the harness project coexists with Claude Code's multi-level configuration system without conflicts.

### 12.1 The Context Separation Problem

Claude Code loads configuration from multiple levels: global (`~/.claude/`), project-level (the repo's `CLAUDE.md` and `.claude/` directory), and user-level skills and agents. The harness operates across three distinct contexts that must stay separated:

- **Context A — Harness Development:** When you are building and maintaining the harness itself (webhook services, skills, agent definitions). Claude Code should see harness architecture context.
- **Context B — Client Repo Execution:** When the Agent Team operates on a client's codebase. Claude Code should see the client's conventions and the harness runtime skills, not the harness service code.
- **Context C — Runtime Configuration:** Skills, agent definitions, and platform profiles authored in the harness but deployed into client repo worktrees at execution time.

If these contexts mix (e.g., harness CLAUDE.md loaded while an agent is implementing client code), the agent receives contradictory instructions and produces confused output.

### 12.2 Physical Directory Layout

```
# ─── Your Global Claude Code Config (unchanged) ───
~/.claude/
├── CLAUDE.md                       # Your personal preferences (tone, analysis depth, etc.)
│                                   # Loaded in ALL sessions — keep this generic
├── settings.json                   # Global MCP servers (GitHub, Playwright, Figma)
├── skills/                         # Personal skills — keep clean, avoid names that
│                                   # collide with harness skills
└── agents/                         # Personal subagent definitions (if any)


# ─── The Harness Project (Context A) ───
~/harness/
├── CLAUDE.md                       # Describes the harness architecture for YOU
│                                   # "This is a multi-agent orchestration system.
│                                   #  The codebase contains webhook services in Python/TS,
│                                   #  skill definitions in markdown, and deployment scripts."
├── .claude/
│   ├── settings.json               # MCP servers needed for harness development
│   └── skills/                     # Skills for developing the harness itself
│       └── harness-dev/            # e.g., "how to write a good skill SKILL.md"
├── services/
│   ├── l1-preprocessing/           # FastAPI webhook service (Python)
│   │   ├── main.py
│   │   ├── adapters/
│   │   │   ├── jira_adapter.py
│   │   │   └── ado_adapter.py
│   │   ├── analyst.py              # Claude API call with /ticket-analyst skill
│   │   └── requirements.txt
│   └── l3-pr-review/               # PR review webhook service
│       ├── main.py
│       ├── event_classifier.py
│       └── spawner.py              # Launches Claude Code headless for fixes
├── scripts/
│   ├── spawn-team.sh               # Launches Agent Team in a client repo worktree
│   ├── inject-runtime.sh           # Copies runtime config into worktree
│   └── cleanup-worktree.sh         # Removes runtime files after session ends
│
│   # ─── Runtime Config (Context C) — authored here, deployed to client repos ───
├── runtime/
│   ├── skills/                     # The 7 harness skills
│   │   ├── ticket-analyst/
│   │   │   ├── SKILL.md
│   │   │   ├── RUBRIC_STORY.md
│   │   │   ├── RUBRIC_BUG.md
│   │   │   ├── RUBRIC_TASK.md
│   │   │   ├── FIGMA_EXTRACTION.md
│   │   │   ├── SIZE_ASSESSMENT.md
│   │   │   ├── CONFLICT_DETECTION.md
│   │   │   └── TEMPLATES/
│   │   ├── plan-implementation/
│   │   │   ├── SKILL.md
│   │   │   ├── PLAN_SCHEMA.md
│   │   │   ├── DECOMPOSITION.md
│   │   │   └── EXAMPLES/
│   │   ├── review-plan/
│   │   │   ├── SKILL.md
│   │   │   ├── ANTIPATTERNS.md
│   │   │   ├── CHECKLIST.md
│   │   │   └── CORRECTION_FORMAT.md
│   │   ├── implement/
│   │   │   ├── SKILL.md
│   │   │   ├── CODING_STANDARDS.md
│   │   │   ├── FIGMA_INTEGRATION.md
│   │   │   └── TEST_PATTERNS.md
│   │   ├── code-review/
│   │   │   ├── SKILL.md
│   │   │   ├── SECURITY_CHECKS.md
│   │   │   ├── STYLE_GUIDE.md
│   │   │   ├── REVIEW_FORMAT.md
│   │   │   └── scripts/
│   │   │       └── check_coverage.sh
│   │   ├── qa-validation/
│   │   │   ├── SKILL.md
│   │   │   ├── UNIT_TEST_VALIDATION.md
│   │   │   ├── INTEGRATION_TEST_GUIDE.md
│   │   │   ├── E2E_PLAYWRIGHT_LIVE.md
│   │   │   ├── E2E_PLAYWRIGHT_GENERATION.md
│   │   │   ├── QA_MATRIX_TEMPLATE.md
│   │   │   └── scripts/
│   │   │       ├── run_tests.sh
│   │   │       └── start_dev_server.sh
│   │   └── pr-review/
│   │       ├── SKILL.md
│   │       ├── ARCHITECTURE_REVIEW.md
│   │       ├── SECURITY_REVIEW.md
│   │       └── REVIEW_TEMPLATE.md
│   ├── agents/                     # Agent Team teammate definitions
│   │   ├── planner.md
│   │   ├── plan-reviewer.md
│   │   ├── developer.md
│   │   ├── code-reviewer.md
│   │   ├── qa.md
│   │   └── merge-coordinator.md
│   ├── platform-profiles/
│   │   ├── sitecore/
│   │   │   ├── PROFILE.md
│   │   │   ├── IMPLEMENT_SUPPLEMENT.md
│   │   │   ├── CODE_REVIEW_SUPPLEMENT.md
│   │   │   ├── QA_SUPPLEMENT.md
│   │   │   └── CONVENTIONS.md
│   │   └── salesforce/
│   │       └── [same structure]
│   ├── harness-CLAUDE.md           # Pipeline instructions injected into client repos
│   │                               # (phase ordering, message format, failure handling,
│   │                               #  teammate roles — NOT client coding conventions)
│   └── harness-mcp.json            # MCP config template for Agent Team sessions
│                                   # (Playwright, Figma, client-specific Jira/GitHub)


# ─── Client Repo (Context B) — where agents actually work ───
~/client-repos/acme-corp/
├── CLAUDE.md                       # Client's own conventions (maintained by humans)
│                                   # "This is a Sitecore XM Cloud project using Next.js..."
├── .claude/
│   ├── settings.json               # Client's own MCP servers (if any)
│   └── skills/                     # Client's own skills (if any)
├── src/
├── tests/
└── package.json
```

### 12.3 Runtime Injection: What Happens When an Agent Team Starts

When L1 finishes enriching a ticket and triggers L2, the spawn script (`scripts/spawn-team.sh`) executes these steps:

**Step 1: Create worktree.** Composio (or the spawn script directly) creates a fresh git worktree of the client repo for this ticket. The worktree is the agent's isolated workspace.

```bash
git worktree add ../worktrees/TICKET-123 main
cd ../worktrees/TICKET-123
```

**Step 2: Inject runtime skills.** Copy harness skills into the worktree's `.claude/skills/` directory. These sit alongside any client-defined skills.

```bash
cp -r ~/harness/runtime/skills/* .claude/skills/
```

**Step 3: Inject teammate definitions.** Copy agent definitions into the worktree's `.claude/agents/` directory.

```bash
cp -r ~/harness/runtime/agents/* .claude/agents/
```

**Step 4: Inject platform profile.** Based on the detected platform (from L1 analysis or client config), append the platform supplement to the relevant skills.

```bash
# Example for Sitecore
cat ~/harness/runtime/platform-profiles/sitecore/IMPLEMENT_SUPPLEMENT.md \
    >> .claude/skills/implement/SKILL.md
cat ~/harness/runtime/platform-profiles/sitecore/CODE_REVIEW_SUPPLEMENT.md \
    >> .claude/skills/code-review/SKILL.md
cat ~/harness/runtime/platform-profiles/sitecore/QA_SUPPLEMENT.md \
    >> .claude/skills/qa-validation/SKILL.md
```

**Step 5: Merge CLAUDE.md files.** The client's CLAUDE.md contains their coding conventions. The harness's `harness-CLAUDE.md` contains the pipeline instructions (team roles, phase ordering, message format, failure handling rules). Both are needed. The spawn script concatenates them with a clear separator.

```bash
# Preserve client's CLAUDE.md, append harness instructions
echo "" >> CLAUDE.md
echo "---" >> CLAUDE.md
echo "# Agentic Harness Pipeline Instructions" >> CLAUDE.md
echo "# (Injected by harness — do not edit manually)" >> CLAUDE.md
echo "" >> CLAUDE.md
cat ~/harness/runtime/harness-CLAUDE.md >> CLAUDE.md
```

The merged CLAUDE.md reads top-to-bottom: client conventions first (so they take priority for coding style), then harness pipeline instructions (so the agent knows the team workflow).

**Step 6: Write MCP configuration.** Generate `.mcp.json` for this session with client-specific endpoints (their Jira instance, their GitHub org) plus standard harness MCPs (Playwright, Figma).

```bash
# Template from harness, with client-specific values substituted
envsubst < ~/harness/runtime/harness-mcp.json > .mcp.json
```

**Step 7: Start the Agent Team session.**

```bash
claude -p "You are the team lead. Here is the enriched ticket: [ticket JSON].
Create an agent team and execute the full pipeline per the harness
instructions in CLAUDE.md."
```

### 12.4 What the Agent Team Sees

When the Agent Team session starts in the worktree, each teammate's context window contains:

1. **Global `~/.claude/CLAUDE.md`** — your personal preferences (loaded automatically)
2. **Project `CLAUDE.md`** — the merged file: client conventions + harness pipeline instructions
3. **Skills in `.claude/skills/`** — both client skills (if any) and harness skills (injected), loaded on-demand when the teammate's task matches a skill description
4. **Agent definitions in `.claude/agents/`** — the teammate role definitions (planner.md, developer.md, etc.)
5. **MCP servers from `.mcp.json`** — Jira/ADO, GitHub, Figma, Playwright
6. **The client codebase** — the actual `src/`, `tests/`, etc.

The agent has no awareness of the harness service code, the spawn scripts, or the runtime directory. It sees a client repo with skills and agent definitions that tell it how to operate as a team.

### 12.5 Cleanup After Session

When the Agent Team session ends (PR submitted or escalated), the spawn script cleans up:

```bash
# The worktree contains:
# - Client code changes (committed to branch, pushed, PR opened)
# - Injected harness files (.claude/skills/*, .claude/agents/*, merged CLAUDE.md)
# - Session artifacts (/.harness/logs/*, /.harness/messages/*)

# Option A: Delete the worktree entirely (simplest)
git worktree remove ../worktrees/TICKET-123 --force

# Option B: Preserve for debugging, clean up after 48 hours
# (useful during Phase 1-2 when you're iterating on skills)
```

The injected harness files never get committed to the client repo. They exist only in the worktree for the duration of the session. The PR contains only the agent's code changes, test files, and generated Playwright specs.

### 12.6 Avoiding Naming Collisions

Three collision risks and their mitigations:

**Personal skills vs. harness skills:** If your personal `~/.claude/skills/` contains a skill named `implement`, it collides with the harness `/implement` skill. Mitigation: keep personal skills named distinctively (e.g., `/my-writing-style`), or keep the personal skills directory clean and use project-level skills exclusively.

**Client skills vs. harness skills:** If a client repo already has a `.claude/skills/implement/` directory, the harness injection would overwrite it. Mitigation: the inject script checks for existing client skills with the same name and either namespaces the harness skill (e.g., `harness-implement`) or merges the content with the client skill taking priority for coding conventions.

**CLAUDE.md conflicts:** The merge approach (client first, harness appended) means client conventions take priority. If the client's CLAUDE.md says "use tabs" and the harness says "use spaces," the client wins because it appears first and Claude Code gives priority to earlier instructions. This is the correct behavior — the harness defines process, the client defines conventions.

### 12.7 Developing and Testing Skills Locally

When you're iterating on a skill (the most common development activity), the workflow is:

```bash
# 1. Edit the skill in the harness project
cd ~/harness
# Edit runtime/skills/implement/SKILL.md

# 2. Test it against a client repo without the full pipeline
cd ~/client-repos/acme-corp
cp -r ~/harness/runtime/skills/implement .claude/skills/
claude  # Start interactive session
# > "Use the /implement skill to add a button component to src/components/"
# Observe how the skill performs, iterate

# 3. Clean up the test injection
rm -rf .claude/skills/implement  # Remove harness skill from client repo
```

This lets you develop and test skills in isolation without running the full L1→L2→L3 pipeline. The harness project is where you author; the client repo is where you test. Git ensures the test injections never get committed.

### 12.8 Global MCP Configuration Recommendations

MCP servers that are useful across all projects can be configured globally to avoid per-session setup:

```jsonc
// runtime/harness-mcp.json — Injected into worktree as .mcp.json
// Minimal: only MCP servers with no CLI equivalent
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--headless"]
    }
    // Figma MCP: blocked on OAuth for headless sessions (see future-enhancements.md)
    // GitHub: using gh CLI instead (training-data fluency)
    // Jira/ADO: using REST adapters in L1 service
    // Salesforce: planned as composable MCP per platform profile
  }
}
```

Playwright and Figma are global because they work the same regardless of client. Jira and GitHub are per-project because each client has different instances, tokens, and permissions.

---

## 13. Gaps to Build

| Gap | Effort | Priority |
|-----|--------|----------|
| ADO MCP Server | 1-2 weeks | P1 (required for ADO clients) |
| L1 Pre-Processing Service (webhook + adapter + analyst + conflict detection) | 1.5 weeks | P1 |
| L3 PR Review Service (webhook + event router + spawner) | 1 week | P1 |
| 7 custom skills (ticket-analyst through pr-review) | 2-3 weeks | P1 |
| Agent Teams abstraction layer (message objects + file-based fallback) | 1 week | P1 |
| Merge Coordinator design and implementation | 1.5 weeks | P1 (promoted from P2) |
| Composio Jira tracker plugin (replacing Linear/GitHub) | 3-5 days | P1 |
| Sitecore Platform Profile (first platform profile) | 1-2 weeks | P1 |
| Observability layer (structured logging + LangSmith integration) | 1 week | P2 |
| Composio dashboard rebrand to XCentium | 3-5 days | P2 |
| Graduated autonomy metrics dashboard | 1-2 weeks | P3 |
| Client Profile configuration system | 1 week | P3 (deferred from P2 — hardcode first client, extract config on client #2) |
| Cross-Ticket Coordinator for decomposed tickets | 1 week | P3 (deferred from P2 — manual ticket splitting until decomposition volume justifies automation) |
| Visual regression testing integration (Percy/Chromatic) | 1-2 weeks | P3 (partially addressed by agent-browser pixel diff — Percy/Chromatic would add CI-managed baselines) |
| Concurrent ticket conflict detection in L1 | 3-5 days | P3 |

---

## 14. Implementation Roadmap

**Timeline assumption:** 16-20 weeks realistic. 12-week ideal-conditions timeline from V1 is revised based on architecture review. Estimates assume dedicated focus with buffer for integration surprises and Agent Teams API instability.

### Phase 1: Walking Skeleton (Weeks 1-4)

Prove the core loop works end-to-end on a single ticket with a single dev agent. Deliberately minimal — no planner, no reviewer, no QA teammates.

- Set up Composio Agent Orchestrator with Claude Code as the agent engine
- Build the L1 Pre-Processing Service with Jira webhook and basic `/ticket-analyst` skill (enrichment only, no size assessment or conflict detection yet)
- Build the Agent Teams abstraction layer with message objects
- Configure a minimal Agent Team: lead + 1 dev (no planner, no reviewer, no QA — just prove the loop)
- Verify: Jira ticket in → enriched ticket → code implementation → draft PR out
- Measure: Max subscription throughput limits (concurrent sessions, rate limits)
- **Success metric:** First end-to-end ticket processed. Empirical throughput data collected.

### Phase 2: Role Separation & Quality Gates (Weeks 5-8)

Add the full teammate roster and the review/QA layer.

- Add Planner, Plan Reviewer, and Code Reviewer teammates with read-only tool restrictions
- Implement parallel dev teammates (2-3 per ticket) with worktree isolation
- Build the Merge Coordinator with the full merge strategy (Section 5.3 Phase 6)
- Build the L3 PR Review Service with GitHub webhook handling
- Add Playwright MCP for E2E QA validation + agent-browser for visual design verification
- Implement failure mode handling for all phase transitions (Section 9)
- Add structured logging (observability layer)
- Build first Platform Profile (Sitecore)
- **Success metric:** PRs that pass human review on first submission >60%. All failure modes handled with explicit escalation.

### Phase 3: Figma & Advanced QA (Weeks 9-13)

Extend the pipeline for design-driven tickets and comprehensive testing.

- Integrate Figma MCP into L1 (design extraction) and L2 (dev implementation)
- Add Playwright Test Agents for persistent test suite generation
- Build ADO MCP server and ADO tracker plugin for Composio
- Implement ticket size assessment in L1 (analyst flags oversized tickets for manual splitting by PM — automated decomposition deferred to Phase 4)
- **Success metric:** Figma-linked tickets produce code that matches design structure (verified by agent-browser pixel diffs). Generated E2E tests run in CI. ADO pipeline operational.

### Phase 4: Production Hardening & Scale (Weeks 14-20)

Prepare for multi-client production deployment.

- Rebrand Composio dashboard with XCentium identity
- Implement graduated autonomy metrics (first-pass acceptance rate, defect escape rate, self-review catch rate)
- Add queue-based ticket processing for concurrent multi-ticket execution
- Build Client Profile configuration system (extract from hardcoded first-client config)
- Build Cross-Ticket Coordinator for automated ticket decomposition and reassembly
- Implement concurrent ticket conflict detection in L1
- Set up secrets management (Vault/AWS Secrets Manager) for multi-client credential isolation
- Load testing: 5-10 simultaneous tickets across a real client codebase
- Build Salesforce Platform Profile (#2)
- Onboard second client with their specific profile
- Documentation, runbooks, and client onboarding materials
- **Success metric:** First-pass PR acceptance rate >80%. System handles 5+ concurrent tickets reliably. Two platform profiles operational. Second client onboarded.

---

## 15. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Agent Teams is experimental | High — API could change | Agent Teams abstraction layer (Section 5.1) with file-based message fallback. System degrades to sequential but preserves correctness. |
| Max subscription throughput limits | Medium — concurrency ceiling | Empirical testing in Phase 1. If throttled, implement ticket queuing with priority ordering. |
| Figma API rate limits | Medium — throttled at scale | Extract design context + frame PNGs once in L1, cache as artifacts. Dev teammates use cached spec. QA uses cached frame PNGs as pixel-diff baselines via agent-browser. No downstream Figma API calls. |
| Atlassian MCP auth instability | Medium — session drops | Use self-hosted Jira MCP with API token auth, not Atlassian's SSE-based MCP. |
| Context window saturation | Low — with 1M window | Monitor token usage per teammate via observability layer. Use context editing (auto-compaction) for long sessions. |
| PR review bottleneck at scale | Medium — human review slows | Graduated autonomy: auto-merge low-risk PRs as confidence builds. AI PR review reduces human review time. |
| Security of agent-generated code | High — vulnerabilities | Code reviewer skill (Opus) includes security checks. CI includes SAST scanning. Human review catches residual. |
| Merge conflicts in parallel execution | Medium — semantic conflicts | Explicit merge strategy (Section 5.3 Phase 6) with topological ordering, test-after-merge, and squash fallback. |
| Cascading failures from poor L1 enrichment | Medium — wasted compute | Circuit breaker (Section 9.2) halts pipeline when >50% of QA criteria fail. Post-mortem artifacts preserved. |

---

## 16. Success Metrics

Three metrics determine the system's effectiveness and drive the graduated autonomy model:

- **First-pass acceptance rate:** Percentage of PRs approved by human reviewers without revision requests. Target: >80% by end of Phase 4.
- **Defect escape rate:** Of PRs approved and merged, how many had bugs found later. Target: <5% over rolling 30-day window.
- **Self-review catch rate:** Percentage of issues found by human reviewers that were also flagged by the AI code review or QA steps. Target: >85%. When this exceeds threshold, expand auto-merge scope.

Additional operational metrics:

- **Time-to-PR by ticket type:** Average elapsed time from "Ready for AI" to draft PR opened, segmented by story/task/bug and small/medium/large.
- **Escalation rate by phase:** Percentage of tickets that require human intervention at each phase, identifying systemic weaknesses.
- **Throughput:** Tickets processed per hour/day on a single Max subscription.

> **Graduated autonomy threshold:** When first-pass acceptance exceeds 90% and defect escape is below 5% over a rolling 30-day window, begin auto-merging low-risk PRs (bug fixes, style changes, dependency updates). When self-review catch rate exceeds 85%, expand to feature work. Never fully remove the human — move them from reviewing every PR to reviewing a statistical sample.

---

*End of Document — V2*
