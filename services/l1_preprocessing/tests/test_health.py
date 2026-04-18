"""Basic health check test to verify the setup works."""

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    # Phase 1: /health must not leak secret-presence booleans to
    # unauthenticated callers. Previously it returned
    # ``anthropic_api_key``/``jira_configured``/``webhook_secret``
    # presence flags — now it returns liveness only.
    assert "anthropic_api_key" not in data
    assert "jira_configured" not in data
