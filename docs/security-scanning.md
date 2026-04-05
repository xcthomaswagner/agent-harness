# Security Scanning Architecture

## The Problem

AI-generated code introduces security vulnerabilities at 2.74x the rate of human-written code (Veracode 2025). The harness generates code autonomously — every PR needs security validation beyond what an AI reviewer can catch alone.

## Two-Layer Defense

The harness uses **deterministic tools** for pattern-matchable vulnerabilities and **LLM judgment** for logic-level security. Neither layer alone is sufficient.

```
Implementation
     │
     ▼
┌─────────────────────────────────────────┐
│  Layer 1: Deterministic (Step 3)        │
│  ─────────────────────────────────────  │
│  Dependency Audit  → known CVEs         │
│  Semgrep SAST      → injection, XSS,   │
│                       hardcoded secrets │
│  gitleaks          → committed secrets  │
│                                         │
│  Machine-verified. Zero false negatives │
│  for covered patterns. Bypasses Judge.  │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│  Layer 2: Judgment (Step 4)             │
│  ─────────────────────────────────────  │
│  Code Reviewer   → auth logic, data     │
│                    flow, trust           │
│                    boundaries            │
│  Judge (Opus)    → validates security   │
│                    findings, filters     │
│                    false positives       │
│                                         │
│  LLM-based. Catches logic flaws that    │
│  tools cannot detect. Subject to bias.  │
└─────────────────┬───────────────────────┘
                  │
                  ▼
           Human PR Review
```

## What Each Tool Catches

### Semgrep (SAST — Static Application Security Testing)

Runs `semgrep --config auto` on changed files only. Catches:

| Vulnerability Class | Languages Covered | Example |
|---|---|---|
| SQL Injection | JS/TS, Python, Java, C# | String concat in queries |
| Command Injection | JS/TS, Python | `eval()`, `exec()`, `shell=True` |
| XSS | JS/TS, Python (templates) | Unsanitized user input in HTML |
| Path Traversal | All | `../` in file paths from user input |
| Hardcoded Secrets | All | API keys, passwords in source |
| Insecure Deserialization | Python, Java | `pickle.loads()`, `ObjectInputStream` |
| SSRF | JS/TS, Python | Unvalidated URLs in HTTP requests |
| Open Redirect | JS/TS, Python | Unvalidated redirect targets |

**Does NOT catch:** Auth logic flaws, business logic bugs, race conditions, IDOR, misconfigured CORS/CSP. These require the Code Reviewer (Layer 2).

### gitleaks (Secrets Scanner)

Scans all files for committed secrets using regex + entropy analysis:
- API keys (AWS, GCP, Azure, Anthropic, OpenAI, Stripe, etc.)
- Tokens (JWT, OAuth, GitHub PATs)
- Passwords in config files, `.env` files committed by mistake
- Base64-encoded credentials
- Private keys (RSA, SSH, PGP)

### Dependency Audit

Checks installed packages against CVE databases:

| Package Manager | Tool | What It Catches |
|---|---|---|
| npm | `npm audit` | Known CVEs in npm packages |
| pip | `pip-audit` | Known CVEs in PyPI packages |
| .NET | `dotnet list package --vulnerable` | Known CVEs in NuGet packages |
| Maven | `mvn dependency-check:check` | Known CVEs in Java dependencies |

