# operator-go

Go implementation of the Agentic Harness operator dashboard.

This is intentionally parallel to `services/operator_ui` rather than an
in-place replacement. The current FastAPI/Preact dashboard remains the default
at `/operator` while the Go frontend can be tested side by side and promoted
after parity checks.

## Run locally

Start the existing L1 service first:

```sh
cd services/l1_preprocessing
uvicorn main:app --reload --port 8000
```

Then start the Go frontend:

```sh
cd services/operator_go
DASHBOARD_API_KEY="$DASHBOARD_API_KEY" \
OPERATOR_BACKEND_URL=http://127.0.0.1:8000 \
go run ./cmd/operator-go
```

Open:

```text
http://127.0.0.1:8081/operator/?api_key=<dashboard-key>
```

The Go service serves the dashboard shell and proxies these existing backend
routes without changing their schemas:

- `/api/operator/*`
- `/api/learning/*`
- `/api/traces/*`

The first authenticated shell load stores the operator key in a local
HttpOnly cookie so deep links such as `/operator/runs` and
`/operator/traces/RND-89151` are reloadable.

## Product shape

The Go dashboard keeps the current operator information architecture:

- `Command Center`: attention items, active runs, lessons, client health.
- `Runs`: active, attention, recent, successful, failed, and hidden buckets.
- `Run Detail`: phase timeline, activity summary, teammates, raw events, and
  stale/misfire/hide actions.
- `Client Health`: profile-level quality and automation indicators.
- `Learning`: lesson triage with draft, approve, snooze, and reject actions.
- `Repo Workflow`: generate, edit, and save repo-local `WORKFLOW.md`.

## Validate

```sh
cd services/operator_go
go test ./...
```

The repo-wide validation wrapper also includes this service:

```sh
python scripts/test_all.py --skip-root --skip-l1 --skip-l3 --skip-ui
```
