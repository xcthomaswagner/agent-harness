# QA Matrix Template

The QA teammate outputs a structured matrix mapping every acceptance criterion to test evidence.

## Format

```json
{
  "ticket_id": "PROJ-123",
  "validation_timestamp": "2026-03-21T15:30:00Z",
  "overall_status": "pass|partial|fail",
  "criteria": [
    {
      "criterion": "User can view their current profile information",
      "source": "original|generated",
      "status": "pass|fail|not_tested",
      "evidence": [
        {
          "test_name": "test_profile_displays_user_info",
          "test_type": "unit",
          "result": "pass|fail",
          "details": "Renders name, email, and avatar correctly",
          "screenshot": null
        }
      ],
      "notes": ""
    },
    {
      "criterion": "Avatar must be resized to 256x256",
      "source": "original",
      "status": "pass",
      "evidence": [
        {
          "test_name": "test_resize_to_256x256",
          "test_type": "unit",
          "result": "pass",
          "details": "Input 1000x800 → output 256x256 verified",
          "screenshot": null
        }
      ],
      "notes": ""
    }
  ],
  "edge_cases": [
    {
      "case": "User has no first name set",
      "status": "pass|fail|not_tested",
      "test_name": "test_handles_missing_name",
      "notes": ""
    }
  ],
  "summary": {
    "total_criteria": 5,
    "passed": 4,
    "failed": 1,
    "not_tested": 0,
    "pass_rate": 0.8
  },
  "failures": [
    {
      "criterion": "Error message shown if upload fails",
      "test_name": "test_upload_error_display",
      "error": "Expected 'Upload failed' text but found 'undefined'",
      "owning_unit": "unit-3",
      "suggested_fix": "Check error state handling in AvatarUpload component"
    }
  ]
}
```

## Rules

- Every acceptance criterion (original + generated) MUST appear in the matrix
- Every edge case from the enriched ticket MUST appear
- `not_tested` is acceptable only if no test could reasonably be written (document why)
- Screenshots are required for all e2e test evidence
- `overall_status` is `pass` only when ALL criteria pass
