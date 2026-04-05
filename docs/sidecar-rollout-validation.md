# Sidecar Rollout — Dry-Run Validation Checklist

Use this checklist after spawning a test Agent Team against a sample ticket
to confirm that L2 is emitting the autonomy-metrics sidecars correctly and
that L1 is ingesting them.

## 1. Verify sidecars exist in the worktree

After the pipeline completes, inspect the worktree's `.harness/logs/`:

```bash
ls -la <worktree>/.harness/logs/
```

Expect to see (depending on pipeline path):

- `code-review.md` + `code-review.json` — always
- `qa-matrix.md` + `qa-matrix.json` — always
- `judge-verdict.md` + `judge-verdict.json` — only when Judge ran
  (CHANGES_NEEDED path). Absence on APPROVED runs is expected.

Also verify the version marker:

```bash
cat <worktree>/.harness/runtime-version
# e.g., 2026.04.0-sidecar
```

## 2. Spot-check sidecar fields

### `code-review.json`

```bash
jq '.verdict, (.issues | length)' <worktree>/.harness/logs/code-review.json
jq '.issues[0]' <worktree>/.harness/logs/code-review.json
```

Confirm:
- `verdict` is `APPROVED` or `CHANGES_NEEDED`
- Each issue has a stable `id` matching `cr-N`
- `blocking` and `is_code_change_request` are both present as booleans
- `severity` and `category` use the documented vocabularies
- `issues` key exists even when empty

### `judge-verdict.json`

```bash
jq '.validated_issues[].source_issue_id, .rejected_issues[].source_issue_id' \
  <worktree>/.harness/logs/judge-verdict.json
```

Confirm every `source_issue_id` exactly matches a `cr-N` id in
`code-review.json`. No id should appear in both arrays. Every issue the
Judge saw should appear in exactly one array.

### `qa-matrix.json`

```bash
jq '.overall, (.issues | length)' <worktree>/.harness/logs/qa-matrix.json
```

Confirm:
- `overall` is `PASS` or `FAIL`
- Issue ids follow `qa-N`
- Only failing / NOT_TESTED checks appear; passing ones don't

## 3. Confirm L1 ingestion

When the PR is opened and the harness reports back to L1 (via the
pipeline's reporting hook or the webhook that uploads artifacts), L1
parses the sidecars.

### Logs

Look for these structlog events on the L1 service:

- `autonomy_sidecars_ingested` — success path, includes counts
- `sidecar_parse_failed` — malformed JSON / schema violation (investigate)
- `sidecar_missing_required_flag` — agent prompt drift (`blocking` or
  `is_code_change_request` missing on a code-review issue)

### Database

Inspect the pending-ingest table for this PR:

```sql
SELECT source, external_id, severity, category, is_code_change_request, is_valid
FROM pending_ai_issues
WHERE pr_external_id = '<pr-id>'
ORDER BY source, external_id;
```

Expected rows:
- `source = 'ai_review'` — one row per `cr-N` in code-review.json
- `source = 'qa'` — one row per `qa-N` in qa-matrix.json
- `is_valid` reflects Judge verdict (1 if validated or Judge didn't run,
  0 if rejected)

## 4. Known failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Sidecar files missing | Skill update didn't deploy; old runtime injected | Re-run `inject-runtime.sh`; check `.harness/runtime-version` |
| `sidecar_parse_failed: invalid_json` | Agent emitted trailing commas or comments | Review agent trace; tighten skill prompt |
| `sidecar_missing_required_flag` warnings | Agent omitted `blocking` or `is_code_change_request` | Surface to user; may indicate prompt drift |
| Judge sidecar present on APPROVED path | Team Lead spawned Judge unnecessarily | Check pipeline logs; Judge should only run on CHANGES_NEEDED |
| `source_issue_id` mismatch | Judge renumbered or invented ids | Reinforce "echo cr-N" in Judge prompt |
| `sidecar_coverage` metric shows gap | Mix of pre- and post-rollout PRs in window | Expected during rollout; monitor convergence |
