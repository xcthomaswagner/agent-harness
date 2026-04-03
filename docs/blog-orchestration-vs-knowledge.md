# Why We Built a Knowledge-First Agent System Instead of Using an Orchestration Framework

*Thomas Wagner, XCentium GenAI Practice | March 2026*

Multi-agent AI is having its moment. CrewAI has over 100,000 certified developers. LangGraph hit 34 million monthly downloads. OpenAI, Google, Microsoft, and a wave of startups are all shipping agent orchestration frameworks. The pitch is compelling: define your agents, wire them together, and let them collaborate on complex work.

We spent the last two months building a system that turns Jira tickets into reviewed, tested, merge-ready Pull Requests using multiple AI agents. Along the way, we evaluated every major orchestration framework and chose a fundamentally different approach. Here is what we learned and why.

## What the Frameworks Actually Do

Every major agent orchestration framework solves the same core problem: **how do you get multiple LLM calls to coordinate?**

**CrewAI** gives you YAML-configured agents with roles, goals, and backstories. You arrange them in sequential chains or hierarchical delegations. A "researcher" agent feeds its output to an "analyst" agent, which feeds to a "writer" agent.

**LangGraph** gives you a directed graph where nodes are functions and edges route between them based on conditions. It is the most flexible but also the most code-heavy. Think of it as a state machine where each state is an LLM call.

**OpenAI's Agents SDK** uses a handoff model: Agent A can transfer control to Agent B, carrying conversation context. It is designed for customer service routing, where a triage bot dispatches to billing, refunds, or FAQ specialists.

**Google's ADK** uses a parent-child hierarchy where coordinator agents delegate to sub-agents. **AutoGen** puts agents in group chats where they take turns speaking.

The patterns vary but the underlying philosophy is the same: **orchestration is the hard problem, and agent intelligence comes from the LLM's general knowledge.**

## The 20-Agent Myth

Marketing materials love big numbers. "Orchestrate 20, 30, even 50 agents!" But Google Research published a paper in early 2026 that measured what actually happens at scale:

- **Centralized systems** (one orchestrator dispatching to specialists) contained error amplification to 4.4x
- **Decentralized systems** saw errors compound with each additional agent
- The practical sweet spot in production is **3-10 specialized agents with an orchestrator**

When you see "20-30 agents" in practice, it almost always means one of three things:

1. **20-30 definitions exist, but only 3-5 activate per request.** It is a routing catalog, not 30 things reasoning simultaneously.
2. **Simulations.** Research experiments with LLM personas in virtual worlds. Interesting science, not production software.
3. **Thin wrappers.** Each "agent" is a system prompt and an API call. Calling them agents is generous; they are functions with personality.

Real multi-agent production deployments at companies like Klarna, Elastic, and Amazon use small, focused teams with strong orchestration. The intelligence is in the design, not the headcount.

## What is Actually Missing

Here is what a QA agent looks like in CrewAI:

```yaml
qa_agent:
  role: "QA Tester"
  goal: "Find bugs in the code"
  backstory: "You are an experienced QA engineer..."
```

That is the entire agent definition. There is no testing rubric. No process for running unit tests, then integration tests, then E2E browser tests in sequence. No template for the output format. No circuit breaker that escalates when more than half the acceptance criteria fail. No pixel-diff verification against design mockups. No responsive viewport testing. No rule about how many fix cycles to attempt before giving up.

The frameworks provide **coordination primitives** (who talks to whom, in what order) but leave **domain expertise** entirely to the LLM's training data. For generic tasks like "summarize these documents" or "route this customer inquiry," training data is sufficient. For specialized work like "review this Pull Request for security vulnerabilities, then have a separate agent validate the findings to filter false positives, then route confirmed issues back to the developer with specific line references" — it is not.

## Knowledge-First vs. Orchestration-First

We took the opposite approach. Instead of sophisticated orchestration with shallow agents, we built deep agents with simple orchestration.

**Our QA agent** receives a 100+ line skill file that specifies: run unit tests first, then integration tests, then E2E flows via Playwright, then Figma design compliance checks using pixel diffs and computed CSS style verification. It knows to check for the existence of a `playwright.config.ts` before attempting E2E tests. It knows to export the QA matrix in a specific Markdown format that downstream agents can parse. It knows that edge case failures don't count toward the circuit breaker threshold but acceptance criteria failures do. It knows to attempt two fix cycles with the developer before escalating.

This is not prompt engineering. It is domain expertise expressed as structured skill files, rubrics, templates, and checklists that are injected into each agent's context at runtime. The agent does not need to figure out how to do QA from first principles. It receives a detailed playbook.

### The Comparison

| Dimension | Orchestration Frameworks | XC Agent Harness |
|---|---|---|
| Agent definition | System prompt + tools | Rich skill files with rubrics, templates, checklists, platform profiles |
| Agent knowledge | LLM general training data | Injected domain expertise per role |
| Per-agent capability | One LLM call per turn | Full Claude Code session with filesystem access, tool use, extended reasoning |
| Execution environment | In-process Python functions | Isolated OS processes in separate git worktrees |
| Orchestration complexity | Sophisticated (graphs, handoffs, state machines) | Simple (three-layer pipeline, file-based communication) |
| Domain depth | Generic (you build everything) | Deep (judge patterns, pixel-diff QA, parallel merge coordination, false-positive filtering) |

## How It Works in Practice

