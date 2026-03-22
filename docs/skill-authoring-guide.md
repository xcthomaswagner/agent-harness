# Skill Authoring Guide

## What is a Skill?

A skill is a collection of markdown files that provide domain-specific knowledge to an agent. Skills are loaded into the agent's context at runtime, giving it expertise for a specific task.

## Skill Structure

```
runtime/skills/<skill-name>/
├── SKILL.md           # Main entry point — role, workflow, output format
├── SUPPORTING.md      # Additional reference files (checklists, templates, etc.)
├── TEMPLATES/         # Output templates
│   └── template.md
└── scripts/           # Helper scripts (optional)
    └── helper.sh
```

## Writing SKILL.md

Every skill needs a `SKILL.md` as its entry point. Structure it as:

### 1. Role Definition
```markdown
## Role
You are a **[Role Name]** — you [what you do] for [what purpose].
```

### 2. Inputs
What the agent receives and where to find it.

### 3. Process
Step-by-step workflow. Be specific and ordered.

### 4. Output Format
Exact JSON/markdown schema the agent should produce. Include examples.

### 5. Quality Guidelines
What makes good vs bad output. Include examples of both.

### 6. Failure Handling
What to do when things go wrong. Max retries, escalation paths.

## Best Practices

### Be Specific, Not Generic
```markdown
# BAD
"Review the code for issues"

# GOOD
"Check for SOQL injection: all queries must use bind variables (:var),
never string concatenation. Flag any instance of 'SELECT ... WHERE ' + variable"
```

### Include Examples
Show the agent what good output looks like. One example is worth a page of instructions.

### Use Checklists
Agents follow checklists reliably. Put mandatory checks in a checklist format:
```markdown
- [ ] All new functions have tests
- [ ] No hardcoded secrets
- [ ] Error messages are user-friendly
```

### Keep Files Focused
Each supporting file should cover one topic. Don't put security checks and style guidelines in the same file — the agent can reference the right file for the right situation.

### Test Your Skills
1. Inject the skill into a test repo: `./scripts/inject-runtime.sh --target-dir <repo>`
2. Start an interactive Claude Code session
3. Ask the agent to use the skill: "Use the /implement skill to add a feature"
4. Observe how it interprets your instructions
5. Iterate on wording until behavior is consistent

## Platform Profile Supplements

To add platform-specific knowledge to an existing skill without modifying the base:

1. Create a supplement file: `platform-profiles/<platform>/IMPLEMENT_SUPPLEMENT.md`
2. The inject script appends it to the base skill
3. Name supplements to match their target skill:
   - `IMPLEMENT_SUPPLEMENT.md` → appended to `/implement/SKILL.md`
   - `CODE_REVIEW_SUPPLEMENT.md` → appended to `/code-review/SKILL.md`
   - `QA_SUPPLEMENT.md` → appended to `/qa-validation/SKILL.md`

## Existing Skills Reference

| Skill | Role | Key Files |
|-------|------|-----------|
| `/ticket-analyst` | Evaluates and enriches tickets | SKILL.md, 3 rubrics, 5 templates |
| `/plan-implementation` | Decomposes tickets into plans | SKILL.md, PLAN_SCHEMA.md, examples |
| `/review-plan` | Reviews plans for correctness | SKILL.md, ANTIPATTERNS.md, CHECKLIST.md |
| `/implement` | Implements code changes | SKILL.md, CODING_STANDARDS.md, TEST_PATTERNS.md |
| `/code-review` | Reviews code diffs | SKILL.md, SECURITY_CHECKS.md, REVIEW_FORMAT.md |
| `/qa-validation` | Validates against acceptance criteria | SKILL.md, 4 validation guides, QA_MATRIX_TEMPLATE.md |
| `/pr-review` | Reviews PRs for architecture | SKILL.md, ARCHITECTURE_REVIEW.md, REVIEW_TEMPLATE.md |
