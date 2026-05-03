# ContentStack QA Supplement

QA on a ContentStack ticket means verifying both halves: the live stack
matches what the schema migration claimed it would change, AND the
frontend actually renders the new shape correctly. Without both, you
have either a doc-only PR (the live stack didn't actually change) or
a frontend-only PR (the schema field doesn't exist for editors to
populate). Both are silent failures.

## Hard rules

### 1. Schema convergence — verify live shape matches the migration

For any ticket whose AC includes a content-type schema change, you MUST
verify against the live stack on `CONTENTSTACK_BRANCH`:

1. Call `mcp__contentstack__cma_get_content_type` (or
   `mcp__contentstack__cda_get_content_type` for read-only verification)
   against the configured branch.
2. Confirm every field the migration claimed to add/modify is present
   with the documented type, mandatory flag, and validation
   constraints.
3. Confirm no fields were unintentionally removed or renamed.

Diff the result against the `contentstack-migration/<TICKET>.md` file
in the PR. Any mismatch is a QA failure — the implement step did not
actually converge.

### 2. CDA round-trip — confirm a real entry can be fetched

After a schema change, fetch at least one entry of that content type
via the CDA on the configured branch + environment and confirm:

- The entry is retrievable (no 404, no 400).
- The new field appears on the entry payload (with `null`/empty value
  if no entry has been authored against the new field yet — that's
  expected for new optional fields).
- The field type in the payload matches the typed component shape
  on the frontend (string vs. object vs. array).

If no entries exist in the type yet, that's a SKIP, not a PASS — flag
it so a real ticket against an empty stack doesn't silently pass on
"renders nothing because there's no data."

### 3. Frontend render — actually exercise the new code path

For tickets that change rendering, do not rely on unit tests alone:

- Run `pnpm build` (or the configured `build_command`). Build failures
  often surface schema/SDK mismatches that unit tests with mocked data
  miss.
- If feasible, run `pnpm dev` and hit the affected page with a known
  entry slug. Capture a screenshot via the agent-browser tool. The
  retrospective from RND-89147 flagged "no final screenshot" as a real
  QA gap on UI-affecting changes.
- Verify the empty-field case: render with `subtitle = null` (or
  whatever the new optional field is) and confirm zero output where
  the field would have appeared. The unit test covers this in
  isolation; the live render confirms there's no surrounding wrapper
  emitting whitespace.

### 4. Branch isolation — never QA against production

QA actions (CMA reads, CDA fetches, dev server runs) MUST target the
branch configured in `CONTENTSTACK_BRANCH` (typically `ai`) and the
environment in `CONTENTSTACK_ENVIRONMENT` (typically `development`).
Never QA against `main` or `production` — even read-only against the
wrong environment may pull stale or different schema state and make
the QA result lie.

### 5. Edge-case ownership — every listed edge case ends in PASS or REJECTED

The RND-89147 retro flagged a real process gap: an edge case
("whitespace-only subtitle") was acknowledged by reviewer + QA +
simplify but no one owned the resolution. The truthy check shipped
with `<h2>   </h2>` as the failure mode.

For ContentStack tickets specifically — where most fields are
free-text and editors WILL produce edge cases (empty strings, runs of
whitespace, Unicode normalization, RTL-mixed text, very long
strings) — QA must close every entry in the ticket's `edge_cases`
list one of two ways:

- **PASS** — wrote a test that exercises the case AND the test passes.
- **REJECTED** — explicit note in the QA matrix saying why the case
  is out of scope, with reference to the ticket text or AC that
  scopes it out.

A SKIP without justification is treated as a QA failure. If the
implementer didn't address the case and the reviewer flagged it as
informational, QA owns either fixing it (in this PR) or rejecting it
(with rationale that survives a second-look review).

## Standard checklist for ContentStack tickets

Run through this checklist on every ContentStack ticket before
recording a QA verdict:

- [ ] CMA `get_content_type` against `CONTENTSTACK_BRANCH` shows the
      schema as documented in `contentstack-migration/<TICKET>.md`
- [ ] CDA fetch of at least one entry succeeds (or SKIP recorded with
      "no entries authored yet" reason)
- [ ] `pnpm build` succeeds (or the configured build command)
- [ ] `pnpm test` covers the new render states (with and without the
      new optional field, edge cases listed in the ticket)
- [ ] `pnpm typecheck` succeeds (catches schema-vs-frontend type drift)
- [ ] `pnpm lint` succeeds
- [ ] Live render verified via dev server + screenshot captured (for
      UI-affecting changes)
- [ ] Every entry in `edge_cases` ends in PASS or REJECTED — no SKIP
      without rationale
- [ ] `pnpm-lock.yaml` change is intentional (no surprise dependency
      additions)

## What to record in the QA matrix

For each functional AC, record the verification path you actually
ran — not just PASS/FAIL. Future debugging depends on knowing whether
"PASS" meant "ran a CDA fetch and the field was there" vs. "trusted
the unit test." The retrospective parser reads this output, so be
specific:

- **AC-001 (Unicode subtitle):** PASS via test
  `BlogPost.test.tsx::renders_unicode_subtitle`
- **AC-002 (CMA write succeeded):** PASS via
  `cma_get_content_type` showing `subtitle` field present on `ai`
  branch with `data_type: text`, `mandatory: false`
- **AC-003 (CDA returns new field on existing entries):** PASS via
  `cda_get_entry` on entry `<uid>` returning `subtitle: null`
- **AC-004 (frontend renders nothing when subtitle absent):** PASS via
  test `renders_no_subtitle_element_when_undefined` + dev-server
  visual check at `/blog/<slug>` with screenshot in `.harness/qa/`

## Patterns mined from real runs

(none yet — first real CMA-verifying ticket runs ahead)
