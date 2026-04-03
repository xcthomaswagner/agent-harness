# Future Enhancements

## ai-explain Label — Ticket Clarification

**Use case:** Someone wants clarification on work that was done on a ticket. They add the `ai-explain` label to a completed ticket with a comment asking their question.

**Proposed flow:**
1. Jira automation fires webhook on `ai-explain` label added
2. L1 receives the webhook, reads the ticket + all comments + linked PR
3. L1 makes a single Claude API call (no L2 spawn, no agent team) that:
   - Reads the original ticket description and acceptance criteria
   - Reads the PR diff (fetched via GitHub API using the PR link from the completion comment)
   - Reads the latest Jira comment (the question being asked)
4. Posts a Jira comment with a plain-language explanation answering the question
5. Removes the `ai-explain` label so it doesn't re-trigger

**Why a new label instead of reopening:** Reopening a Done ticket and re-triggering `ai-implement` would kick off the full pipeline (analyst → developer → reviewer → QA → new PR), which is not what's needed. A separate label keeps the flows distinct.

**Effort:** Small — L1-only change, no L2/L3 involvement. New webhook handler + one Claude API call.

## ai-revise Label — Rework Based on Feedback

**Use case:** A PR was created but the human reviewer wants changes that go beyond what the L3 comment handler can do. They add `ai-revise` to the ticket with detailed feedback in a comment.

**Proposed flow:**
1. Jira automation fires webhook on `ai-revise` label
2. L1 reads the ticket + the latest comment (the revision request) + the existing PR
3. L1 spawns an agent on the existing branch (not a new worktree) that:
   - Reads the revision request from the Jira comment
   - Makes the requested changes on the existing PR branch
   - Runs review + QA
   - Pushes to the same PR
4. Posts a Jira comment confirming the revision is done

**Why not just re-trigger ai-implement:** That would create a new branch and new PR, losing the review history. `ai-revise` works on the existing PR.

## Figma MCP Integration for Agent Sessions

**Use case:** Give L2 developer agents direct access to Figma design context during implementation — richer than the REST-extracted design spec they receive today.

**Why not now:** The official Figma MCP server (`mcp.figma.com`) requires interactive OAuth authentication. Our agent sessions run headless via `claude -p` and cannot complete the browser-based OAuth flow. Figma has explicitly declined to add PAT (Personal Access Token) support to the MCP server.

**What it would unlock:**
- `get_design_context` — returns code-ready component/layout representation (React+Tailwind, configurable)
- `get_variable_defs` — design tokens (spacing, colors, typography) as structured variables
- `search_design_system` — query across all connected libraries for components/variables/styles
- `get_screenshot` — visual screenshot of any selection

**Current approach:** L1's REST-based Figma extractor (`figma_extractor.py`) fetches the file tree at depth=4, extracts components/colors/typography, and renders up to 5 frames as PNGs. The structured design spec is passed via the enriched ticket JSON. The rendered frame PNGs are copied into the agent worktree at `.harness/attachments/figma-*.png` and used by the QA skill as **pixel-diff baselines** — `agent-browser diff screenshot` compares the rendered page against these baselines to verify visual fidelity.

**Unblock conditions (any one):**
1. Figma adds PAT/API-token auth to their MCP server
2. A third-party MCP wrapper (e.g. `figma-console-mcp`) matures to expose the same rich tools
3. Claude Code adds OAuth token caching that persists across headless `claude -p` sessions

**Rate limits to watch:** Official MCP allows 200 calls/day (Pro Full seat) or 600/day (Enterprise), 15-20 calls/min. Our REST API hit a ~4-day cooldown on community tokens — MCP limits are at least documented and predictable.

