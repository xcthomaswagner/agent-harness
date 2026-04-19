# Implicit Requirements by Feature Type

The ticket author usually describes the happy path. Competent implementation
requires edge cases the ticket never mentions. The analyst classifies a
ticket's feature type(s) and adds these ACs alongside the ticket-derived
ones, marking them `category: "implicit"`.

A ticket may match multiple feature types — a "buyer portal page with
form filters and an order list" matches both `form_controls` and
`list_view`. Apply every matched type's checklist.

If NO type matches (typo fix, pure refactor, doc-only), produce ZERO
implicit ACs. The checklist does not apply to every ticket.

AC IDs are positional (`AC-001`, `AC-002`, …) and stable only within
a single analyst run. Downstream artifacts must not persist joins by
ID across runs.

---

## Feature type: form_controls

Triggers: ticket mentions filters, search inputs, date pickers, amount
ranges, numeric inputs, dropdowns, validation messages, or UI with
user-supplied values driving results.

Add ACs for:
- Cross-field validation for range pairs (start date < end date, min
  < max, from < to). Invalid pair shows inline validation; form does
  not submit.
- Concurrent-change race safety. When the user changes multiple filter
  inputs rapidly, only the latest result set is shown. Two rapid
  changes (first slow, second fast) — final UI reflects the second
  request.
- URL state persistence. Filter state survives page reload and
  back/forward navigation. Apply filters, reload, confirm state.
- Session-timeout handling on any mutating action. Stale session
  redirects to login with return-URL preserved, not a generic error.
- Empty / error / loading states render without layout shift.

## Feature type: list_view

Triggers: table, grid, pagination controls, infinite scroll, sort
indicators, row actions on each item.

Add ACs for:
- First-page state: Previous / First controls disabled.
- Last-page state: Next / Last controls disabled.
- Single-page state: all pagination controls disabled.
- Empty list: distinct "empty-after-filter" message vs "empty-always".
- Sort stability: equal sort keys preserve prior order.
- Large payload handling: above threshold N, server-side pagination —
  not client-side.

## Feature type: crud_mutation

Triggers: create, add, update, edit, delete, remove of any entity.
Bulk operations (import N rows, delete selected) are a strong signal.

Add ACs for:
- Concurrent modification: two clients editing the same record — last
  writer wins OR optimistic-lock error surfaced to second writer
  (pick one and state it in the AC).
- Partial failure on batch: some rows succeed, some fail — result
  surfaces per-row status; does not silently succeed or fail whole.
- Audit trail: mutation is visible in audit log with actor, timestamp,
  before/after.
- Authorization: user without permission gets explicit denial, not
  silent no-op.

## Feature type: api_endpoint

Triggers: new or modified HTTP route, REST endpoint, webhook handler,
GraphQL resolver, RPC method.

Add ACs for:
- Auth rejection: missing / malformed / expired credentials each
  return 401 with distinct responses. Test one case per variant.
- Rate limit: configured limit enforced, returns 429 with
  Retry-After header.
- Payload size: oversize body returns 413, not 500.
- Idempotency where semantically relevant: POST creates accept an
  Idempotency-Key header; second call with same key returns first
  result.

## Feature type: auth_flow

Triggers: login, logout, sign-in, sign-up, session, token, MFA, SSO,
password reset.

Add ACs for:
- Session expiry: expired session → redirect-to-login with return-URL.
- Credential revocation: admin-revoked user cannot continue current
  session on next request (not just on next login).
- MFA enrollment: user without enforcement flag can still use
  account; user with enforcement must enroll before accessing
  protected pages.
- Rate limit on credential attempts: N failures locks or backs off.
- Password-reset token: single-use, time-limited, cannot be reused
  after successful reset.

## Feature type: data_import

Triggers: bulk create, upload CSV, upload Excel, import, migration,
sync job that ingests external data.

Add ACs for:
- Per-row validation: bad rows reported with line number and field;
  good rows processed.
- Duplicate handling: chosen strategy (skip / update / reject) stated
  and tested.
- Large file behavior: above N rows streams or chunks; does not load
  entirely in memory.
- Transactional semantics: clear on "all or nothing" vs "best effort".
- Audit trail: import record with source filename, user, row counts.

## Feature type: async_job

Triggers: scheduled job, queue, background task, cron, batch, retry.

Add ACs for:
- Retry semantics: transient failures retried with backoff; permanent
  failures do not loop.
- Idempotency: re-running the same job produces the same result.
- Observability: job emits structured logs with start / end / outcome;
  failure surfaces to monitoring, not just logs.
- Long-running handling: job running above N minutes does not
  time-out the worker silently.

## Feature type: integration

Triggers: call to external service, webhook consumer, third-party API,
SaaS connector.

Add ACs for:
- External failure: non-2xx response handled, user-facing message
  generic, full error logged.
- Timeout: configured timeout enforced; not unbounded wait.
- Credential rotation: expired credentials surface a specific error,
  not a generic "service unavailable".
- Schema drift: unexpected response shape logs a warning and fails
  closed; does not propagate partial data as success.
- Retry + idempotency on external side: duplicate requests caused by
  retry do not cause duplicate effects.

---

## Output format

For each matched feature type, emit implicit ACs as
`AcceptanceCriterion` entries with:
- `category: "implicit"`
- `feature_type: "<matched type>"`
- `text`: adapted from the checklist to the ticket's specific field
  names or entity names. Verbatim is acceptable when the ticket
  context does not specialize it.
- `verifiable_by`: one of `unit_test`, `integration_test`, `e2e_test`,
  `manual_review`, `static_analysis`. Pick the narrowest layer that
  can prove the behavior.

Implicit ACs are not less important than ticket-derived ACs. They
carry equal weight in planning, implementation, code review, and QA.

If the ticket text already covers one of the checklist items (e.g.,
ticket says "validate start date is before end date"), do NOT
duplicate it as an implicit AC. Classify that AC as ticket-derived
instead.

Record classification reasoning in the `classification_reasoning`
field on `EnrichedTicket`. Keep it short — one or two sentences
per matched type, stating the trigger words or phrases.
