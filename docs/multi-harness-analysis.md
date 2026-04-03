# Multi-Harness Scaling Analysis

> BCG-style assessment of running multiple harness instances — either one developer with multiple harnesses, or multiple developers each with their own harness.

---

## Scenarios

### Scenario A: One Developer, Multiple Harnesses
Thomas runs 3 harness instances simultaneously, each processing tickets for different client repos (harness-test-client, rockwell-test, a Salesforce project). One L1 service routes tickets to the correct harness instance based on client profile.

### Scenario B: Multiple Developers, Each With Their Own Harness
A 5-person XCentium team. Each developer runs their own harness instance on their machine (or a cloud VM), processing tickets from their assigned project. No shared infrastructure except Jira and GitHub.

### Scenario C: Centralized Multi-Tenant Harness
One harness deployment serves the entire team. Tickets from all projects funnel through a single L1, which dispatches to a pool of L2 workers. Shared observability, shared configuration, shared queue.

---

## Feasibility Assessment

### What Already Works

The harness is **already designed for multi-client routing.** Client profiles (`runtime/client-profiles/`) route tickets to the correct repo by project key. L1 is a single service that handles multiple projects. The missing pieces are about **concurrency and coordination**, not architecture.

| Capability | Current State | Multi-Harness Ready? |
|---|---|---|
| Client profile routing | Built, unit tested | Yes |
| Concurrent L2 sessions | One at a time per L1 instance | Needs queue worker |
| Worktree isolation | Per-ticket worktree | Yes — no cross-ticket interference |
| Agent identity | Single bot account (xcagentrockwell) | Would need per-developer or per-client identity |
| Git branch naming | `ai/<ticket-id>` | Yes — ticket IDs are globally unique |
| Trace/observability | Per-ticket JSONL logs | Yes — trace IDs are scoped to ticket |
| Session timeouts | Enforced per session | Yes |
| Jira webhooks | Single ngrok tunnel to one L1 | Bottleneck for Scenario A |

### What Needs Work

| Gap | Scenario A | Scenario B | Scenario C |
|---|---|---|---|
| **Concurrent L2 dispatch** | L1 queue worker (built but untested) | Not needed (each dev runs independently) | Required — shared queue with priority |
| **Claude Max session limits** | Risk — multiple L2 sessions compete for rate limits | Same risk per developer | Highest risk — all tickets share one subscription |
| **Webhook routing** | One L1 → multiple L2 spawns | Each dev has their own ngrok/L1 | Single L1, multiple workers |
| **Conflict detection** | Cross-harness file conflict detection | Developers coordinate manually (current state) | L1 already has conflict detection built |
| **Shared observability** | Traces scattered across instances | Each dev has their own dashboard | Single dashboard, all traces |
| **Configuration drift** | Multiple copies of skills/agents could diverge | Same risk | Single source of truth |

---

## BCG Matrix: Strategic Assessment

### Scenario A — One Dev, Multiple Harnesses

```
                        High Value
                            │
                     ┌──────┴──────┐
                     │   STARS     │  ← Scenario A is here
                     │             │    (high value, medium feasibility)
                     │             │
          Low ───────┼─────────────┼─────── High
       Feasibility   │             │     Feasibility
                     │  QUESTION   │
                     │   MARKS     │
                     └──────┬──────┘
                            │
                        Low Value
```

**Value: HIGH.** One senior developer supervising 3 concurrent pipelines processing tickets across multiple clients is a massive productivity multiplier. Instead of context-switching between projects, the developer reviews PRs from 3 parallel pipelines.

**Feasibility: MEDIUM.** Almost works today. The L1 queue worker exists but is untested. The main risk is Claude Max rate limits — if 3 L2 sessions run simultaneously, they may throttle each other.

**What to build:**
1. Test the queue worker with 2-3 concurrent tickets
2. Measure actual Max subscription throughput limits (planned for Phase 1 but never done)
3. Add ticket priority to the queue (urgent tickets first, large tickets deferred to off-hours)

