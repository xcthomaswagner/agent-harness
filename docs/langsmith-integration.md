# LangSmith Observability Integration

## Overview

LangSmith provides visualization and querying of the harness pipeline's structured logs. It can trace the full journey of a ticket through L1 → L2 → L3.

## Setup

1. **Get a LangSmith API key** at smith.langchain.com
2. Set environment variables:
   ```bash
   export LANGCHAIN_API_KEY="ls__..."
   export LANGCHAIN_PROJECT="agent-harness"
   export LANGCHAIN_TRACING_V2=true
   ```

3. Install the SDK:
   ```bash
   pip install langsmith
   ```

## Integration Points

### L1 Analyst Tracing

Wrap the analyst API call with LangSmith tracing:

```python
from langsmith import traceable

@traceable(name="ticket-analyst", run_type="chain")
async def analyze(self, ticket):
    # ... existing code
```

### Pipeline Phase Tracing

Each pipeline phase becomes a span:

```python
from langsmith import traceable

@traceable(name="l1-pipeline", run_type="chain")
async def process(self, ticket):
    with trace("analyst", run_type="llm"):
        output = await self._analyst.analyze(ticket)
    with trace("route", run_type="chain"):
        return await self._handle_enriched(output, log)
```

### Importing pipeline.jsonl

For agent sessions (which run as separate processes), import the structured logs after completion:

```python
from langsmith import Client

client = Client()

# Read pipeline.jsonl and create runs
import json
with open("/.harness/logs/pipeline.jsonl") as f:
    for line in f:
        entry = json.loads(line)
        client.create_run(
            name=entry["phase"],
            run_type="chain",
            inputs={"ticket_id": entry["ticket_id"]},
            outputs={"event": entry["event"]},
            project_name="agent-harness",
        )
```

## What You'll See in LangSmith

- **Per-ticket traces**: Full pipeline journey from webhook to PR
- **Phase timing**: Which phase is the bottleneck
- **Token usage**: Per-analyst-call token consumption
- **Error traces**: Where tickets fail and why
- **Comparison**: Side-by-side analysis of different ticket types
