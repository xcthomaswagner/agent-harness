# Structured Log Format

Every teammate writes structured JSON Lines logs to `/.harness/logs/`.

## Log Entry Schema

```json
{
  "timestamp": "2026-03-21T15:30:00.123Z",
  "ticket_id": "PROJ-123",
  "phase": "planning|plan_review|implementation|code_review|qa|merge|pr_review",
  "teammate_role": "team_lead|planner|plan_reviewer|developer|code_reviewer|qa|merge_coordinator",
  "event_type": "phase_started|phase_completed|phase_failed|decision|tool_call|message_sent|message_received|escalation",
  "details": {
    "description": "Human-readable description of what happened",
    "data": {}
  },
  "token_usage": {
    "input_tokens": 1500,
    "output_tokens": 500
  },
  "duration_ms": 3200
}
```

## Log Files

| File | Contents |
|------|----------|
| `/.harness/logs/session.log` | Full session log (all teammates) |
| `/.harness/logs/pipeline.jsonl` | Structured JSON Lines (machine-readable) |

## Required Log Events

Every teammate MUST log:

### Phase transitions
```json
{"event_type": "phase_started", "phase": "implementation", "details": {"description": "Starting implementation with 3 dev teammates"}}
{"event_type": "phase_completed", "phase": "implementation", "details": {"description": "3/3 units complete", "data": {"units_complete": 3, "units_blocked": 0}}}
```

### Decisions
```json
{"event_type": "decision", "details": {"description": "Chose 3 dev teammates because plan has 3 independent units", "data": {"unit_count": 3, "parallel_tracks": 2}}}
```

### Escalations
```json
{"event_type": "escalation", "details": {"description": "Plan review failed after 2 rounds", "data": {"unresolved_issues": ["same-file conflict in unit-2 and unit-3"]}}}
```

## How to Write Logs

Append to `/.harness/logs/pipeline.jsonl`:

```bash
echo '{"timestamp":"2026-03-21T15:30:00Z","ticket_id":"PROJ-123","phase":"planning","teammate_role":"planner","event_type":"phase_started","details":{"description":"Starting plan decomposition"}}' >> /.harness/logs/pipeline.jsonl
```

Or use the Write tool to append a line.

## Observability Integration (Phase 2+)

Structured logs in JSON Lines format are compatible with:
- **LangSmith** — import for visualization and trace analysis
- **Datadog/Splunk** — ship via log collector
- **Custom dashboard** — parse JSON Lines for metrics
