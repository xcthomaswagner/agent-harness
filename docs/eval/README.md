# Analyst Evaluation

Goldens under `goldens/` encode expected analyst behavior on a curated
ticket set. Used to catch regressions when changing the analyst prompt
or the implicit-requirements checklist.

## Running

Mocked mode (fast, no API cost, runs in CI):

```
python scripts/eval_analyst.py
```

Mocked mode assembles the analyst system prompt and asserts each
expected feature type's checklist is present in the prompt string.
It does NOT call the LLM. It catches:

- `IMPLICIT_REQUIREMENTS.md` deleted, renamed, or removed from the
  prompt-assembly list
- Feature-type headers renamed or missing
- SKILL.md Step 5 (classification) section removed

It does NOT catch whether the analyst actually produces correct
implicit ACs on a real call. For that, use live mode.

Live mode (real Anthropic API, ~$0.25 total across all 5 goldens):

```
python scripts/eval_analyst.py --live
```

Live mode calls the real analyst and scores:

- `detected_feature_types` against `expected_feature_types`
- Implicit AC text content against `expected_implicit_acs` (substring
  match, case-insensitive)
- Ticket-AC count >= `expected_min_ticket_acs`
- Implicit-AC count <= `expected_max_implicit_acs`

Run live mode before merging any change to:
- `runtime/skills/ticket-analyst/SKILL.md`
- `runtime/skills/ticket-analyst/IMPLICIT_REQUIREMENTS.md`
- `services/l1_preprocessing/analyst.py`
- Any template under `runtime/skills/ticket-analyst/TEMPLATES/`

Iterate on a single golden:

```
python scripts/eval_analyst.py --live --golden form_heavy_order_history
```

## Adding a golden

1. Author `goldens/<id>.yaml` with:
   - `golden_id`: kebab- or snake-case id
   - `ticket`: `{ id, source, ticket_type, title, description, acceptance_criteria }`
   - `expected_feature_types`: list of feature-type strings from
     `runtime/skills/ticket-analyst/IMPLICIT_REQUIREMENTS.md`
   - `expected_implicit_acs`: list of substrings expected to appear in at
     least one implicit AC's text (case-insensitive)
   - `expected_min_ticket_acs`: sanity-floor on analyst's ticket-derived output
   - `expected_max_implicit_acs`: sanity-cap on analyst's implicit output
   - `notes`: provenance and any tricky edges
2. Run `python scripts/eval_analyst.py --live --golden <id>` to verify.
3. Commit the YAML.

## Interpreting failures

Each failing golden prints:

- Feature-type set mismatches (expected vs detected)
- Missing implicit AC substrings
- Count violations (ticket ACs below min, implicit ACs above max)

A failure is NOT proof the analyst is wrong. Inspect the output first:

- If the analyst's phrasing satisfies the expectation but the golden's
  substring is overly specific, relax the substring.
- If the analyst genuinely missed the expected behavior, fix the prompt
  or checklist and re-run.
- If the analyst over-classified (detected a feature type that doesn't
  apply), that's often a prompt issue — tighten the feature-type
  triggers in `IMPLICIT_REQUIREMENTS.md` and re-run.

## Corpus

| Golden | Purpose |
|---|---|
| `form_heavy_order_history` | The motivating case. Mirrors XCSF30-88825. Covers form_controls + list_view. |
| `simple_typo_fix` | Negative test. Checklist must NOT fire. |
| `crud_buyer_account` | CRUD + list_view coverage. |
| `new_api_endpoint` | api_endpoint + async_job coverage. |
| `auth_flow_mfa` | auth_flow coverage. |

5 goldens is the starter set. Add more when a real ticket surfaces a
feature-type combination that isn't represented — do not proactively
expand the corpus.

## Why a separate script and not pytest?

This script does not live under pytest because (a) live mode has a
real per-run API cost and should not be picked up by `pytest`-style
test discovery, and (b) mocked mode is fast enough to run directly
on-demand without the pytest harness tax. If workflow demands
integration, wrap the script in a pytest test gated by
`@pytest.mark.eval` — don't move the runner.
