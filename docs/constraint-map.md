# Constraint Map

> Every hard constraint in the harness — tool restrictions, retry limits, circuit breakers, timeouts, phase ordering, and behavioral boundaries. This is the single reference for auditing what the system can and cannot do.
>
> **Design principle:** "Constraints compose; behaviors don't." You can combine constraint sets and predict what happens at their boundaries. You cannot combine goal-seeking agents and predict anything. Constraint-first design is what makes the pipeline tractable.

---

## 1. Pipeline Phase Ordering

The pipeline phase order is fixed. No agent can skip, reorder, or bypass phases.

### Simple Pipeline (single unit)
```
Ticket Read → Implementation → Code Review [→ Judge if needed] → QA → Simplify → Push + PR
```

### Full Pipeline (multiple units)
```
Ticket Read → Planning → Plan Review → Parallel Implementation → Merge → Code Review [→ Judge] → QA → Simplify → Push + PR
```

**Source:** `runtime/harness-CLAUDE.md` (entire file defines this ordering)

---

## 2. Retry Limits & Circuit Breakers

Every phase has a max retry count. After exhaustion, the pipeline escalates — it never retries indefinitely.

| Phase | Constraint | Max | On Exhaustion | Source |
|-------|-----------|-----|--------------|--------|
| Planning | Planner attempts | 2 | Escalate with analysis | `harness-CLAUDE.md:115` |
| Plan Review | Review-correction cycles | 2 | Escalate with plan + issues | `harness-CLAUDE.md:139` |
| Implementation | Developer self-correction per unit | 3 | Mark unit BLOCKED, continue others | `harness-CLAUDE.md:57` |
| Code Review | Review-fix cycles | 2 | Proceed with warnings in PR | `harness-CLAUDE.md:300` |
| QA Validation | QA-dev round trips per criterion | 2 | Include failure details in PR | `harness-CLAUDE.md:322` |
| QA Validation | **Circuit breaker: >50% AC failure** | — | **Escalate entire ticket** | `harness-CLAUDE.md:324` |
| Merge | Conflict resolution attempts | 2 | Squash fallback + `needs-human-merge` label | `harness-CLAUDE.md:224-226` |
| Any phase | Sub-agent crash | 1 retry | Escalate | `harness-CLAUDE.md:446` |

**Circuit breaker detail:** Only original acceptance criteria (`acceptance_criteria` + `generated_acceptance_criteria`) count toward the 50% threshold. Edge cases and design compliance checks do NOT count. This prevents non-critical failures from triggering a full escalation.

---

## 3. Session Timeouts

Every agent session has a hard timeout enforced by the spawner. Stuck agents are killed.

| Session Type | Default Timeout | Env Override | Source |
|-------------|----------------|--------------|--------|
| L2 Quick mode | 30 min (1800s) | `AGENT_TIMEOUT_SECONDS` | `spawn_team.py:182` |
| L2 Multi mode | 90 min (5400s) | `AGENT_TIMEOUT_SECONDS` | `spawn_team.py:182` |
| L3 PR review | 30 min (1800s) | `L3_SESSION_TIMEOUT` | `spawner.py:122-123` |
| L3 CI fix | 30 min (1800s) | `L3_SESSION_TIMEOUT` | `spawner.py:122-123` |
| L3 Comment response | 15 min (900s) | `L3_SESSION_TIMEOUT` | `spawner.py:122-125` |
| Figma API calls | 30 sec | — (hardcoded) | `figma_extractor.py:61` |

---

## 4. Tool Restrictions by Agent Role

Each agent has enforced tool access. These are not suggestions — they are tool-level restrictions that produce fundamentally different cognitive behavior.

| Role | Can Read Code | Can Write Code | Can Run Commands | Can Spawn Agents | Special Access |
|------|:---:|:---:|:---:|:---:|---|
| **Team Lead** | Yes | **No** | Yes | **Yes** | Orchestration only — must delegate all work |
| **Planner** | Yes | **No** | Yes (read-only) | No | Writes to `.harness/plans/` only |
| **Plan Reviewer** | Yes | **No** | No | No | Writes corrected plans only |
| **Developer** | Yes | **Yes** | Yes | No | Restricted to assigned unit files |
| **Code Reviewer** | Yes | **No** | Yes (lint/coverage) | No | Cannot modify source files |
| **Judge** | Yes | **No** | Yes (git blame) | No | Scoring only — no code changes |
| **QA** | Yes | **No** | Yes (test runners) | No | agent-browser + Playwright MCP |
| **Merge Coordinator** | Yes | **Yes** (merge only) | Yes (git) | No | Git merge operations only |

**Key principle:** The Code Reviewer, Judge, and QA agent cannot write code. This separation ensures review quality — a reviewer who can also fix code is incentivized to fix rather than flag.

---

## 5. Scoring & Threshold Constraints

