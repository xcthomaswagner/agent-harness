# QA Matrix Output Format

The QA validator writes a structured Markdown report to `.harness/logs/qa-matrix.md`.

## Template

```markdown
## QA Matrix — <ticket-id>
### Overall: PASS | FAIL
### Acceptance Criteria
| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | <criterion text> | PASS | <test name, output, or screenshot path> |
| 2 | <criterion text> | FAIL | <error message and failing test> |
| 3 | <criterion text> | NOT_TESTED | <reason why not testable> |

### Edge Cases
| Case | Status | Notes |
|------|--------|-------|
| <edge case description> | COVERED | <test name> |
| <edge case description> | NOT_COVERED | <reason> |

### E2E Visual Validation (if performed)
| Page/Component | Screenshot | Status | Notes |
|---------------|-----------|--------|-------|
| /profile | .harness/screenshots/profile.png | PASS | Layout matches |

### Figma Design Compliance (if design spec present)
| Check | Expected (Figma) | Actual (Rendered) | Status | Evidence |
|-------|-----------------|-------------------|--------|----------|
| Pixel diff | Matches baseline | 2.1% deviation | PASS | [diff](.harness/screenshots/design-diff.png) |
| Primary color | #1B2A4A | #1B2A4A | PASS | `agent-browser get styles` |
| Heading font | Inter 24px Bold | Inter 24px 700 | PASS | `agent-browser get styles` |
| Component: Button | Present | Found (role=button) | PASS | `agent-browser snapshot` |
| Responsive: 375px | Stacked layout | flex-direction: column | PASS | [screenshot](.harness/screenshots/responsive-375.png) |

### Test Results
Unit/Integration: X passed, Y failed
E2E: X passed, Y failed (or "skipped — no Playwright")
Design Compliance: X/Y checks passed (or "Skipped — no Figma design spec provided in ticket")
```

## Rules

- Every acceptance criterion (original + generated) MUST appear in the matrix
- Every edge case from the enriched ticket MUST appear
- `NOT_TESTED` is acceptable only with a specific reason (not a blanket excuse)
- Screenshots are required for all e2e test evidence
- Overall is `PASS` only when ALL acceptance criteria pass
- Overall is `FAIL` if any acceptance criterion fails (edge cases and design compliance don't affect overall)

## JSON Sidecar

In addition to qa-matrix.md, use the Write tool to create
`.harness/logs/qa-matrix.json` matching this shape exactly:

```json
{
  "overall": "PASS" | "FAIL",
  "issues": [
    {
      "id": "qa-1",
      "severity": "critical" | "warning",
      "category": "acceptance_criterion" | "edge_case" | "e2e" | "design_compliance",
      "file_path": "",
      "line_start": 0,
      "line_end": 0,
      "summary": "One-line description of the failing check",
      "details": "Longer explanation — test name, error output, or observed vs expected",
      "acceptance_criterion_ref": "AC-2"
    }
  ]
}
```

ID convention: `qa-1`, `qa-2`, ... in the order failing checks appear
in the Markdown matrix. Do not reuse or renumber ids between runs.

Only include failing / NOT_TESTED checks in `issues`. Passing checks
do not appear.

`acceptance_criterion_ref` required when the issue traces to a specific
AC; otherwise the empty string `""`.

Empty case: `{"overall": "PASS", "issues": []}` — never omit the
issues key.
