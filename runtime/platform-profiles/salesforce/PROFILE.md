# Salesforce Platform Profile

## Activation Rules

This profile activates when any of these are detected:
- `sfdx-project.json` exists in the repo root
- `force-app/` directory exists
- Client Profile explicitly specifies `platform_profile: salesforce`

## Profile Contents

| File | Injected Into |
|------|--------------|
| `IMPLEMENT_SUPPLEMENT.md` | `/implement` skill |
| `CODE_REVIEW_SUPPLEMENT.md` | `/code-review` skill |
| `QA_SUPPLEMENT.md` | `/qa-validation` skill |
| `CONVENTIONS.md` | Copied to `/implement/CONVENTIONS.md` |

## Salesforce Technologies Covered

- Apex (classes, triggers, batch, queueable)
- Lightning Web Components (LWC)
- Aura Components (legacy)
- SOQL / SOSL queries
- Salesforce DX / CLI
- B2B Commerce (Lightning)
- Agentforce / GenAI metadata
