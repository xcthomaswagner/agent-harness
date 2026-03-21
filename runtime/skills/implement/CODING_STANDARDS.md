# Coding Standards

## Core Principle

**Follow existing patterns.** The codebase you're working in has established conventions. Match them exactly rather than imposing different standards.

## How to Discover Conventions

1. Read the project's CLAUDE.md — it defines the authoritative standards
2. Find 3 files similar to what you're building and study them
3. When in doubt, match the most common pattern you see

## Universal Guidelines

These apply regardless of language or framework:

- **Naming:** Match the project's convention (camelCase, snake_case, PascalCase, etc.)
- **File organization:** Put new files where similar files already live
- **Error handling:** Copy the project's error handling pattern
- **Imports:** Follow the import ordering the project uses
- **Comments:** Only where the code isn't self-explanatory. No "obvious" comments
- **No dead code:** Don't leave commented-out code or unused variables
- **No debugging artifacts:** Remove all console.log, print, debugger before commit
