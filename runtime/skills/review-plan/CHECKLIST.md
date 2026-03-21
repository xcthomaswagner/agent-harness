# Plan Review Checklist

## Mandatory Checks (must all pass)

- [ ] **Schema valid:** All required fields present, valid JSON
- [ ] **No cycles:** Dependency graph is a valid DAG
- [ ] **No parallel conflicts:** No shared files between unrelated parallel units
- [ ] **AC coverage:** Every acceptance criterion has at least one unit covering it
- [ ] **Test coverage:** Every test scenario from the enriched ticket is assigned to a unit
- [ ] **Unit descriptions are actionable:** A developer can implement from the description alone

## Quality Checks (warn if failed, don't block)

- [ ] **Reasonable sizing:** Complexity ratings match the actual scope
- [ ] **Architecture fit:** Plan follows existing codebase patterns (per CLAUDE.md and code inspection)
- [ ] **Edge cases addressed:** Enriched ticket's edge cases appear in test criteria
- [ ] **No over-decomposition:** Simple work isn't split into unnecessary units
- [ ] **Integration point exists:** There's a unit or step that wires everything together
