# /run-reflection — End-of-Run Retrospective

Read the full pipeline trace for this run and emit a structured retrospective that the self-learning miner can ingest later. You are the last step before PR creation — your output is what teaches the system.

## When This Runs

After `/simplify` completes and before PR creation. Runs ONCE per pipeline run, regardless of whether the pipeline succeeded or partially escalated.

## Inputs

- `.harness/ticket.json` — enriched ticket
- `.harness/logs/pipeline.jsonl` — phase transitions logged by the Team Lead
- `.harness/logs/*.md` — span detail files (code-review.md, qa-matrix.md, judge-verdict.md, plan-review.md, merge-report.md, simplify.md, blocked-units.md, escalation.md — any that exist)
- `.harness/logs/*.json` — structured sidecars (code-review.json, qa-matrix.json, judge-verdict.json — if present)
- `.harness/plans/plan-v*.json` — plan and any revisions

Use `Glob` to enumerate `.harness/logs/*.{md,json}`. Missing files are normal. Do not error on absence.

## Outputs

Two files, both required. Write them in this order:

### `.harness/logs/retrospective.md`

Human-readable narrative. Structure loosely as:

```markdown
# Retrospective — <ticket-id>

## Pipeline Summary
Short paragraph: pipeline mode, which phases ran, overall verdict.

## What Went Well
- Specific things, with pointers to artifacts.

## What Went Wrong
- Specific things, with pointers to artifacts.

## What Would Have Caught It Earlier
- If a pattern emerged, describe the rule/check that would have prevented it.

## Proposals
1-2 sentences per proposal, mirroring what's in the JSON candidates.
```

### `.harness/logs/retrospective.json`

Machine-readable, CANONICAL schema:

```json
{
  "schema_version": 1,
  "status": "ok",
  "ticket_id": "<ticket.json.id>",
  "trace_id": "<from any pipeline.jsonl entry>",
  "generated_at": "<ISO-8601 UTC now>",
  "markdown_summary": "1-2 sentence summary",
  "error": null,
  "lesson_candidates": [
    {
      "pattern_key": "stable identifier, SAME across tickets for the same rule",
      "scope_key": "<client_profile>|<platform_profile>|<pattern_key>|<ticket_id>",
      "severity": "critical|warning|info",
      "client_profile": "xcsf30",
      "platform_profile": "salesforce",
      "proposed_delta_json": "{\"rule\":\"...\",\"target\":\"...\"}",
      "evidence_refs": [
        {"source_ref": "judge-verdict.json", "snippet": "optional short quote"}
      ]
    }
  ]
}
```

#### Field rules

| Field | Rules |
|---|---|
| `schema_version` | Literal integer `1`. |
| `status` | `"ok"` on success; `"failed"` on any internal error. |
| `ticket_id` | From `ticket.json.id` (or `ticket_id`). Required. |
| `trace_id` | From any pipeline.jsonl entry's `trace_id`, or empty string. |
| `generated_at` | ISO-8601 UTC. Produce via `date -u +%Y-%m-%dT%H:%M:%SZ` at write time. |
| `markdown_summary` | 1–2 sentences. No PII, no verbatim credentials. |
| `error` | `null` when `status=ok`; short string when `status=failed`. |
| `pattern_key` | STABLE across runs. Do NOT embed `ticket_id` here. |
| `scope_key` | Per-ticket observations: include `ticket_id`. Shape `"<client>\|<platform>\|<pattern_key>\|<ticket_id>"`. |
| `severity` | `"critical"`, `"warning"`, or `"info"`. Reserve `"critical"` for clear process regressions. |
| `client_profile`, `platform_profile` | Copy from `ticket.json`. Empty strings allowed — the miner drops rows with unresolvable platforms. |
| `proposed_delta_json` | JSON STRING (not an object) under 500 chars. Miner treats as opaque. |
| `evidence_refs` | 0–5 items. Each item: `source_ref` (artifact filename) + optional `snippet` (max 200 chars, redacted). |

## Which Patterns to Emit

Only emit a candidate if you can point at a specific artifact for the evidence. Guardrails:

- Judge rejected >80% of reviewer findings → propose tightening reviewer rubric.
- QA caught an AC that the reviewer did not mention → propose extending the code-review supplement.
- Reviewer flagged a regression class that the tests missed → propose test scenarios.
- Cross-cutting process observation (e.g., planner missed a dependency, merge conflicts clustered in one directory) → one proposal.

Do NOT duplicate patterns already covered by dedicated detectors:

- Simplify wrote no sidecar → covered by `simplify_no_sidecar` detector.
- Cross-unit object pivot without permset realignment → covered by `cross_unit_object_pivot` detector.
- Form-control AC gap (cross-field validation, race safety, URL state, session timeout) → covered by `form_controls_ac_gaps` detector. Note: the detector currently uses phrase-match heuristics against AC text because the analyst does not emit a structured `category` field on generated acceptance criteria. When analyst output gains AC taxonomy, the detector will switch to category-lookup; today's behavior is an intentional interim.
- Reviewer/Judge rejection rate trend → covered by `reviewer_judge_rejection_rate` detector.

## Failure Protocol

If anything goes wrong mid-reflection:

1. Write `retrospective.json` with `"status": "failed"`, `"error": "<one-line cause>"`, `"lesson_candidates": []`.
2. Write `retrospective.md` noting the failure.
3. Return normally. Do NOT raise.

The miner's `retrospective_ingest.py` drops rows where `status != "ok"`, so a failed retrospective contributes zero lesson rows without blocking the pipeline.

## Verify Before Returning

Before exiting, confirm:

- [ ] `.harness/logs/retrospective.json` exists and parses as valid JSON.
- [ ] It contains the top-level keys `schema_version`, `status`, `ticket_id`, `generated_at`, `lesson_candidates`.
- [ ] `schema_version == 1`.
- [ ] `status` is `"ok"` or `"failed"`.
- [ ] `lesson_candidates` is a list (possibly empty).
- [ ] `.harness/logs/retrospective.md` exists and is non-empty.

If any check fails, rewrite both files to the failure-protocol state and return.

## Version History

- **v1** (2026-04-17): initial release. Covers judge-rejection patterns, missed-AC patterns, and cross-cutting process observations. The miner records these as `detector_name="run_reflector"`, version 1.
