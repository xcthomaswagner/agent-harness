# Style Guide

## Core Principle

**Match the project's existing style.** The project's CLAUDE.md and existing code are the authority. This guide covers universal checks.

## Universal Checks

### Naming
- Consistent with the codebase (camelCase, snake_case, PascalCase, etc.)
- Descriptive names that reveal intent
- No single-letter variables (except loop counters `i`, `j`, `k`)
- No abbreviations unless they're standard in the project

### Formatting
- Consistent indentation (tabs or spaces — match the project)
- Line length within project limits
- Consistent brace style
- No trailing whitespace

### Code Quality
- No dead code (commented-out blocks, unused variables/imports)
- No debugging artifacts (`console.log`, `print`, `debugger`, `TODO` that should have been resolved)
- No duplicate code that should be extracted
- Error messages are helpful and user-facing where appropriate

### Imports
- Ordered per project conventions
- No unused imports
- No circular imports

### Comments
- Only where the code isn't self-explanatory
- No "obvious" comments (e.g., `// increment i` above `i++`)
- JSDoc/docstrings on public APIs where the project uses them
