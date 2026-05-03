# TODO: ContentStack Code Review Supplement — populate after first 2-3 tickets land.

This stub exists so the profile loads cleanly.

Candidate review rules (fill in as evidence accumulates):
- "Never call `cma_delete_entry` without explicit human confirmation"
- "Management tokens, delivery tokens, and stack API keys never appear in PR
  diffs (regex check for `CONTENTSTACK_*_TOKEN` / `CONTENTSTACK_API_KEY`
  values, not just env-var names)"
- "Content-type schema changes require a migration note in the PR description"
- "Schema field additions/removals must be reflected in the typed component
  data shape on the frontend"
- "Branch / environment scope must match the configured client profile —
  reject PRs that touch production env or branches outside the profile"