| Constraint | Value | Effect | Source |
|-----------|-------|--------|--------|
| Judge validation threshold | **80+** | Only issues scoring 80-100 are routed to developer | `harness-CLAUDE.md:276` |
| Judge security exception | **60+** | Security findings pass at lower threshold | `agents/judge.md:57` |
| Graduated autonomy: semi-auto | 90% first-pass, <5% defect escape, 20+ PRs | Enable auto-merge of low-risk PRs | `autonomy.py:26-30` |
| Graduated autonomy: full-auto | 95% first-pass, <3% defect escape, 85% self-review catch, 50+ PRs | Enable auto-merge with sampling | `autonomy.py:32-37` |
| Autonomy rolling window | 30 days | Metrics evaluated over trailing window | `autonomy.py:39` |

---

## 6. File & Size Constraints

| Constraint | Value | Source |
|-----------|-------|--------|
| Max image attachment size | 5 MB | `models.py:59` |
| Max Figma rendered frames | 5 | `figma_extractor.py:30` |
| Supported image types | PNG, JPEG, GIF, WebP | `models.py:52-57` |
| Analyst max output tokens | 4,096 | `analyst.py:215` |
| Max recommended devs per ticket | 10 | `models.py:109` |
| Min estimated units | 1 | `models.py:108` |

---

## 7. Naming & Convention Constraints

| Constraint | Pattern | Why | Source |
|-----------|---------|-----|--------|
| Feature branch | `ai/<ticket-id>` | Merge coordinator depends on this | `harness-CLAUDE.md:27` |
| Unit branches | `ai/<ticket-id>/unit-<N>` | Merge coordinator searches for this pattern | `harness-CLAUDE.md:147` |
| Never push to default branch | — | Prevent accidental direct merges | `harness-CLAUDE.md:470` |
| Plan versioning | `plan-v1.json`, `plan-v2.json`, etc. | Never overwrite; team lead reads highest | `harness-CLAUDE.md:139` |
| Agent git identity | `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL` | Separate agent commits from human commits | `spawn_team.py:122-123` |
| PR always draft | `--draft` flag required | Human must explicitly approve | `harness-CLAUDE.md:372` |
| PR comment marker | `<!-- xcagent -->` | Bot-loop detection in L3 | `harness-CLAUDE.md:393` |

---

## 8. Behavioral Prohibitions

Hard "never do this" rules enforced by convention and skill files:

| Prohibition | Why | Source |
|------------|-----|--------|
| Never commit `.claude/skills/`, `.claude/agents/`, `.harness/` | Harness files are injected at runtime, not part of client repo | `harness-CLAUDE.md:471` |
| Never commit `.env`, secrets, credentials | Security | `harness-CLAUDE.md:469` |
| Never use `git add .` or `git add -A` | Could stage secrets or harness files | `skills/implement/SKILL.md:72` |
| Never skip code review or QA | Quality gates are mandatory | `harness-CLAUDE.md:468` |
| Team lead never implements code directly | Must spawn sub-agents for all work | `harness-CLAUDE.md:467` |
| Developer must not install new dependencies | Unless ticket explicitly requires it | `skills/implement/SKILL.md:62` |
| Never let parallel units touch same file | Would cause merge conflicts | `harness-CLAUDE.md:109` |

---

## 9. Escalation Protocol

When any constraint is exhausted, escalation follows a fixed protocol — no exceptions:

1. Log to `pipeline.jsonl`: `{"phase": "<phase>", "event": "Escalated", "reason": "..."}`
2. Write `.harness/logs/escalation.md` with: what failed, attempts made, error details, resume instructions
3. **Always push partial work** — branch + draft PR with `needs-human` label
4. **Stop pipeline** — do not continue to subsequent phases

**Source:** `harness-CLAUDE.md:454-463`

---

## 10. L1 API Retry Policy

| Error Type | Retry? | Backoff | Max Retries | Source |
|-----------|--------|---------|-------------|--------|
| Rate limit (429) | Yes | Exponential (2s, 4s, 8s) | 3 | `analyst.py:220` |
| Connection error | Yes | Exponential | 3 | `analyst.py:235` |
| Server error (5xx) | Yes | Exponential | 3 | `analyst.py:250` |
| Client error (4xx) | **No** | — | — | `analyst.py:251` |

---

## 11. Observability Constraints

| Rule | Detail | Source |
|------|--------|--------|
| Only Team Lead writes `pipeline.jsonl` | Sub-agents write their own span detail files | `harness-CLAUDE.md:426` |
| Timestamps must be real | `date -u +%Y-%m-%dT%H:%M:%SZ` at moment of write | `harness-CLAUDE.md:432-436` |
| Span detail file ownership | Code Reviewer → `code-review.md`, Judge → `judge-verdict.md`, QA → `qa-matrix.md`, Merge → `merge-report.md`, Plan Reviewer → `plan-review.md` | `harness-CLAUDE.md:416-424` |

---

## 12. Graceful Degradation

When optional tools are missing, the pipeline degrades gracefully rather than failing:

| Missing Tool | Behavior | Source |
|-------------|----------|--------|
| Playwright MCP | Mark E2E tests as "Playwright not installed" | `SKILL.md:41` |
| agent-browser | Mark visual checks as "agent-browser not installed" | `SKILL.md:57-58` |
| Test framework | Mark as "No test framework configured" | `SKILL.md:25` |
| Figma API token | Skip design extraction, no design spec in ticket | `figma_extractor.py:78-80` |

---

*Last updated: 2026-03-30*
