# Security Review Checklist

## Critical (block on failure)

- [ ] **No hardcoded secrets**: No API keys, tokens, passwords, or credentials in code
- [ ] **No SQL injection**: Parameterized queries only, no string concatenation for queries
- [ ] **No command injection**: No `eval()`, `exec()`, unsanitized `subprocess` with `shell=True`
- [ ] **No XSS vectors**: User input is escaped/sanitized before rendering in HTML
- [ ] **Auth checks present**: Protected endpoints verify authentication and authorization
- [ ] **No sensitive data in logs**: Tokens, passwords, PII not logged

## Important (warn, don't block)

- [ ] **Input validation**: User inputs validated at system boundaries (API endpoints, form handlers)
- [ ] **Error messages safe**: Error responses don't leak internal details (stack traces, file paths)
- [ ] **CORS configured**: If adding endpoints, CORS is appropriately scoped
- [ ] **Rate limiting**: Public endpoints have rate limiting (or note if handled elsewhere)
- [ ] **Dependencies**: New dependencies are from reputable sources, no known CVEs

## Language-Specific

### JavaScript/TypeScript
- No `dangerouslySetInnerHTML` without sanitization
- No `eval()` or `new Function()`
- `innerHTML` assignments use sanitized content
- `target="_blank"` links include `rel="noopener noreferrer"`

### Python
- No `pickle.loads()` on untrusted data
- No `yaml.load()` without `Loader=SafeLoader`
- No `os.system()` or `subprocess.call()` with `shell=True` and user input
- SQL queries use parameterized statements

### Apex (Salesforce)
- `with sharing` keyword present on classes accessing records
- SOQL queries use bind variables, not string concatenation
- FLS/CRUD checks before DML operations
