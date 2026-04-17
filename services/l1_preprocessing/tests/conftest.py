"""Shared test fixtures for L1 Pre-Processing Service."""

import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import main
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_review_issue,
    open_connection,
    upsert_pr_run,
)
from main import app


@pytest.fixture(scope="session", autouse=True)
def _scrub_live_api_keys() -> Generator[None, None, None]:
    """Prevent webhook tests from making live Anthropic API calls.

    Several tests (notably in test_webhooks.py) exercise the full pipeline
    via the ASGI transport. Under ASGITransport, FastAPI background tasks
    run inline before the response awaits return — so any _process_ticket
    call that reaches TicketAnalyst.analyze() will hit the real Anthropic
    API if a live key is present in .env. On one developer's network this
    call hangs indefinitely, wedging the whole suite.

    Scrubbing the key at session scope forces the analyst to short-circuit
    ("analyst skipped — no key configured") so webhook-level dispatch tests
    don't accidentally depend on (or bill for) a live LLM call. Tests that
    need a real analyst should use AsyncMock on the TicketAnalyst directly
    rather than relying on environment leakage.
    """
    key = "anthropic_api_key"
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    if hasattr(main, "settings") and hasattr(main.settings, key):
        original = getattr(main.settings, key)
        object.__setattr__(main.settings, key, "")
    else:
        original = None
    try:
        yield
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
        if original is not None:
            object.__setattr__(main.settings, key, original)


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset lazily-initialized singletons and per-ticket state between tests.

    Without this, subsequent tests hitting the same ticket ID with the same
    trigger tag get treated as non-edge and silently skipped.
    """
    main._reset_state()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Self-learning miner fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def learning_conn(tmp_path: Path):
    """A fresh autonomy.db with the v5 schema applied.

    Used by every self-learning test — consolidates the identical
    open / ensure_schema / close pattern that the four Phase A
    test modules each had inline.
    """
    c = open_connection(tmp_path / "autonomy.db")
    try:
        ensure_schema(c)
        yield c
    finally:
        c.close()


def seed_pr_run_for_learning(
    conn,
    *,
    pr_number: int,
    ticket_id: str,
    client_profile: str,
    opened_at: str = "",
) -> int:
    """Insert a pr_runs row shaped for Detector 2's JOIN.

    Shared between human_issue_cluster tests and the backfill
    script's end-to-end test.
    """
    return upsert_pr_run(
        conn,
        PrRunUpsert(
            ticket_id=ticket_id,
            pr_number=pr_number,
            repo_full_name="acme/app",
            head_sha=f"sha-{pr_number}",
            client_profile=client_profile,
            opened_at=opened_at,
        ),
    )


def seed_human_issue_for_learning(
    conn,
    *,
    pr_run_id: int,
    category: str,
    file_path: str,
    summary: str = "human-flagged issue",
) -> int:
    """Insert a human_review review_issues row with is_valid=1."""
    return insert_review_issue(
        conn,
        pr_run_id=pr_run_id,
        source="human_review",
        file_path=file_path,
        category=category,
        summary=summary,
        is_valid=1,
    )
