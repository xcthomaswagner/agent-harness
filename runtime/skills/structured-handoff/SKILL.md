# Structured Handoff Skill

Use this skill whenever one harness teammate hands work to another teammate or back to the Team Lead.

## Rule

Chat summaries are not authoritative. Every handoff must be backed by a canonical file under `.harness/`.

## Canonical Artifacts

| Handoff | Author | Required File |
|---------|--------|---------------|
| Plan | Planner | `.harness/plans/plan-v<N>.json` |
| Risk challenge | Challenger | `.harness/logs/risk-challenge.json` |
| Plan review | Plan Reviewer | `.harness/logs/plan-review.md` and `.harness/logs/plan-review.json` |
| Plan decision | Team Lead | `.harness/logs/plan-decision.md` and `.harness/logs/plan-decision.json` when risk challenge ran |
| Implementation result | Developer | `.harness/logs/implementation-result-<unit_id>.json` |
| Merge result | Merge Coordinator | `.harness/logs/merge-report.md` and `.harness/logs/merge-report.json` |
| Code review | Code Reviewer | `.harness/logs/code-review.md` and `.harness/logs/code-review.json` |
| Judge verdict | Judge | `.harness/logs/judge-verdict.md` and `.harness/logs/judge-verdict.json` |
| QA verdict | QA | `.harness/logs/qa-matrix.md` and `.harness/logs/qa-matrix.json` |
| Reflection | Run Reflector | `.harness/logs/retrospective.md` and `.harness/logs/retrospective.json` |

## Team Lead Responsibilities

- Read the canonical file before routing the next phase.
- If a required artifact is missing or invalid, re-prompt that teammate once.
- If the artifact is still missing or invalid, escalate rather than routing from memory.
- Log only summaries to `pipeline.jsonl`; sub-agents never write that file.

## Sub-Agent Responsibilities

- Write the required artifact before reporting completion.
- Keep JSON valid and concise.
- Use stable IDs (`risk-1`, `cr-1`, `qa-1`, etc.) so later phases can reference the same finding.
- Do not overwrite another role's artifact except where the runtime instructions explicitly say to write the next version.
