# Task Completeness Rubric

Evaluate each criterion as **present**, **partial**, or **missing**.

## Required Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 1 | **Scope Statement** | What needs to be done. Clear description of the work. |
| 2 | **Definition of Done** | How to verify the task is complete. Acceptance criteria or checklist. |

## Recommended Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 3 | **Technical Approach** | Hints about how to implement (e.g., "use the existing auth middleware"). |
| 4 | **Affected Areas** | Which parts of the codebase, services, or systems are impacted. |
| 5 | **Dependencies** | Other tickets, services, or approvals needed before or after. |

## Decision Logic

- **Criteria 1-2 present** → Path A (enrich and proceed)
- **Criterion 1 present, criterion 2 missing** → Path A (you will generate the definition of done)
- **Criterion 1 missing or extremely vague** → Path B (info request — "What specifically needs to be done?")
