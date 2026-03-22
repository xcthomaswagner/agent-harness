# Platform Profile Authoring Guide

## What is a Platform Profile?

A platform profile injects platform-specific knowledge (Sitecore, Salesforce, etc.) into the harness skills without modifying the base skill files. Each profile is a directory of supplement files.

## Creating a New Profile

### 1. Create the Directory

```bash
mkdir -p runtime/platform-profiles/<platform-name>/
```

### 2. Create PROFILE.md

Define activation rules — how the system detects this platform:

```markdown
# <Platform> Platform Profile

## Activation Rules

This profile activates when any of these are detected:
- `<config-file>` exists in the repo root
- `package.json` contains `<platform-package>` dependency
- Client Profile specifies `platform_profile: <name>`

## Profile Contents

| File | Injected Into |
|------|--------------|
| `IMPLEMENT_SUPPLEMENT.md` | `/implement` skill |
| `CODE_REVIEW_SUPPLEMENT.md` | `/code-review` skill |
| `QA_SUPPLEMENT.md` | `/qa-validation` skill |
| `CONVENTIONS.md` | Copied to `/implement/CONVENTIONS.md` |
```

### 3. Create Supplement Files

#### IMPLEMENT_SUPPLEMENT.md
Platform-specific implementation patterns:
- Component/class structure and conventions
- Data access patterns
- Framework-specific APIs
- Common gotchas and workarounds

#### CODE_REVIEW_SUPPLEMENT.md
Platform-specific review checks:
- Security rules specific to the platform
- Performance anti-patterns
- Architecture compliance checks
- Common mistakes

#### QA_SUPPLEMENT.md
Platform-specific testing guidance:
- Test framework and patterns
- Mock/fixture patterns for platform APIs
- What to test vs what to skip
- Test data setup

#### CONVENTIONS.md
Platform-specific naming and structure:
- File organization
- Naming conventions
- Code style examples
- CLI commands

## Existing Profiles

### Sitecore (`runtime/platform-profiles/sitecore/`)
- JSS / Next.js component patterns
- Experience Editor compatibility
- Helix architecture compliance
- SCS / Unicorn serialization

### Salesforce (`runtime/platform-profiles/salesforce/`)
- Apex patterns and governor limits
- LWC component structure
- SOQL security and FLS/CRUD
- B2B Commerce and Agentforce metadata

## Testing a Profile

1. Inject with the profile flag:
   ```bash
   ./scripts/inject-runtime.sh --target-dir <repo> --platform-profile <name>
   ```

2. Verify the supplement was appended:
   ```bash
   tail -20 <repo>/.claude/skills/implement/SKILL.md
   # Should show "Platform Supplement: <name>" section
   ```

3. Test with a platform-specific ticket to verify the agent follows platform conventions.
