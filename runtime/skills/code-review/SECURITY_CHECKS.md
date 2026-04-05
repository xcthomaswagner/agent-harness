# Security Review Checklist

## How Security Scanning Works

The harness uses **two layers** of security checking:

1. **Deterministic (Step 3 — before you see the code):** Semgrep SAST, gitleaks secrets scanner, and dependency audit run automatically. These catch pattern-matchable vulnerabilities (injection, XSS, hardcoded secrets, known CVEs) with zero false negatives for covered patterns. Findings bypass the Judge and go straight to the developer.

2. **Judgment-based (this checklist — your job):** You review for logic-level security that tools cannot catch: auth flow integrity, data flow safety, trust boundary violations, business logic flaws. Focus your review here — don't duplicate what Semgrep already checked.

**If Semgrep ran:** Focus on the "Judgment Required" section below. The "Tool-Covered" items were already checked.

**If Semgrep was NOT available:** Review everything — you are the only security gate.

---

## Tool-Covered (Semgrep + gitleaks handle these — verify only if tools were unavailable)

- [ ] **No hardcoded secrets**: API keys, tokens, passwords, credentials in code
- [ ] **No SQL injection**: String concatenation in queries instead of parameterized
- [ ] **No command injection**: `eval()`, `exec()`, unsanitized `subprocess` with `shell=True`
- [ ] **No XSS vectors**: Unsanitized user input rendered in HTML
- [ ] **No sensitive data in logs**: Tokens, passwords, PII in log statements
- [ ] **No `dangerouslySetInnerHTML`** without sanitization (JS/TS)
- [ ] **No `pickle.loads()`** on untrusted data (Python)
- [ ] **No `yaml.load()`** without SafeLoader (Python)

## Judgment Required (tools cannot catch these — always review)

- [ ] **Auth checks present**: Protected endpoints verify authentication AND authorization
- [ ] **Auth logic correct**: Role checks aren't bypassable, session handling is sound
- [ ] **Input validation at boundaries**: User inputs validated at API endpoints, form handlers
- [ ] **Error messages safe**: Error responses don't leak internals (stack traces, file paths, SQL)
- [ ] **Data flow safe**: Sensitive data doesn't flow to untrusted outputs (logs, error pages, client)
- [ ] **CORS scoped**: If adding endpoints, CORS isn't `*` for authenticated routes
- [ ] **Rate limiting**: Public endpoints have rate limiting (or handled at infrastructure level)
- [ ] **Trust boundaries respected**: Client-side checks backed by server-side enforcement

## Language-Specific (Judgment — Semgrep covers syntax patterns but not logic)

### JavaScript/TypeScript
- `target="_blank"` links include `rel="noopener noreferrer"`
- Prototype pollution risk in deep merge/assign operations
- Client-side auth checks have server-side enforcement

### Python
- `os.system()` / `subprocess` with user-controlled arguments
- Deserialization of untrusted data (beyond pickle — json.loads of user input used in eval)

### Apex (Salesforce)
- `with sharing` keyword present on classes accessing records
- SOQL queries use bind variables, not string concatenation
- FLS/CRUD checks before DML operations (`WITH SECURITY_ENFORCED`)
- `@AuraEnabled` methods validate input before processing

### C# / .NET
- Anti-forgery tokens on form submissions
- `[Authorize]` attribute on protected controllers/actions
- No raw SQL via `ExecuteSqlRaw` with user input
