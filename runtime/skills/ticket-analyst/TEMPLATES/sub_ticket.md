# Sub-Ticket Template

The sub-ticket format is defined in the Path C output schema in `SKILL.md`:

```json
{
  "output_type": "decomposition",
  "reason": "Why decomposition is needed",
  "sub_tickets": [
    {
      "title": "Sub-ticket title",
      "description": "What this sub-ticket covers",
      "estimated_size": "small|medium",
      "acceptance_criteria": ["AC 1", "AC 2"]
    }
  ],
  "dependency_order": ["sub-1", "sub-2", "sub-3"]
}
```

The L1 pipeline posts this as a Jira comment with `needs-splitting` label for manual PM action.
