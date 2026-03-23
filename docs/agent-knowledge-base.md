# Agent Knowledge Base

## Overview

Client tribal knowledge — architecture decisions, coding patterns, business rules, past incidents — can be provided to agents as a structured directory of markdown files. This gives agents domain context that goes beyond what's in the codebase and CLAUDE.md.

## Directory Structure

```
runtime/client-knowledge/<client-name>/
├── KNOWLEDGE_INDEX.md          # Table of contents — what's here and when to read it
├── architecture/
│   ├── system-overview.md      # How the system is structured
│   ├── adr-001-auth-pattern.md # Architecture decision records
│   ├── adr-002-caching.md
│   └── api-contracts.md        # API specs, endpoint documentation
├── patterns/
│   ├── coding-patterns.md      # "We always do X this way because..."
│   ├── anti-patterns.md        # "Never do Y because of incident Z"
│   └── design-system.md        # Component library conventions, tokens, usage
├── business/
│   ├── domain-glossary.md      # Business terms and their meaning in this context
│   ├── business-rules.md       # "Discounts can never exceed 30%", "Orders over $10K need approval"
│   └── compliance.md           # Regulatory constraints, data handling rules, PII policies
└── history/
    ├── past-incidents.md       # Things that went wrong and why — prevents repeating mistakes
    └── review-feedback.md      # Recurring PR review themes — "reviewers always flag X"
```

## KNOWLEDGE_INDEX.md

Every knowledge base must have an index file that tells agents what's available and when to consult each section:

```markdown
# Knowledge Base — <Client Name>

## When to Read What

| Situation | Read |
|-----------|------|
| Planning any feature | architecture/system-overview.md |
| Working with auth or sessions | architecture/adr-001-auth-pattern.md |
| Building UI components | patterns/design-system.md |
| Writing any business logic | business/business-rules.md |
| Handling user data or PII | business/compliance.md |
| Before any code review | patterns/anti-patterns.md, history/review-feedback.md |
| Unfamiliar domain term | business/domain-glossary.md |

## Contents
- architecture/ — system structure, ADRs, API contracts
- patterns/ — how we code here, what to avoid
- business/ — domain knowledge, rules, compliance
- history/ — past incidents, recurring review feedback
```

## How Agents Access It

1. `inject_runtime.py` copies the client's knowledge directory into the worktree at `.claude/knowledge/`
2. Each agent prompt includes: "Before starting, check `.claude/knowledge/KNOWLEDGE_INDEX.md` for relevant domain knowledge, patterns, and constraints."
3. Agents read the index, then pull in specific files relevant to their task
4. Agents don't read everything — the index directs them to what matters

## Which Agents Use It

| Agent | What They Look For |
|-------|--------------------|
| **L1 Analyst** | Business rules, domain glossary — to write better acceptance criteria |
| **Planner** | Architecture overview, system structure — to decompose correctly |
| **Developer** | Coding patterns, design system, API contracts — to implement consistently |
| **Code Reviewer** | Anti-patterns, past incidents, review feedback — to catch known issues |
| **QA** | Business rules, compliance — to validate against domain constraints |

## Source Document Conversion

### Markdown files
Drop directly into the knowledge directory. No conversion needed.

### Word documents (.docx)
Convert using pandoc:
```bash
pandoc document.docx -o document.md --wrap=none
```

Batch convert all docx files in a folder:
```bash
for f in *.docx; do pandoc "$f" -o "${f%.docx}.md" --wrap=none; done
```

### Confluence pages
Option 1 — Export as markdown:
- Confluence → Page → Export → Markdown

Option 2 — Use the Confluence API:
```bash
# Fetch page content as storage format, convert to markdown
curl -u user:token "https://site.atlassian.net/wiki/rest/api/content/{id}?expand=body.storage" \
  | jq -r '.body.storage.value' \
  | pandoc -f html -t markdown -o page.md
```

Option 3 — Use the Obsidian MCP or Confluence MCP if available in your environment.

### SharePoint documents
Option 1 — Download as .docx, then convert with pandoc (see above).

Option 2 — Use the Outlook MCP's SharePoint tools to fetch documents programmatically:
```
mcp__outlook-mcp__outlook_get_sharepoint_file
mcp__outlook-mcp__outlook_list_sharepoint_files
```

### PDF files
```bash
pandoc document.pdf -o document.md
# or for better results:
pip install marker-pdf
marker document.pdf document.md
```

## Sizing Guidelines

| Size | Documents | Approach |
|------|-----------|----------|
| Small (< 20 files) | ADRs, patterns, key rules | Direct file access — agents read what they need |
| Medium (20-50 files) | Full wiki section, design system docs | Direct file access with a well-organized index |
| Large (50-100 files) | Multiple wiki spaces, extensive history | Consider splitting into "essential" (always injected) and "reference" (read on demand) |
| Very large (100+ files) | Entire knowledge base, all documentation | Consider RAG with a vector database for semantic search |

For most client engagements, 10-30 markdown files in a structured directory is sufficient. The agents' 1M context window can handle substantial knowledge alongside the codebase.

## Why Not RAG

RAG (Retrieval Augmented Generation) adds infrastructure: a vector database, an embedding model, a retrieval pipeline, and chunk management. For static historical knowledge:

- **Direct file access is simpler** — no infrastructure to deploy or maintain
- **Agents can read files natively** — they already use Read/Glob/Grep tools
- **The index file acts as a lightweight retrieval system** — agents know what to look for
- **Full document context is preserved** — no chunking artifacts or missed context

RAG makes sense when:
- Knowledge exceeds what fits in a file tree (hundreds of documents)
- Knowledge changes frequently and needs real-time retrieval
- Agents need semantic search ("find anything related to payment processing")
- Multiple unrelated knowledge sources need to be unified

## Client Profile Integration

The client profile YAML (`runtime/client-profiles/<client>.yaml`) should reference the knowledge base:

```yaml
client: "Acme Corp"
knowledge_base: "acme"  # maps to runtime/client-knowledge/acme/
```

The inject script uses this to copy the correct knowledge base into each worktree.

## Maintenance

- **Review quarterly** — remove outdated documents, update patterns that have evolved
- **Add post-incident** — after any production incident, add to `history/past-incidents.md`
- **Add post-review** — when human reviewers consistently flag the same issue, add to `history/review-feedback.md`
- **Version control** — the knowledge base lives in the harness repo and is versioned with git