A Jira ticket labeled `ai-implement` triggers a three-layer pipeline:

**Layer 1: Pre-Processing.** A FastAPI service receives the webhook, normalizes the ticket, downloads image attachments, and runs a Ticket Analyst (a single Claude Opus API call with vision support). The analyst evaluates completeness against a rubric, generates acceptance criteria and test scenarios, detects conflicts with in-progress work, and decides whether to enrich the ticket, request clarification, or decompose it into sub-tickets. If the ticket includes a Figma URL, the analyst extracts the design specification (components, colors, typography, layouts) and renders frame PNGs for downstream visual verification.

**Layer 2: Agent Team Execution.** The enriched ticket spawns a team of specialized agents, each with role-specific skill files and tool restrictions:

- A **Planner** decomposes the ticket into atomic implementation units with a dependency graph
- A **Plan Reviewer** critiques the plan for gaps, missing edge cases, and parallelization errors
- **Developer agents** implement their assigned units in parallel, each in an isolated git worktree
- A **Code Reviewer** evaluates the merged result (read-only access, cannot modify code)
- A **Judge** validates code review findings — scoring each issue 0-100 to filter false positives before routing to developers
- A **QA agent** runs unit tests, integration tests, E2E browser flows, and Figma design compliance checks (pixel diffs, computed style verification, responsive viewport testing)
- A **Merge Coordinator** integrates parallel branches in topological order with conflict resolution

Each agent operates within enforced constraints. The code reviewer cannot write code. The QA agent cannot modify source files. The developer cannot skip tests. These constraints are not suggestions in a system prompt; they are tool-access restrictions that produce fundamentally different behavior.

**Layer 3: PR Review & Feedback.** GitHub webhooks trigger AI architecture review on new PRs, auto-fix agents for CI failures, and routing for human review comments.

The PR is the hard checkpoint. Everything before it is automated. The human reviews a Pull Request that has already been planned, implemented, self-reviewed, QA-validated, and simplified.

## What the Frameworks Get Right

This is not a dismissal of orchestration frameworks. They solve real problems:

**LangGraph's durable execution** is genuinely useful. Workflows that checkpoint state and survive crashes are important for production systems. If we were starting over, we might use LangGraph for our L1/L3 services instead of raw FastAPI.

**CrewAI's Flows** provide clean abstractions for event-driven agent coordination. Their `@start()`, `@listen()`, `@router()` decorators are elegant for defining when agents activate.

**OpenAI's handoff model** is the right abstraction for customer service. A triage agent that routes to specialists based on intent classification is a solved pattern.

The frameworks are excellent **infrastructure**. What they are not is a substitute for domain expertise. You would not build a hospital by designing the hallway layout and then hiring doctors who have only read Wikipedia. The hallways matter, but the medical knowledge matters more.

## The Anthropic Validation

Anthropic published a detailed account of their own multi-agent research system in 2026. Their findings align with our experience:

> The performance gains came from **spreading reasoning across multiple independent context windows** with good task decomposition, not from sophisticated inter-agent protocols.

Their system uses Claude Opus as a lead agent with Claude Sonnet sub-agents, achieving a 90% improvement over single-agent performance. The architecture is simple: the lead decomposes the problem, sub-agents work independently, the lead synthesizes results. The value is in the decomposition quality and the per-agent capability, not in the coordination mechanism.

This matches what we see in our pipeline. The Judge agent — which re-reads the actual code at each flagged line and scores whether a code review finding is a real issue or a false positive — adds more value than any amount of orchestration sophistication. It directly prevents wasted developer fix cycles on phantom issues. That is domain depth, not coordination depth.

## When to Use What

**Use an orchestration framework when:**
- Your agents are doing general-purpose work (content generation, research synthesis, customer routing)
- The LLM's training data is sufficient for each agent's task
- You need durable execution, checkpointing, or complex routing logic
- Your team is comfortable building agent intelligence from scratch

**Use a knowledge-first approach when:**
- Your agents need deep domain expertise (code review rubrics, QA processes, design compliance checks)
- Quality gates matter more than speed of coordination
- Each agent session is long-running with filesystem access and tool use
- You need enforced constraints (not just prompt suggestions) on what each agent can do
- The cost of a bad output is high (a merged PR with security vulnerabilities, a false-positive code review finding that wastes an hour of developer time)

For software development specifically, we believe the knowledge-first approach is correct. The hard problem is not getting agents to talk to each other. The hard problem is making each agent good enough at its job that the pipeline produces work a human would approve on the first review.

## What We Measure

Three metrics drive our system:

- **First-pass acceptance rate:** PRs approved without revision requests. Target: >80%.
- **Defect escape rate:** Merged PRs with bugs found later. Target: <5%.
- **Self-review catch rate:** Issues found by humans that the AI review also flagged. Target: >85%.

When self-review catch rate exceeds 85% over a rolling 30-day window, we expand auto-merge scope. The human never fully leaves the loop — they move from reviewing every PR to reviewing a statistical sample. This is graduated autonomy, not blind trust.

None of these metrics measure orchestration sophistication. They measure **output quality**. That is the point.

---

*The XC Agent Harness is an internal tool built by XCentium's GenAI Practice. It processes Jira tickets into reviewed, tested Pull Requests using Claude Code as the execution engine. The system currently supports multi-client routing, parallel development in isolated worktrees, and visual design verification against Figma exports.*
