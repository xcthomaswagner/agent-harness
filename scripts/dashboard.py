#!/usr/bin/env python3
"""Standalone trace dashboard — view pipeline run history without starting L1.

Usage:
    python scripts/dashboard.py [--port 8080]

Opens a browser-based dashboard showing all processed tickets with trace timelines.
Reads directly from data/logs/*.jsonl — no database, no L1 service required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add L1 module to path for tracer + dashboard imports
L1_DIR = Path(__file__).resolve().parents[1] / "services" / "l1_preprocessing"
sys.path.insert(0, str(L1_DIR))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

from trace_dashboard import router as dashboard_router  # noqa: E402

app = FastAPI(
    title="Agent Harness — Trace Dashboard",
    description="View pipeline run history and trace timelines.",
)

app.include_router(dashboard_router)


@app.get("/")
async def root() -> RedirectResponse:
    """Redirect root to the traces list."""
    return RedirectResponse(url="/traces")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Harness Trace Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to serve on (default: 8080)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    print(f"[dashboard] Trace dashboard starting at http://{args.host}:{args.port}/traces")
    print(f"[dashboard] Reading traces from {L1_DIR.parents[1] / 'data' / 'logs'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