**Effort:** 1-2 weeks testing + tuning. No architectural changes.

---

### Scenario B — Multiple Devs, Independent Harnesses

```
                        High Value
                            │
                     ┌──────┴──────┐
                     │             │
                     │             │
                     │             │
          Low ───────┼─────────────┼─────── High
       Feasibility   │             │     Feasibility
                     │             │  ← Scenario B is here
                     │             │    (medium value, high feasibility)
                     └──────┬──────┘
                            │
                        Low Value
```

**Value: MEDIUM.** Each developer gets their own AI assistant for their project. Useful, but doesn't unlock the multiplier effect of Scenario A (one person supervising many pipelines). It's essentially "everyone gets their own copilot with better quality gates."

**Feasibility: HIGH.** Works today with zero changes. Each developer:
- Clones the harness repo
- Sets up their own ngrok tunnel and Jira automation rule
- Configures a client profile for their project
- Runs `python services/l1_preprocessing/main.py` locally

The only coordination needed is ensuring two developers don't process the same ticket simultaneously. That's a human process issue, not a technical one.

**Risks:**
- Configuration drift — each developer's harness copy diverges over time
- No shared learning — if one developer improves a skill file, others don't benefit
- Cost — each developer needs their own Claude Max subscription ($100/month per seat)

**Mitigation:** Keep the harness as a shared Git repo. Developers pull updates. Skill improvements are committed centrally. Client-specific config stays in profiles.

---

### Scenario C — Centralized Multi-Tenant

```
                        High Value
                            │
                     ┌──────┴──────┐
                     │   STARS     │
                     │             │  ← Scenario C (future)
                     │             │    (highest value, lowest feasibility today)
          Low ───────┼─────────────┼─────── High
       Feasibility   │             │     Feasibility
                     │  QUESTION   │
                     │   MARKS     │
                     └──────┬──────┘
                            │
                        Low Value
```

**Value: HIGHEST.** One deployment serves the entire team. Shared observability, shared configuration, shared metrics for graduated autonomy. The team gets a single dashboard showing all pipeline activity across all projects. This is the production end-state.

**Feasibility: LOW (today).** Requires:
- Docker Compose or Kubernetes deployment (planned but not built)
- Durable job queue (Redis/SQS) replacing the in-memory queue worker
- Multi-subscription Max pooling (each concurrent L2 session needs its own Max seat)
- Centralized secrets management (Vault/AWS Secrets Manager)
- Shared trace storage (database instead of per-ticket JSONL files)

**Effort:** 4-6 weeks. This is essentially Phase 4 of the roadmap.

---

## Risk Analysis

### Risk 1: Claude Max Rate Limits (HIGH)

**The binding constraint.** Each Claude Max subscription supports a limited number of concurrent `claude -p` sessions. Running multiple L2 pipelines simultaneously means multiple long-running sessions competing for throughput.

**What we don't know:** Anthropic doesn't publish exact concurrent session limits for Max. Empirical testing was planned for Phase 1 but never done.

**Mitigation:**
- Queue with concurrency limit (run N tickets at a time, buffer the rest)
- Priority scheduling (small/quick tickets first, large tickets during off-hours)
- Per-session monitoring — if a session is throttled, back off and retry

**Impact if unmitigated:** Pipelines stall or produce degraded output (model falls back to shorter responses under rate pressure).

### Risk 2: Cross-Ticket File Conflicts (MEDIUM)

**Two tickets modifying the same files simultaneously.** Each ticket runs in its own worktree, so there's no git-level conflict during development. But when both PRs try to merge to `main`, one will have conflicts.

**Current state:** L1's conflict detection (`CONFLICT_DETECTION.md` in the analyst skill) checks in-progress tickets for overlapping file scopes. It's built but untested.

