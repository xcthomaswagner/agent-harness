# Research Skill Specification

> Hand this document to the skill-creator. It contains the complete design for a `/research` skill that follows Anthropic's orchestrator-workers + evaluator-optimizer pattern.

---

## What This Skill Does

A multi-agent research pipeline that produces verified, source-classified findings on any topic. The user asks a research question, the skill orchestrates parallel researchers and an adversarial reviewer, and returns structured output with a confidence matrix showing which claims are verified, unverified, contradicted, or unsupported.

**The problem it solves:** When Claude Code researches a topic, the assembled findings frequently contain errors — confident claims from training data presented as fact, outdated information, misattributed statistics, and logical leaps. A separate review agent consistently finds these problems. This skill automates the research + adversarial review cycle so the user receives pre-vetted output.

## When It Should Trigger

- User says `/research <question>`
- User says "research this topic", "investigate", "do a deep dive on", "what's the current state of"
- User asks a question that requires consulting multiple sources and synthesizing findings (not a quick factual lookup)

## When It Should NOT Trigger

- Quick factual questions ("what's the capital of France")
- Code-related questions (those use existing coding skills)
- Questions about the current project/codebase (those use Grep/Read/Glob)
- Summarizing a single document or URL (that's just reading + summarizing)

---

## Architecture

Follows Anthropic's proven pattern from their Claude Research system: **Orchestrator-Workers + Evaluator-Optimizer.**

### Four Roles

#### 1. Lead Researcher (Orchestrator + Evaluator)

**Model:** Opus (or whatever the user's default model is)

**Responsibilities:**
- Scopes the question (clarification, complexity assessment, knowledge store check)
- Decomposes into independent sub-questions
- Spawns parallel researcher agents
- Assembles findings into a draft document
- Reads the Reviewer's assessment
- Decides whether to re-research contradicted claims (max 1 additional cycle)
- Produces final structured output

**The Lead does NOT do the adversarial review.** That must be a separate agent with a fresh context window. The Lead's cognitive stance is "produce good research." The Reviewer's stance is "find what's wrong." These must not share context.

#### 2. Researcher Subagents (Workers)

**Model:** Sonnet (faster, parallel, disposable)

**Spawned by:** Lead, via `Agent()` tool

**Each researcher receives:**
- One sub-question from the Lead's decomposition
- Output format template (structured findings with source citations)
- Constraints: max 8 sources, 5-minute scope, condensed output (1000-2000 tokens)

**Each researcher returns:**
- Findings as bullet points
- Source-to-claim mapping (which source supports which claim)
- Source metadata: URL, title, date accessed, source type

**How many researchers:** The Lead decides based on complexity:
- Simple fact-finding: 1 researcher, no parallelism needed
- Comparison/landscape: 2-4 researchers in parallel
- Deep multi-domain research: up to 5 researchers

**Researchers use:** WebSearch, WebFetch, Read (for knowledge store), Grep, Glob

#### 3. Reviewer (Adversarial Evaluator)

**Model:** Opus (needs strong reasoning to catch subtle errors)

**Spawned by:** Lead, as a separate `Agent()` call AFTER findings are assembled

**Critical design requirement:** The Reviewer receives ONLY the assembled draft document. It does NOT see the research process, the sub-questions, the individual researcher outputs, or the Lead's reasoning. Fresh context = fresh evaluation.

**The Reviewer's job:**
1. Read the assembled findings
2. For each major claim, attempt independent verification:
   - Run its own web searches to spot-check
   - Check if the cited source actually says what the claim says (fetch the URL if possible)
   - Look for contradicting sources
3. Classify each claim:
   - **Verified** — found independent confirmation from a credible source
   - **Unverified** — couldn't confirm, but didn't find contradictions either
   - **Contradicted** — found conflicting information (include the conflicting source)
   - **Unsupported** — no source provided by the researcher, and Reviewer couldn't find one
4. Flag logical issues: non-sequiturs, correlation presented as causation, survivorship bias, cherry-picked data
5. Produce a Review Matrix

**The Reviewer does NOT rewrite the findings.** It only classifies and flags. The Lead decides what to do with the review.

**Reviewer uses:** WebSearch, WebFetch, Read (same tools as researchers, used independently)

#### 4. Lead Again (Evaluator-Optimizer Loop)

After reading the Review Matrix, the Lead:
- Accepts Verified claims as-is
- Marks Unverified claims with a confidence note in the final output
- For Contradicted claims: spawns ONE targeted re-research agent to resolve the specific contradiction (max 1 cycle, max 2 contradictions re-researched — don't re-research everything)
- Marks Unsupported claims with a warning
- Assembles final output with the Review Matrix attached

---

## Knowledge Store Abstraction

The skill optionally reads from the user's knowledge store during scoping and research. The store is configured via a section at the top of SKILL.md that the user customizes.

### Operations

| Operation | Purpose |
|---|---|
| SEARCH(query) | Find existing notes/docs related to the research topic |
| READ(path) | Read content of a specific note |
| APPEND(path, content) | Add findings to an existing note (not overwrite) |
| LIST_RECENT(topic, days) | Find recently written notes on a topic |

### Backend Configurations

```yaml
# Obsidian (via obs CLI)
knowledge_store:
  type: obsidian
  search: 'obs search query="{query}" limit=10'
  read: 'obs read path="{path}"'
  append: 'obs append path="{path}" content="{content}" silent'
  write: 'obs create path="{path}" content="{content}" silent'
  list_recent: 'obs search query="{query}" limit=5'

# Local markdown directory
knowledge_store:
  type: local_files
  base_dir: "~/research"
  search: 'grep -rl "{query}" ~/research/ | head -10'
  read: 'cat ~/research/{path}'
  write: '# write to ~/research/{path}'

# No knowledge store
knowledge_store:
  type: none
```

### Graceful Degradation

| Store Configured | Behavior |
|---|---|
| Yes | Scoping checks for existing notes; findings can reference prior research |
| No | Scoping still asks clarifying questions; just skips the "what do you already know" check |

**Scoping always runs regardless of knowledge store.** The store adds context but scoping (clarification, complexity assessment, output format selection) is valuable on its own.

---

## Constraints

### Timeouts

| Scope | Timeout | Rationale |
|---|---|---|
| Per researcher subagent | 5 minutes | Prevents rabbit holes; forces concise research |
| Reviewer agent | 8 minutes | Needs time to spot-check via independent searches |
| Re-research agent (for contradictions) | 5 minutes | Targeted, single-claim verification |
| Total pipeline | 30 minutes | Hard ceiling on entire research session |

### Retry Limits

| Phase | Max Attempts | On Exhaustion |
|---|---|---|
| Researcher produces no useful findings | 1 retry with reformulated query | Mark sub-question as "insufficient sources" |
| Reviewer can't access cited URLs | Skip that claim's verification | Mark as "Unverified — source inaccessible" |
| Re-research for contradictions | 1 attempt per contradiction, max 2 contradictions | Include both versions in output, flag for user |
| Knowledge store unreachable | Skip store operations | Continue without store context |

### Capacity Limits

| Limit | Value | Rationale |
|---|---|---|
| Max sub-questions | 5 | Beyond 5, the synthesis becomes unwieldy |
| Max sources per researcher | 8 | Prevents context window bloat |
| Max web fetches per researcher | 6 | Rate limiting, focus |
| Max research cycles | 2 (initial + 1 re-research) | Diminishing returns beyond 2 |
| Max contradictions to re-research | 2 | Focus on the most important discrepancies |
| Researcher output size | 1000-2000 tokens | Condensed summaries, not raw dumps |

### Circuit Breaker

If >50% of sub-questions return "insufficient sources," stop the pipeline and tell the user: "This topic doesn't have enough accessible sources for reliable research. Consider narrowing the question or providing specific sources to consult."

---

## Output Format

### Final Research Output

```markdown
# Research: <question>

## Summary
<3-5 sentence executive summary of key findings>

## Findings

### <Sub-question 1>
- <Finding with inline source classification>
  **Source:** <URL or reference> | **Classification:** Verified

- <Finding>
  **Source:** Training data only | **Classification:** Unsupported — treat with caution

### <Sub-question 2>
...

## Review Matrix

| # | Claim | Source Type | Reviewer Classification | Notes |
|---|-------|-----------|------------------------|-------|
| 1 | <claim summary> | Primary (gov report) | Verified | Independent confirmation found |
| 2 | <claim summary> | Secondary (news article) | Verified | Corroborated by 2 sources |
| 3 | <claim summary> | None cited | Unsupported | No source found by reviewer |
| 4 | <claim summary> | Secondary (blog) | Contradicted | Conflicting data: <source> says X |

## Confidence Summary
- Verified: X claims
- Unverified: X claims
- Contradicted: X claims (see details above)
- Unsupported: X claims

## Sources Consulted
1. <URL> — <title> — accessed <date>
2. ...

## Gaps & Limitations
- <What couldn't be determined>
- <What the user should verify independently>

---
*Researched by /research skill | <date> | <N> researchers | <N> sources consulted | Review: <N> verified, <N> flagged*
```

### Source Type Classification

The skill uses categorical classification, not numeric scoring:

| Classification | Definition | Example |
|---|---|---|
| **Primary Source** | Original data, official report, peer-reviewed paper, company filing | SEC filing, WHO dataset, Nature paper |
| **Secondary Source** | Reputable reporting or analysis of primary sources | Reuters article, Gartner report, established trade publication |
| **Tertiary Source** | Blog, forum, opinion piece, aggregator content | Medium post, Reddit thread, Stack Overflow answer |
| **Training Data Only** | Claim comes from LLM knowledge with no cited source | Agent stated a fact without providing a URL or reference |

**Why not numeric scores:** A number (e.g., "confidence: 73") implies false precision. The model cannot determine whether a claim is 73% or 77% likely to be true. Categorical classification honestly communicates what the system actually knows: the *type* of evidence supporting a claim, not a probability of correctness.

---

## Pipeline Walkthrough (Example)

**User:** `/research "How are enterprises deploying multi-agent AI systems in production as of 2026?"`

**Step 1 — Lead scopes:**
- Searches knowledge store: finds 2 existing notes on multi-agent systems
- Reads them: user already knows about CrewAI, LangGraph, Anthropic's research system
- Assesses complexity: comparison/landscape → 3-4 researchers
- Asks: "You have existing notes covering framework comparisons from March 2026. Want me to focus on what's changed since then, or do a fresh landscape? Target output: vault note, blog draft, or internal reference?"
- User answers: "What's changed since March, vault note format"
- Decomposes into 4 sub-questions:
  1. New frameworks or major releases since March 2026
  2. New production deployment case studies
  3. Published failure modes or lessons learned at scale
  4. Evolution of the orchestration vs. knowledge-first debate

**Step 2 — Parallel researchers (4 Sonnet agents):**
- Each gets one sub-question + output template + constraints
- Each runs web searches, fetches key articles, produces 1000-2000 token findings with sources
- Complete in ~3-4 minutes

**Step 3 — Lead assembles:**
- Reads all 4 researcher outputs
- Merges into a single draft document organized by sub-question
- Removes obvious duplicates

**Step 4 — Reviewer (separate Opus agent):**
- Receives ONLY the assembled draft
- Reads each claim, spot-checks 5-8 via independent web searches
- Finds: one statistic is from 2024 not 2026, one company mentioned doesn't exist, one growth number contradicts the cited source
- Produces Review Matrix: 15 Verified, 3 Unverified, 2 Contradicted, 1 Unsupported

**Step 5 — Lead evaluates:**
- 2 Contradicted claims: spawns 1 re-research agent to check both specifically
- Re-research confirms one was wrong (corrects it), the other was a legitimate discrepancy (includes both versions)
- 1 Unsupported claim: marks with warning in final output
- Assembles final output with Review Matrix attached

**Step 6 — Output:**
- Writes to knowledge store (if configured)
- Presents to user with confidence summary: "18 claims verified, 2 unverified, 1 corrected after review, 1 flagged — see Review Matrix for details"

---

## Test Prompts for Skill Creator Eval

Use these to evaluate the skill against a baseline (no-skill) research:

### Test 1: Factual landscape (should decompose, parallelize)
**Prompt:** "What are the current pricing models for AI agent platforms — Salesforce Agentforce, ServiceNow, Microsoft Copilot Studio, and AWS Bedrock Agents?"
**Assertions:**
- Output should have separate findings per platform (not blended)
- Each pricing claim should cite a source
- Review Matrix should be present
- At least one claim should be classified as Unverified or Unsupported (pricing changes frequently)

### Test 2: Simple factual (should NOT over-decompose)
**Prompt:** "What is the current Claude Max subscription price and what does it include?"
**Assertions:**
- Should use 1 researcher, not 4
- Should complete in under 5 minutes
- Review should still verify the price against anthropic.com

### Test 3: Opinionated/analytical (harder to verify)
**Prompt:** "Is the multi-agent AI framework market consolidating or fragmenting as of 2026?"
**Assertions:**
- Output should distinguish between facts (funding rounds, adoption numbers) and analysis (market direction)
- Analytical claims should be classified as Secondary or Tertiary source
- Review Matrix should flag opinion-as-fact if present

### Test 4: Knowledge store integration (if configured)
**Prompt:** "What's changed in Salesforce Agentforce since my last notes on it?"
**Assertions:**
- Should search knowledge store first
- Should identify what the user already knows
- Findings should focus on delta, not repeat existing knowledge

### Test 5: Insufficient sources (should trigger circuit breaker)
**Prompt:** "What are the internal agent orchestration patterns used by Palantir's AIP platform?"
**Assertions:**
- Most sub-questions should return limited results (Palantir is notoriously opaque)
- Circuit breaker should fire or output should clearly state "limited public information available"
- Should NOT fabricate details to fill gaps

---

## What This Skill Does NOT Do

- **Does not write code.** It's a research tool, not a coding tool.
- **Does not make decisions for the user.** It presents findings with confidence classifications. The user decides what to trust.
- **Does not guarantee accuracy.** The Review Matrix communicates confidence honestly. Unsupported and Unverified claims are marked, not hidden.
- **Does not replace domain expertise.** For specialized topics (medical, legal, financial), the output should be treated as a starting point for expert review, not a final answer.
- **Does not persist state between invocations.** Each `/research` call is independent. The knowledge store provides continuity, but the skill itself is stateless.

---

## v2 Enhancements (Not for v1)

- **Grounding Agent** — dedicated agent that cross-references findings against knowledge store for contradiction detection and redundancy filtering. Deferred because the ordering question (ground before or after review?) needs empirical data.
- **Write-back to knowledge store** — automatically save findings. Deferred because write-back of incorrect findings creates data quality risk. v1 presents output; user decides whether to save.
- **Backlink/tag queries** — for Obsidian users, leverage `obs backlinks` and tag search for richer grounding. Requires the knowledge store abstraction to support BACKLINK and TAG_SEARCH operations.
- **Citation chain verification** — Reviewer follows citation chains (source A cites source B — does B actually say that?). Expensive but catches a common error pattern.
- **Research session history** — track what was researched and when, so future research can build on past sessions without re-researching the same ground.
