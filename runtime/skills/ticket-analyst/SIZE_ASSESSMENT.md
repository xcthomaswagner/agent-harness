# Size Assessment

## Classification Criteria

Assess the ticket's size based on these heuristics:

### Small (1 dev, 1 unit)
- Single file change or 2-3 closely related files
- One functional area (e.g., one component, one API endpoint, one utility)
- Estimated < 200 lines of new/changed code
- Test scenarios are all unit tests
- Examples: bug fix, add a button, update validation logic, config change

### Medium (2-3 devs, 2-3 units)
- 4-8 files across 2-3 functional areas
- Multiple layers involved (e.g., API + UI, or model + service + controller)
- Estimated 200-500 lines of new/changed code
- Mix of unit and integration tests needed
- Examples: new feature with API + UI, refactor a service with multiple consumers

### Large (4+ devs, 4+ units)
- 8+ files across 4+ functional areas
- Multiple independent work streams
- Estimated 500+ lines of new/changed code
- Requires unit, integration, and potentially e2e tests
- Examples: new page with multiple components + API + data model, major refactor

## Decomposition Trigger

Flag for decomposition (Path C) when:
- **5+ independent implementation units** identified
- **3+ distinct functional areas** that could be worked on in parallel
- **Multiple ticket types embedded** (e.g., "add the feature AND fix the bug AND refactor the old code")
- The ticket description contains phrases like "also," "in addition," "while we're at it"

## Estimation Heuristics

### Counting Independent Units
An implementation unit is independent if:
1. It can be implemented without waiting for another unit
2. It touches distinct files (no shared file modifications)
3. It can be tested in isolation
4. It produces a meaningful, reviewable diff

### File Count Estimation
From the ticket description, estimate affected files by identifying:
- **Data layer**: models, schemas, migrations, types
- **Business logic**: services, utilities, helpers
- **API layer**: routes, controllers, endpoints, resolvers
- **UI layer**: components, pages, layouts, styles
- **Tests**: one test file per implementation file
- **Config**: environment variables, feature flags, build config

### Complexity Multipliers
Increase the size classification by one level if:
- The ticket involves security-sensitive code (auth, payments, PII)
- The ticket requires database migrations
- The ticket touches shared infrastructure (middleware, base classes)
- The ticket involves third-party API integration
- The ticket explicitly mentions backward compatibility

## Output

Include in the size assessment:

```json
{
  "classification": "small|medium|large",
  "estimated_units": 3,
  "recommended_dev_count": 2,
  "decomposition_needed": false,
  "rationale": "Three files across API and UI layers. API endpoint is independent from the React component, allowing parallel development."
}
```

Always include a **rationale** explaining WHY this size was chosen. The rationale helps the plan reviewer validate the assessment.
