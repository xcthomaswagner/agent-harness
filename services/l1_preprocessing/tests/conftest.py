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


def seed_lesson_candidate(
    *,
    scope: str = "xcsf30|salesforce|security|*.cls",
    detector: str = "human_issue_cluster",
    pattern: str = "security|*.cls",
    client_profile: str = "xcsf30",
    platform_profile: str = "salesforce",
    frequency: int = 3,
    proposed_delta_json: str = "",
) -> str:
    """Insert a proposed lesson candidate; return its lesson_id.

    Uses ``autonomy_conn`` so it respects the ``autonomy_db_path``
    setting the caller configured. Shared between the learning API
    and dashboard test modules.
    """
    from autonomy_store import (
        LessonCandidateUpsert,
        autonomy_conn,
        upsert_lesson_candidate,
    )
    from learning_miner.detectors.base import compute_lesson_id

    lid = compute_lesson_id(detector, pattern, scope)
    with autonomy_conn() as conn:
        upsert_lesson_candidate(
            conn,
            LessonCandidateUpsert(
                lesson_id=lid,
                detector_name=detector,
                pattern_key=pattern,
                client_profile=client_profile,
                platform_profile=platform_profile,
                scope_key=scope,
                window_frequency=frequency,
                proposed_delta_json=proposed_delta_json or "{}",
            ),
        )
    return lid


def make_anthropic_response(
    text: str, *, tokens_in: int = 100, tokens_out: int = 50
):
    """Build a MagicMock shaped like an Anthropic Messages response.

    Shared by analyst + drafter tests so the mock shape can't drift
    between them. The real Anthropic SDK returns an object with
    ``content = [TextBlock(type='text', text=...)]`` and a ``usage``
    record carrying input/output token counts.
    """
    from unittest.mock import MagicMock

    block = MagicMock()
    block.type = "text"
    block.text = text
    usage = MagicMock()
    usage.input_tokens = tokens_in
    usage.output_tokens = tokens_out
    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    return resp


@pytest.fixture
def mock_anthropic_client():
    """AsyncMock-shaped Anthropic client for drafter/analyst tests."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


def seed_draft_ready_candidate(
    *,
    unified_diff: str = "--- a/x\n+++ b/x\n@@\n+rule",
    target_path: str = "runtime/skills/code-review/SKILL.md",
    rationale: str = "r",
) -> str:
    """Seed a candidate and walk it to ``status='draft_ready'``.

    Shared by PR-opener tests that need a lesson poised for
    /approve. Callers may override the unified_diff when they want
    a specific scope or the drafter-emission to fail a path check.
    """
    import json

    from autonomy_store import autonomy_conn, update_lesson_status

    lid = seed_lesson_candidate(
        proposed_delta_json=json.dumps(
            {
                "target_path": target_path,
                "unified_diff": unified_diff,
                "rationale_md": rationale,
            }
        ),
    )
    with autonomy_conn() as conn:
        update_lesson_status(conn, lid, "draft_ready", reason="drafter ok")
    return lid


def seed_applied_candidate(
    *,
    merged_commit_sha: str = "abc1234",
    pr_url: str = "https://github.com/x/y/pull/1",
) -> str:
    """Walk a fresh candidate all the way to ``status='applied'``.

    Used by the revert flow tests that need a lesson with a recorded
    merge commit sha. Walks proposed → draft_ready → approved →
    applied in one call.
    """
    from autonomy_store import autonomy_conn, update_lesson_status

    lid = seed_draft_ready_candidate()
    with autonomy_conn() as conn:
        update_lesson_status(conn, lid, "approved", reason="test")
        update_lesson_status(
            conn, lid, "applied",
            reason="test",
            pr_url=pr_url,
            merged_commit_sha=merged_commit_sha,
        )
    return lid


@pytest.fixture
def learning_api_client(configure_admin_auth: str):
    """Pre-wired TestClient for learning_api.py.

    Builds a minimal FastAPI app with just the learning router mounted
    plus the admin auth settings. Three PR-opener test classes
    previously reconstructed this inline.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from learning_api import router as learning_api_router

    app = FastAPI()
    app.include_router(learning_api_router)
    client = TestClient(app)
    token = configure_admin_auth
    client.headers.update({"X-Autonomy-Admin-Token": token})
    return client


@pytest.fixture
def configure_admin_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Point autonomy.db at a scratch DB and set a predictable admin token.

    Returns the configured admin token so tests can build the
    ``X-Autonomy-Admin-Token`` header. Also installs a generous
    rate-limit bucket so happy-path tests never trip 429; abuse
    tests monkey-patch ``autonomy_ingest._bucket`` to force it.
    """
    import autonomy_ingest
    from autonomy_ingest import TokenBucket
    from config import settings as _settings

    db_path = tmp_path / "autonomy.db"
    token = "admin-token"
    monkeypatch.setattr(_settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(_settings, "autonomy_admin_token", token)
    monkeypatch.setattr(
        autonomy_ingest,
        "_bucket",
        TokenBucket(capacity=100, refill_per_sec=100.0),
    )
    return token
