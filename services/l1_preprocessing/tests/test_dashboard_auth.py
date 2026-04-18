"""Phase 1: dashboard / admin-GET auth tests.

Covers the ``_require_dashboard_auth`` dependency on:
    * ``GET /traces/{ticket_id}/bundle``
    * ``GET /traces/{ticket_id}/artifact/{artifact_type}``
    * ``GET /stats/webhooks``

Each endpoint must:
    * Fail closed (503) when neither ``API_KEY`` nor
      ``DASHBOARD_ALLOW_ANONYMOUS=true`` is configured.
    * Accept the request when ``DASHBOARD_ALLOW_ANONYMOUS=true``.
    * Accept the request when ``API_KEY`` is set and the caller
      supplies a matching ``X-API-Key`` header.
    * Reject with 401 when ``API_KEY`` is set and the caller omits or
      supplies the wrong ``X-API-Key`` header.

Also verifies ``/health`` no longer leaks secret-presence booleans.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from tracer import ARTIFACT_SESSION_LOG, append_trace


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    return logs


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _seed_bundle_trace(trace_dir: Path, ticket_id: str) -> None:
    """Minimal trace needed so ``/bundle`` doesn't short-circuit with 404."""
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(
            ticket_id, "t0", "webhook", "jira_webhook_received",
            ticket_type="story", source="jira", title="Add widget",
        )
        append_trace(
            ticket_id, "t0", "artifact", ARTIFACT_SESSION_LOG,
            content="session narrative content here",
        )


async def test_bundle_requires_auth_when_api_key_set(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """When ``API_KEY`` is configured, unauthenticated bundle requests
    are rejected with 401 and authenticated ones succeed."""
    _seed_bundle_trace(trace_dir, "AUTH-1")
    with (
        patch("tracer.LOGS_DIR", trace_dir),
        patch("main.settings.api_key", "super-secret"),
        patch("main.settings.dashboard_allow_anonymous", False),
    ):
        missing = await client.get("/traces/AUTH-1/bundle")
        assert missing.status_code == 401
        wrong = await client.get(
            "/traces/AUTH-1/bundle",
            headers={"X-API-Key": "nope"},
        )
        assert wrong.status_code == 401
        ok = await client.get(
            "/traces/AUTH-1/bundle",
            headers={"X-API-Key": "super-secret"},
        )
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "application/gzip"


async def test_bundle_fails_closed_when_no_api_key_and_no_anonymous(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Phase 1 fail-closed: empty API key + ``dashboard_allow_anonymous=False``
    raises 503. Previously /bundle was open to anyone with network access."""
    _seed_bundle_trace(trace_dir, "AUTH-2")
    with (
        patch("tracer.LOGS_DIR", trace_dir),
        patch("main.settings.api_key", ""),
        patch("main.settings.dashboard_allow_anonymous", False),
    ):
        resp = await client.get("/traces/AUTH-2/bundle")
        assert resp.status_code == 503
        assert "auth" in resp.json()["detail"].lower()


async def test_bundle_opens_when_anonymous_flag_set(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Phase 1 opt-in: ``dashboard_allow_anonymous=True`` with no
    API key opens the dashboard routes for local dev."""
    _seed_bundle_trace(trace_dir, "AUTH-3")
    with (
        patch("tracer.LOGS_DIR", trace_dir),
        patch("main.settings.api_key", ""),
        patch("main.settings.dashboard_allow_anonymous", True),
    ):
        resp = await client.get("/traces/AUTH-3/bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"


async def test_stats_webhooks_fails_closed_without_auth(
    client: AsyncClient,
) -> None:
    """``/stats/webhooks`` also gets fail-closed treatment."""
    with (
        patch("main.settings.api_key", ""),
        patch("main.settings.dashboard_allow_anonymous", False),
    ):
        resp = await client.get("/stats/webhooks")
    assert resp.status_code == 503


async def test_artifact_requires_api_key_when_set(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """``/traces/{id}/artifact/{type}`` requires X-API-Key when configured."""
    _seed_bundle_trace(trace_dir, "ART-1")
    with (
        patch("tracer.LOGS_DIR", trace_dir),
        patch("main.settings.api_key", "art-secret"),
        patch("main.settings.dashboard_allow_anonymous", False),
    ):
        missing = await client.get("/traces/ART-1/artifact/session_log")
        assert missing.status_code == 401
        ok = await client.get(
            "/traces/ART-1/artifact/session_log",
            headers={"X-API-Key": "art-secret"},
        )
        assert ok.status_code == 200


async def test_health_does_not_expose_secret_presence(
    client: AsyncClient,
) -> None:
    """Phase 1: /health returns liveness only. The prior shape leaked
    booleans about which credentials were configured (webhook_secret,
    anthropic_api_key, etc.) to every unauthenticated caller."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # Only ``status`` should be present; anything else is a regression.
    assert body == {"status": "ok"}
    for leaky in (
        "anthropic_api_key",
        "jira_configured",
        "ado_configured",
        "webhook_secret",
        "client_repo",
        "api_key",
        "dashboard_allow_anonymous",
        "allow_unsigned_webhooks",
    ):
        assert leaky not in body, (
            f"/health must not expose {leaky!r} — secret-presence leak"
        )
