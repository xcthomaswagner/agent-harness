# PR-Level Security Review

This complements the per-file security checks done during code review. Here we look at security across the entire PR.

## Checks

### Data Flow
- Sensitive data (tokens, PII, passwords) doesn't cross trust boundaries inappropriately
- Client-side code doesn't receive server-side secrets
- API responses don't leak internal implementation details

### Auth Flow Integrity
- Changes to auth logic maintain the security model
- Protected routes remain protected after the change
- Token handling follows project's auth pattern

### Input Trust Boundaries
- User input validated at the API boundary (not just client-side)
- File uploads have size and type restrictions
- Query parameters and path parameters are sanitized

### Third-Party Risk
- New npm/pip packages: check for known vulnerabilities
- External API calls: auth credentials handled securely
- Webhook endpoints: signature validation present