**Mitigation:**
- Test and enable conflict detection in L1
- If conflict detected: queue the second ticket until the first PR merges
- Or: flag both PRs with `potential-conflict` label for human awareness

**Impact if unmitigated:** Two PRs that can't both merge cleanly. Human resolves the conflict manually. Annoying but not catastrophic.

### Risk 3: Configuration Drift (MEDIUM — Scenario B only)

**Multiple copies of the harness diverge.** Developer A improves the QA skill, Developer B doesn't pull the update. Skills produce inconsistent quality across projects.

**Mitigation:**
- Single harness repo, all developers pull from `main`
- Skill files are never modified locally — client-specific behavior goes in client profiles
- CI check: compare local skill hashes against `main` to detect drift

**Impact if unmitigated:** Inconsistent PR quality across projects. Hard to debug because the same pipeline produces different results on different machines.

### Risk 4: Observability Fragmentation (LOW-MEDIUM)

**Traces scattered across machines/instances.** In Scenario B, each developer has their own trace dashboard. No one has a global view of pipeline health.

**Mitigation:**
- Centralized trace storage (even a shared SQLite or Postgres) that all instances write to
- Or: periodic trace export to a shared location
- Long-term: Langfuse integration (already on the future enhancements list)

**Impact if unmitigated:** Can't compute graduated autonomy metrics across the team. Each developer's autonomy level is independent, which is fine initially but limits the "auto-merge" progression.

### Risk 5: Cost Scaling (LOW)

**Claude Max is $100/month per seat.** For Scenario B with 5 developers, that's $500/month. For Scenario C, you need enough seats to cover peak concurrent sessions.

**Context:** The L1 analyst uses the Anthropic API (pay-per-token, ~$0.10/ticket). L2 and L3 sessions use Max (flat rate). The cost model is already favorable — the question is whether the flat rate covers the throughput needed.

**Mitigation:** Monitor actual session-hours per developer per month. If a developer's harness is idle most of the time, they can share a Max seat with another developer (not simultaneously).

---

## Recommended Path

### Phase 1: Validate Concurrency (Now — 1 week)

Run 2-3 concurrent tickets through the existing L1 queue worker on a single Max subscription. Measure:
- Actual throughput (tokens/minute under concurrent load)
- Session latency (does the second session slow down the first?)
- Failure modes (throttling, timeouts, degraded output)

This answers the binding question: **how many concurrent pipelines can one Max subscription support?**

### Phase 2: Scenario A — One Dev, Multiple Clients (2-3 weeks)

Once concurrency limits are known:
- Enable the queue worker with a concurrency cap
- Add ticket priority (small/quick first)
- Test with 2 client profiles processing tickets simultaneously
- Monitor via the existing trace dashboard

This is the highest ROI step: one senior developer supervising 3 parallel pipelines.

### Phase 3: Scenario B — Team Rollout (When ready for second developer)

- Document the single-developer setup process
- Create a `docs/developer-setup.md` with step-by-step (ngrok, Jira automation, client profile, `.env`)
- Establish the "pull from main" discipline for skill updates
- Each developer runs independently; coordinate ticket assignment via Jira board

### Phase 4: Scenario C — Centralized (When team > 3 developers)

- Docker Compose deployment
- Durable queue (Redis)
- Centralized trace storage
- Shared graduated autonomy metrics
- Multi-seat Max subscription pooling

---

## Summary

| Scenario | Value | Feasibility | Effort | When |
|---|---|---|---|---|
| **A: One dev, multiple clients** | High | Medium | 1-2 weeks | Now (test concurrency first) |
| **B: Multiple devs, independent** | Medium | High | Documentation only | When second developer onboards |
| **C: Centralized multi-tenant** | Highest | Low (today) | 4-6 weeks | When team exceeds 3 developers |

**The binding constraint across all scenarios is Claude Max concurrent session throughput.** Everything else is solvable with known patterns. Test that first.

---

*Last updated: 2026-03-30*