Also mitigates **package hallucination** (AI suggesting packages that don't exist or are typosquatted) — the audit command fails if the package can't be resolved.

### Code Reviewer (LLM — Judgment)

Reviews for issues tools cannot detect:
- Authentication logic correctness (roles, sessions, tokens)
- Authorization enforcement (server-side vs client-side only)
- Data flow between trust boundaries
- Error handling that leaks internals
- Business logic that could be abused
- Platform-specific security (Apex `with sharing`, FLS checks)

### Judge (LLM — Validation)

Filters Code Reviewer findings to prevent false positives. For security findings:
- Uses **Opus model** (stronger reasoning than Sonnet)
- Lower threshold: **60+** (vs 80+ for non-security findings)
- Evaluates: Is it real? Is the code path reachable? Is the fix correct? Is it pre-existing?

## Composability: Adding a New Language

The security scanning architecture is designed so adding a new language requires **zero pipeline changes**. Each layer handles new languages differently:

### Semgrep (automatic)

Semgrep's `--config auto` flag automatically loads rules for any language it supports. Current coverage:

| Language | Rule Count | Coverage Quality |
|---|---|---|
| JavaScript/TypeScript | 500+ | Excellent |
| Python | 400+ | Excellent |
| Java | 300+ | Good |
| C# | 150+ | Good |
| Go | 200+ | Good |
| Ruby | 100+ | Moderate |
| Rust | 50+ | Basic |
| Swift | 30+ | Basic |
| Kotlin | 100+ | Good |
| Apex (Salesforce) | 20+ (community) | Basic |

**To add a new language (e.g., Rust, Go, Swift):**
1. Semgrep auto-detects the language from file extensions — **nothing to configure**
2. For deeper coverage, add a custom ruleset: `semgrep --config auto --config p/rust-security`
3. Community rulesets: `semgrep.dev/explore` has language-specific packs

**Impact: Zero changes to the pipeline. Semgrep just works.**

### gitleaks (automatic)

Language-agnostic — scans text patterns regardless of file type. No changes needed for any language.

**Impact: Zero.**

### Dependency Audit (one-time addition per package manager)

Each new package manager needs a detection rule and audit command in `harness-CLAUDE.md`:

```bash
# To add Rust/Cargo:
elif [ -f Cargo.toml ]; then
  cargo audit --json 2>/dev/null || true

# To add Go:
elif [ -f go.mod ]; then
  govulncheck ./... 2>/dev/null || true
```

**Impact: ~3 lines added to harness-CLAUDE.md per new package manager.**

### Code Reviewer (automatic with platform profile)

The Code Reviewer reads `SECURITY_CHECKS.md` which has language-specific sections. To add a new language:

1. Add a section to `SECURITY_CHECKS.md`:
   ```markdown
   ### Rust
   - No `unsafe` blocks without justification
   - No `.unwrap()` on user input (use proper error handling)
   - Memory safety: no raw pointer dereference from untrusted data
   ```

2. Or add a platform profile (`runtime/platform-profiles/rust/CODE_REVIEW_SUPPLEMENT.md`) with Rust-specific review checks.

**Impact: ~10 lines in SECURITY_CHECKS.md or a new platform profile supplement.**

### Judge (no changes needed)

The Judge evaluates findings by reading code and applying reasoning. It's language-agnostic — it works on any code it can read.

**Impact: Zero.**

## Summary: Cost of Adding a New Language

| Component | Change Required | Effort |
|---|---|---|
| Semgrep SAST | None (auto-detects) | Zero |
| gitleaks | None (text patterns) | Zero |
| Dependency Audit | Add detection + command | ~3 lines |
| Code Reviewer | Add language section or platform profile | ~10 lines |
| Judge | None | Zero |
| **Total** | | **~13 lines** |

## Graceful Degradation

If security tools are not installed, the pipeline degrades gracefully:

| Tool Missing | Behavior |
|---|---|
| Semgrep not installed | Warning logged, LLM reviewer is sole security gate |
| gitleaks not installed | Warning logged, reviewer checks for hardcoded secrets |
| Audit tool not installed | Warning logged, dependency security unchecked |
| All tools missing | Pipeline proceeds with LLM-only security review |

The harness never blocks on missing tools — it logs warnings so the operator knows the security coverage is reduced.

## What This Does NOT Cover

- **Runtime vulnerabilities** — DAST (Dynamic Application Security Testing) is not in the pipeline. Would require a running server.
- **Infrastructure security** — Dockerfile, Terraform, K8s manifests are partially covered by Semgrep but not deeply.
- **License compliance** — Dependencies are checked for CVEs but not license compatibility.
- **Supply chain attacks** — Package hallucination is partially mitigated by dependency audit, but sophisticated supply chain attacks (compromised maintainer, typosquatting with valid packages) are not detected.