**References:**
- [Figma MCP Tools & Prompts](https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/)
- [PAT Support Request (Figma Forum)](https://forum.figma.com/ask-the-community-7/support-for-pat-personal-access-token-based-auth-in-figma-remote-mcp-47465)

## Lint-First Agent Feedback Loop

**Inspiration:** Factory.ai's "Using Linters to Direct Agents" (Alvin Sng, 2025) and their open-source `@factory/eslint-plugin`. Core insight: encode conventions as machine-verifiable lint rules rather than prose instructions. Agents self-correct against precise lint errors (file X, line Y, rule Z) in a tight loop until green.

**What we already do:** Developer agents run the project's configured linters as part of implementation, and the QA skill runs the full test suite including lint. CLAUDE.md specifies lint commands per client repo. Agents already self-correct against linters — we just don't formalize it as a named pipeline phase.

**What to add:**

### 1. Explicit Lint Gate Phase (Low effort)

**The problem today:** Linting happens inside the developer agent's implementation loop — the developer runs tests and linters as part of its 3 self-correction attempts, but lint failures can still slip through to code review. When they do, the Code Reviewer flags them, the Judge has to evaluate whether they're real issues, and a developer fix cycle gets burned on something a linter could have caught automatically. That's wasted pipeline time and token spend.

**What to do:** Add a named "Lint Gate" phase between implementation and code review in `harness-CLAUDE.md`:
- Developer completes implementation and commits
- **Lint Gate:** Team Lead runs all configured linters (`pnpm lint`, `pnpm typecheck`, `ruff check`, etc.). If failures, route back to developer with the exact lint output. Max 2 cycles.
- Only when lint-green → proceed to code review

**Why it matters:** The Code Reviewer and Judge become more effective because they never see formatting, import ordering, type errors, or other mechanical issues. They focus on logic, security, and architecture — the things linters can't catch. Fewer false positives, fewer wasted fix cycles, faster pipeline.

### 2. Client Profile Lint Configuration (Low effort)

**The problem today:** The harness assumes each client repo has lint commands but doesn't know what they are. The developer agent discovers them by reading `package.json` or `CLAUDE.md`. Different clients use different stacks — one runs `pnpm lint`, another runs `ruff check .`, a Salesforce project might run `sf project deploy start --dry-run`. There's no central place to configure this.

**What to do:** Add a `lint_commands` field to client profiles (`runtime/client-profiles/`):

```yaml
lint_commands:
  - "pnpm lint"
  - "pnpm typecheck"
quality_gate: lint_green_required  # or lint_warnings_ok
```

**Why it matters:** The Lint Gate phase (above) needs to know what commands to run. Client profiles already handle project-key routing and repo paths — adding lint config means the harness works correctly across different stacks without the agent guessing. It also lets us set the strictness level per client: some clients may accept warnings, others require zero lint errors.

### 3. Agent-Friendly Lint Recommendations for Client Onboarding (Medium effort)

**The problem today:** Agent effectiveness depends heavily on how navigable a codebase is. Agents find code via `grep` and `glob`. If a codebase uses anonymous default exports, inconsistent file naming, or scattered type definitions, agents waste context window tokens searching for things and sometimes put new files in the wrong place.

**What to do:** Create a `docs/client-onboarding-lint-guide.md` that we share when setting up a new client. Factory identifies six categories of agent-friendly lint rules; the two highest-impact for our agents:

**Grep-ability** — Named exports and deterministic naming conventions. Example: `export function UserProfile()` is instantly findable via `grep "function UserProfile"`. `export default () => { ... }` is invisible to search. ESLint rule: `no-default-export`. This alone makes agents dramatically faster at navigating unfamiliar code.

**Glob-ability** — Predictable file placement. Types live in `types.ts`, constants in `constants.ts`, tests colocated next to source files (not in a separate `__tests__/` tree). When an agent needs to add a new type, it knows exactly where to put it. When it needs to find existing constants, it knows exactly where to look. ESLint rules: `types-file-organization`, `constants-file-organization`, `test-file-location`.

**Why it matters:** These aren't harness features — they're client codebase improvements that make every agent in the pipeline more effective. A well-organized codebase means fewer wrong guesses during planning, faster implementation, and fewer code review findings about misplaced files. We don't enforce these (every client has their own conventions), but we recommend them. The ROI is high: adding 3-4 lint rules to a client repo can measurably reduce agent pipeline time.

### 4. Lint-Driven Mass Refactoring (Future)

**The problem:** Codebases accumulate technical debt in patterns — inconsistent naming, deprecated API usage, old import styles, missing test files. Fixing these manually is tedious and error-prone. Fixing them with a feature ticket doesn't make sense because there's no feature — it's mechanical cleanup.

**What to do:** A new `ai-lint-fix` Jira label. The workflow:
1. Client adds a new lint rule to their repo (e.g., `no-default-export`)
2. The rule immediately surfaces hundreds of existing violations
3. Client creates a ticket, labels it `ai-lint-fix`, and references the rule
4. The harness spawns agents to fix all violations across the codebase
5. PR includes the fixes + the rule is now enforced in CI, preventing regression

Factory describes this as a continuous cycle: observe drift → codify as lint rule → spawn agents to fix all violations → rule prevents recurrence. It's a different pipeline mode than `ai-implement` because there's no planning or decomposition needed — just mechanical rule compliance applied file by file.

**Why it matters:** This turns the harness into a codebase hygiene tool, not just a feature delivery tool. Clients get a way to modernize their codebase incrementally: add a rule, run the agents, and the entire repo conforms. The agents are perfect for this because lint fixes are mechanical, deterministic, and easily verified (lint passes = done).

**References:**
- [Using Linters to Direct Agents (Factory.ai)](https://www.factory.ai/blog/using-linters-to-direct-agents)
- [Factory ESLint Plugin (GitHub)](https://github.com/Factory-AI/eslint-plugin)

## Ticket Re-processing (Reopened Tickets)

**Use case:** A ticket was Done, but a bug was found in the implementation. Someone moves it back to "To Do" and adds `ai-implement`.

**Current behavior:** The spawn script detects the existing worktree and cleans it up before creating a new one. A fresh pipeline run happens. This works but creates a new PR rather than updating the existing one.

**Future improvement:** Detect that a PR already exists for this ticket (check for `ai/<ticket-id>` branch). If so, work on the existing branch instead of creating a new one. This preserves review history and avoids PR proliferation.
