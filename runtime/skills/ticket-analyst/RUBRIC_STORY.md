# Story Completeness Rubric

Evaluate each criterion as **present**, **partial**, or **missing**.

## Required Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 1 | **User Role** | Who is the user? ("As a [role]...") Explicit or clearly inferable from context. |
| 2 | **Desired Action** | What does the user want to do? Clear description of the feature or behavior. |
| 3 | **Business Value** | Why does the user want this? The benefit or outcome. Can be implicit if the value is obvious. |
| 4 | **Acceptance Criteria** | At least 2 testable, specific criteria that define "done." |
| 5 | **Scope Boundary** | Clear indication of what is in scope and what is NOT. Implicit scope is acceptable if the story is focused. |

## Recommended Criteria

| # | Criterion | Description |
|---|-----------|-------------|
| 6 | **UI Description or Mockup** | For UI stories: wireframe, mockup, Figma link, or textual description of the interface. |
| 7 | **Data Requirements** | What data is needed? New fields, API contracts, data sources. |
| 8 | **Error Handling** | How should errors be presented to the user? |
| 9 | **Non-functional Requirements** | Performance expectations, accessibility needs, browser/device support. |

## Decision Logic

- **All 5 required criteria present** → Path A (enrich and proceed)
- **3-4 required criteria present, gaps are inferable** → Path A (enrich, note inferences in analyst_notes)
- **Criteria 1-3 present but no acceptance criteria (criterion 4)** → Path A (you will generate the AC)
- **Criteria 2 missing (what to do is unclear)** → Path B (info request)
- **Multiple required criteria missing and not inferable** → Path B (info request)
