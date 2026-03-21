"""Shared test fixtures for L1 Pre-Processing Service."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

import main
from main import app


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset lazily-initialized singletons between tests."""
    main._jira_adapter = None
    main._pipeline = None


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
