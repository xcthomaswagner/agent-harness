"""Basic health check test to verify the setup works."""

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


async def test_health_does_not_expose_secret_presence(client: AsyncClient) -> None:
    """Regression: ``/health`` used to return ``anthropic_api_key``,
    ``jira_configured``, ``ado_configured``, ``webhook_secret`` and
    ``client_repo`` booleans. That told an unauthenticated caller
    exactly which integrations were wired up — useful reconnaissance
    before targeting a specific auth path. The endpoint must now
    return only liveness.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    # Only the liveness field is allowed.
    assert set(data.keys()) == {"status"}, (
        f"/health must expose only 'status', got: {sorted(data.keys())}"
    )
    # Explicit check for each previously-leaked field.
    for forbidden in (
        "anthropic_api_key",
        "jira_configured",
        "ado_configured",
        "webhook_secret",
        "client_repo",
    ):
        assert forbidden not in data, (
            f"/health leaked secret-presence field {forbidden!r}"
        )
