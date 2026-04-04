"""Basic health check test to verify the setup works."""

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "anthropic_api_key" in data
    assert "jira_configured" in data
