# PR Review Output Template

## GitHub PR Review Comment

Post as a single PR review comment:

```markdown
## AI Architecture Review

**Ticket:** {ticket_id} — {ticket_title}
**Verdict:** ✅ Approved | ⚠️ Approved with notes | ❌ Changes requested

### Summary
{1-2 sentence summary of the change and its quality}

### Architecture
{Findings from architecture review, or "No concerns" if clean}

### Security
{Findings from security review, or "No concerns" if clean}

### Naming & Consistency
{Any cross-file naming or consistency issues, or "Consistent" if clean}

### Test Coverage
{Assessment of test coverage completeness}

### Inline Comments
{N inline comments posted on specific lines}
```

## Inline Comments

For specific issues, post inline review comments on the affected lines:

```
File: src/lib/auth.ts, Line 42
Category: security
Severity: critical
Comment: Token is logged at debug level — this could leak credentials to log aggregators.
Suggestion: Remove the debug log or redact the token value.
```

## Verdicts

| Verdict | When to Use |
|---------|-------------|
| Approved | No issues found, or only minor non-blocking suggestions |
| Approved with notes | No blocking issues, but notable observations for the human reviewer |
| Changes requested | Critical issues that must be fixed before merge |
