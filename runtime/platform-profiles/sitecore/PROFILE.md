# Sitecore Platform Profile

## Activation Rules

This profile activates when any of these are detected:
- `sitecore.json` exists in the repo root
- `.sln` file references Sitecore assemblies
- `package.json` contains `@sitecore-jss/*` dependencies
- Client Profile explicitly specifies `platform_profile: sitecore`

## Profile Contents

| File | Injected Into |
|------|--------------|
| `IMPLEMENT_SUPPLEMENT.md` | `/implement` skill |
| `CODE_REVIEW_SUPPLEMENT.md` | `/code-review` skill |
| `QA_SUPPLEMENT.md` | `/qa-validation` skill |
| `CONVENTIONS.md` | Copied to `/implement/CONVENTIONS.md` |

## Sitecore Versions Covered

- Sitecore XM Cloud (Next.js + JSS)
- Sitecore XP/XM with Headless SDK
- Sitecore with .NET + React hybrid architectures
