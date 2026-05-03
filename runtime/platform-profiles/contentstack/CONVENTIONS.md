# TODO: ContentStack Conventions — populate after first 2-3 tickets land.

This stub exists so the profile loads cleanly. Do not pre-write rules from
ContentStack docs — wait until real ticket runs surface real gaps, then mine
those into rules. See `runtime/platform-profiles/sitecore/CONVENTIONS.md` and
`runtime/platform-profiles/salesforce/CONVENTIONS.md` for eventual targets.

Candidate sections (fill in as evidence accumulates):
- File organization (where modular blocks, page resolvers, and content-type
  schemas live in the client repo)
- Naming conventions (block UIDs, content-type display names, field naming)
- Branch / environment scoping discipline (`ai` branch isolation, never
  publish to production from harness)
- Modular blocks vs. JSON RTE — when to prefer each
- Reference field discipline (cross-content-type references, max depth)
