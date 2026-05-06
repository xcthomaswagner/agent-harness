# Message Protocol

## Overview

All inter-teammate communication uses structured JSON messages. This protocol abstracts the transport layer — messages work identically whether sent via Agent Teams direct messaging (primary) or file-based fallback.

## Message Format

```json
{
  "sender_role": "team_lead|planner|challenger|plan_reviewer|developer|code_reviewer|judge|qa|merge_coordinator|run_reflector",
  "recipient_role": "team_lead|planner|challenger|plan_reviewer|developer|code_reviewer|judge|qa|merge_coordinator|run_reflector",
  "message_type": "plan|risk_challenge_result|plan_decision|review|correction|implementation_result|review_result|judge_result|test_results|merge_request|reflection_result|escalation",
  "payload": {},
  "ticket_id": "PROJ-123",
  "timestamp": "2026-03-21T15:30:00Z"
}
```

## Message Types

### plan
**From:** Planner → Team Lead
**Payload:** The implementation plan JSON (see PLAN_SCHEMA.md)

Authoritative artifact: `.harness/plans/plan-v<N>.json`

### risk_challenge_result
**From:** Challenger → Team Lead
**Payload:** Summary of `.harness/logs/risk-challenge.json`

Authoritative artifacts:
- `.harness/logs/risk-challenge.md`
- `.harness/logs/risk-challenge.json`

### plan_decision
**From:** Team Lead → Plan Reviewer
**Payload:** Team Lead synthesis of Challenger objections and routing decision

Authoritative artifacts:
- `.harness/logs/plan-decision.md`
- `.harness/logs/plan-decision.json`

### review
**From:** Plan Reviewer → Team Lead
**Payload:** Review decision (approved, corrections_needed, escalate)

Authoritative artifacts:
- `.harness/logs/plan-review.md`
- `.harness/logs/plan-review.json`

### correction
**From:** Team Lead → Planner (forwarding reviewer corrections)
**Payload:** The correction issues from the reviewer

### implementation_result
**From:** Developer → Team Lead
**Payload:** Unit completion status, branch, files changed, test results

Authoritative artifact: `.harness/logs/implementation-result-<unit_id>.json`

### review_result
**From:** Code Reviewer → Team Lead
**Payload:** Code review findings (approved or change_requests per unit)

Authoritative artifacts:
- `.harness/logs/code-review.md`
- `.harness/logs/code-review.json`

### judge_result
**From:** Judge → Team Lead
**Payload:** Validated and rejected review findings

Authoritative artifacts:
- `.harness/logs/judge-verdict.md`
- `.harness/logs/judge-verdict.json`

### test_results
**From:** QA → Team Lead
**Payload:** Pass/fail matrix mapping each AC to test evidence

Authoritative artifacts:
- `.harness/logs/qa-matrix.md`
- `.harness/logs/qa-matrix.json`

### merge_request
**From:** Team Lead → Merge Coordinator
**Payload:** List of branches to merge in dependency order

Authoritative artifacts:
- `.harness/logs/merge-report.md`
- `.harness/logs/merge-report.json`

### reflection_result
**From:** Run Reflector → Team Lead
**Payload:** Retrospective status and candidate count

Authoritative artifacts:
- `.harness/logs/retrospective.md`
- `.harness/logs/retrospective.json`

### escalation
**From:** Any → Team Lead
**Payload:** What failed, why, and what a human should do

## Transport: Agent Teams (Primary)

When Agent Teams API is available, messages are sent via direct teammate messaging:
- Use `SendMessage` tool with the recipient's agent name
- Include the full JSON message in the content
- The recipient parses the JSON from the message content

## Transport: File-Based Fallback

When Agent Teams is unavailable, messages route through the filesystem:

### Writing Messages
Write to: `/.harness/messages/{ticket_id}/{timestamp}-{sender}-to-{recipient}.json`

Example: `/.harness/messages/PROJ-123/20260321T153000Z-planner-to-team_lead.json`

### Reading Messages
The team lead polls `/.harness/messages/{ticket_id}/` and dispatches:
1. List files in the messages directory, sorted by timestamp
2. Read each unprocessed message
3. Route to the appropriate teammate's next action
4. Rename processed files with a `.done` suffix

### Fallback Detection
The team lead detects Agent Teams unavailability when:
- `SendMessage` tool is not available
- `SendMessage` returns an error
- No response received within 60 seconds

On detection, the team lead logs a warning and switches to file-based transport for the remainder of the session.

## Structured State Rule

Message payloads are notifications. Canonical files are authoritative. The Team
Lead must read the relevant `.harness/` artifact before routing a phase. If the
artifact is missing or invalid, re-prompt the teammate once; if it is still
missing or invalid, escalate instead of routing from chat history.
