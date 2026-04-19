# TODO — Projects vocabulary + multi-source routing

> Status: open · Author: operator-dashboard session 2026-04-19
> This doc captures a design conversation. No code has been written
> yet. See the matching auto-memory note
> `session_2026_04_19_operator_dashboard.md` for the broader context
> of the operator-dashboard branch this is adjacent to.

## The model (words Thomas chose)

> "One project has one source (Jira OR ADO) and one repo for output.
> The harness listens on many projects simultaneously — each YAML in
> `runtime/client-profiles/` is one project. The `ai-implement` label
> (and `ai-quick`) is how the harness decides whether any given
> ticket on a listened-to project should be picked up."

The code already enforces this rule (`ado_routing_one_profile_per_project`
memory note from 2026-04-09). What's missing is the rule being NAMED
that way in the code, docs, and UI — today it's called "client
profile" which invites confusion.

## TODO checklist

- [ ] **(1) Rename UI surface "Client profiles" → "Projects"**
  - `services/operator_ui/src/views/Home.tsx` SectionHeader label
  - `services/operator_ui/src/views/Autonomy.tsx` view-head copy
  - `services/operator_ui/src/chrome/Sidebar.tsx` (leave — it says
    nothing about profiles; OK as-is)
  - Zero functional change. 15 min.

- [ ] **(2) Duplicate-binding validation at profile load**
  - `services/l1_preprocessing/client_profile.py` — new
    `validate_no_collisions()` run from `load_all` startup and on
    hot-reload. Raise / `log.error` when two profiles claim:
    - same `(source.type, source.project_key OR ado_project_name)`
    - OR same `target.owner/repo` pair
  - 3 tests: duplicate source, duplicate target, no duplicates.
  - ~50 LOC. **Fail closed** — operators want the error at startup,
    not a silent misroute.

- [ ] **(3) Document the source→target model**
  - `runtime/client-profiles/schema.yaml` — top-of-file comment
    ("1 project = 1 source + 1 repo; harness listens on N projects;
    ai-implement is the per-ticket opt-in")
  - New `docs/project-model.md` with a diagram
  - Quick reference in `CLAUDE.md` if it helps future sessions
  - ~30 min.

- [ ] **(4) [deferred] YAML schema cleanup** — rename `ticket_source`
  → `source`, `source_control` → `target` with a backward-compat
  migration layer. ~200 LOC + migration + 5 tests. Cosmetic. Only
  do it when we're already rewriting `client_profile.py` for another
  reason.

- [ ] **(5) [deferred] Onboarding flow** —
  `POST /api/operator/projects` (admin-gated) that takes
  `(source, target, ai_label)`, writes a new YAML, hot-reloads. OR
  the auto-detect variant (first unknown-project webhook with
  `ai-implement` creates a stub YAML + notifies operator). Only
  after the operator-dashboard branch merges.

## Three recipes for "one source needs to output to two repos"

The `ai-implement` label is how a ticket becomes eligible. If you
need different tickets on the same source to land in different
repos, there are three routing recipes. Listed low-to-high complexity.

### Recipe A — split the source (preferred, zero code)

Create two Jira/ADO projects, one per repo. Each gets its own YAML.
Matches the one-project-one-binding rule without any code change.
Cleanest organizational story ("storefront work → STORE project,
API work → API project"). Use when the organizational split aligns
with the repo split.

### Recipe B — per-label routing (low code)

Two YAMLs share the same `source.project_key` but use different
`ai_label` values:

```yaml
# storefront.yaml
ticket_source: { project_key: SHOP, ai_label: ai-storefront }
source_control: { repo: acme/storefront }

# api.yaml
ticket_source: { project_key: SHOP, ai_label: ai-api }
source_control: { repo: acme/api }
```

Operator picks the routing per ticket via label. Requires widening
`find_profile_by_ado_project` (and the Jira equivalent) to "first
match whose ai_label is present on the ticket's labels" + making
TODO #2's collision check allow same project_key when ai_label
differs.

### Recipe C — per-ticket override via custom field (medium)

Add a custom Jira/ADO field `target_repo`. When set, overrides the
profile's `target` for that ticket only. Escape hatch for one-off
cross-repo work.

## Out of scope (flagging for awareness)

- **Multi-repo writes from a single ticket** — analyst decomposes
  one ticket into sub-tickets landing in multiple repos. Needs L2
  to spawn multiple worktrees and L3 to track multi-repo PR sets.
  Only do this if a real use case arrives.
- **Platform-level project groups** (e.g., Salesforce org with
  multiple cloud surfaces) — already addressed via the
  `required_clouds` detector gating roadmap item. Different axis.
