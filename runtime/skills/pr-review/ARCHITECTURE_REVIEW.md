# Architecture Review

## Checks

### Pattern Consistency
- Does the change follow existing patterns or introduce new ones?
- If new patterns: are they justified? Do they conflict with established ones?
- Are there similar features in the codebase that were implemented differently? (Inconsistency risk)

### Separation of Concerns
- Business logic in the right layer (not in UI components or API routes)
- Data access through the established ORM/query layer (not direct DB calls from unexpected places)
- Configuration in the right place (not hardcoded)

### API Contracts
- Request/response schemas documented or type-safe
- Error responses follow the project's standard format
- Breaking changes flagged (if modifying existing endpoints)

### Dependencies
- New dependencies are justified (can't be done with existing libraries?)
- Dependency is well-maintained and has appropriate license
- No duplicate functionality with existing dependencies

### Cross-Cutting Concerns
- Logging added where needed
- Error handling consistent with project patterns
- Performance implications considered (N+1 queries, unnecessary re-renders, large payloads)
