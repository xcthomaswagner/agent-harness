# TODO: ContentStack Implement Supplement — populate after first 2-3 tickets land.

This stub exists so the profile loads cleanly. The official `@contentstack/mcp`
carries the API surface (~126 tools across CMA / CDA / Launch / Personalize),
so the agent already knows the shape of every call. Mine real gaps from real
ticket runs rather than pre-authoring rules from docs.

Candidate sections (fill in as evidence accumulates):
- "Use `cma_create_entry` not `cma_publish_entry` when X" — tool-selection
  patterns the agent fumbles between
- "Always specify `locale`" — defaults the agent forgets
- "Prefer modular blocks over JSON RTE for Y" — content-modeling guidance
- "Schema migration discipline" — when adding a field requires a migration
  script vs. when it doesn't
- Branch / alias scoping rules ("never write outside the configured branch")
