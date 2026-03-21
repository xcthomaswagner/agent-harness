# Decomposition Patterns

## By Ticket Type

### Stories (Features)
Decompose by **functional layer**:
1. Data layer (models, schemas, migrations)
2. Business logic (services, utilities)
3. API layer (routes, controllers, endpoints)
4. UI layer (components, pages)
5. Tests for each layer

Dependencies typically flow: data → logic → API → UI.

### Bugs
Decompose by **fix scope**:
1. Root cause fix (the actual code change)
2. Regression test (prevent recurrence)
3. Related fixes (if the bug reveals a pattern)

Most bugs are a single unit. Only decompose if the fix touches multiple independent areas.

### Tasks (Refactoring, Chores)
Decompose by **affected module**:
1. Module A changes + tests
2. Module B changes + tests
3. Integration verification

## Anti-Patterns to Avoid

- **God unit:** One unit that does everything. Break it down.
- **Test-only unit:** Tests should be part of the implementation unit, not separate.
- **Same-file parallel:** Two parallel units touching the same file. Always add a dependency.
- **Premature abstraction:** Don't create units for "extract common utility" unless the ticket asks for it.
- **Over-decomposition:** A single-file bug fix doesn't need 5 units.
