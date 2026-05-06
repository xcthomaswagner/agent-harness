# Risk Challenge Skill

Use this skill when a high-risk implementation plan needs adversarial review before implementation starts.

## Purpose

The Challenger tests whether the plan is safe, complete, and aligned with the repo before developers spend cycles on it. This is a targeted challenge gate, not a debate for every ticket.

## High-Risk Triggers

Run the Challenger when any of these are true:

- The ticket is full-pipeline and estimated at 3 or more units.
- The plan changes auth, permissions, payment, pricing, personally identifiable data, migrations, schema, CMS content model, production config, shared APIs, or integration contracts.
- The platform profile is Salesforce or ContentStack and the plan touches metadata, schema, permissions, live CMS data, or deployment configuration.
- The plan touches SAP, Oracle, ERP, CRM, middleware, or other external system contracts.
- The plan creates a new abstraction where the repo already has a similar one.
- The plan modifies more than five source files or changes both backend/API and frontend/UI surfaces.
- The ticket, analyst notes, or repo `WORKFLOW.md` explicitly calls out risk.

## Process

1. Read `.harness/ticket.json`.
2. Read the highest-numbered `.harness/plans/plan-v<N>.json`.
3. Read `CLAUDE.md` and `.harness/repo-workflow.md` if present.
4. Inspect existing files named in `affected_files` and nearby patterns.
5. Identify concrete objections only when backed by evidence.
6. Write `.harness/logs/risk-challenge.md`.
7. Write `.harness/logs/risk-challenge.json`.

## Output Contract

The JSON sidecar is authoritative:

```json
{
  "risk_level": "low|medium|high",
  "blocking": true,
  "summary": "One-paragraph assessment.",
  "objections": [
    {
      "id": "risk-1",
      "area": "architecture|security|data|integration|test|operations|scope",
      "severity": "blocking|warning",
      "concern": "What could go wrong.",
      "evidence": "Specific file path, ticket AC, command output, or convention.",
      "recommended_change": "Specific plan change."
    }
  ],
  "alternate_plan_summary": "",
  "requires_plan_revision": true
}
```

## Review Standard

- Prefer fewer, better objections.
- Do not block on style preference.
- Do not invent root causes.
- Do not request implementation details that belong to the Developer unless they affect plan safety.
- If an objection is only a hypothesis, state what evidence would confirm it.

## Team Lead Follow-Up

If `requires_plan_revision` is true, route the challenge to the Plan Reviewer. The Plan Reviewer should either write `plan-v<N+1>.json` or explicitly reject the objection with evidence in `.harness/logs/plan-review.json`.
