# Plan Anti-Patterns

Watch for these common planning mistakes:

## 1. Same-File Parallel Conflict
**Symptom:** Two units with no dependency between them both list the same file in `affected_files`.
**Fix:** Add a dependency between the units, or restructure so each unit touches distinct files.

## 2. Missing Test Coverage
**Symptom:** An acceptance criterion from the enriched ticket has no corresponding test in any unit.
**Fix:** Add the test to the most relevant unit's `test_criteria`.

## 3. Circular Dependencies
**Symptom:** unit-1 depends on unit-2, and unit-2 depends on unit-1.
**Fix:** Restructure units to break the cycle. Often means merging two units.

## 4. God Unit
**Symptom:** One unit touches 10+ files and has "moderate" or "complex" complexity while all other units are trivial.
**Fix:** Break the large unit into smaller units with clear boundaries.

## 5. Test-Only Unit
**Symptom:** A unit that only writes tests without implementing anything.
**Fix:** Tests should be part of the implementation unit, not separate.

## 6. Missing Integration Points
**Symptom:** Units are decomposed correctly but there's no unit that wires them together (e.g., no unit adds the route to the router, or no unit imports the new component into the page).
**Fix:** Either add an integration unit or include the wiring in the last dependent unit.

## 7. Over-Decomposition
**Symptom:** A simple bug fix decomposed into 5 units.
**Fix:** Small changes should be 1 unit. Only decompose when there are genuinely independent work streams.

## 8. Reverse Dependencies
**Symptom:** UI unit has no dependency on the API unit it consumes. Plan says they can run in parallel.
**Fix:** The UI unit should depend on the API unit (API must exist before UI can integrate with it).
