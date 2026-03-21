# Message Protocol

## Overview

All inter-teammate communication uses structured JSON messages. This protocol abstracts the transport layer — messages work identically whether sent via Agent Teams direct messaging (primary) or file-based fallback.

## Message Format

```json
{
  "sender_role": "team_lead|planner|plan_reviewer|developer|code_reviewer|qa|merge_coordinator",
  "recipient_role": "team_lead|planner|plan_reviewer|developer|code_reviewer|qa|merge_coordinator",
  "message_type": "plan|review|correction|implementation_result|review_result|test_results|merge_request|escalation",
  "payload": {},
  "ticket_id": "PROJ-123",
  "timestamp": "2026-03-21T15:30:00Z"
}
```

## Message Types

### plan
**From:** Planner → Team Lead
**Payload:** The implementation plan JSON (see PLAN_SCHEMA.md)

### review
**From:** Plan Reviewer → Team Lead
**Payload:** Review decision (approved, corrections_needed, escalate)

### correction
**From:** Team Lead → Planner (forwarding reviewer corrections)
**Payload:** The correction issues from the reviewer

### implementation_result
**From:** Developer → Team Lead
**Payload:** Unit completion status, branch, files changed, test results

### review_result
**From:** Code Reviewer → Team Lead
**Payload:** Code review findings (approved or change_requests per unit)

### test_results
**From:** QA → Team Lead
**Payload:** Pass/fail matrix mapping each AC to test evidence

### merge_request
**From:** Team Lead → Merge Coordinator
**Payload:** List of branches to merge in dependency order

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
