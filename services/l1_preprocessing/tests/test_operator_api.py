"""Tests for operator_api — /operator SPA shell + static assets.

Covers:

* Shell route returns HTML with the configured API key injected into the
  meta tag so the SPA can use it on subsequent fetch/SSE calls.
* Shell route requires auth when ``settings.api_key`` is configured —
  missing key → 401. Reuses the query-param-or-header auth pattern.
* SPA fallback — deep paths like ``/operator/traces/HARN-123`` return
  the shell (not 404), so client-side routing works.
* Static asset routes serve the built JS/CSS/JSON with correct content
  types, without requiring auth (browser loads them after shell
  establishes the session).
* Asset lookalike paths under /operator/ (foo.js, foo.css) are
  rejected with 404 so the SPA catch-all does not accidentally serve
  HTML with a .js extension.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from operator_api import router


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Default-open posture so most tests can omit the key; specific
    # auth tests re-configure the key.
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    return TestClient(_mk_app())


@pytest.fixture
def secured_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "api_key", "secret-key-42")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)
    return TestClient(_mk_app())


def test_shell_returns_html(client: TestClient) -> None:
    r = client.get("/operator/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<div id=\"app\"></div>" in r.text


def test_shell_no_trailing_slash(client: TestClient) -> None:
    r = client.get("/operator")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_shell_injects_api_key_into_meta_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "api_key", "deterministic-test-key")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)
    c = TestClient(_mk_app())
    r = c.get("/operator/?api_key=deterministic-test-key")
    assert r.status_code == 200
    assert (
        '<meta name="operator-api-key" content="deterministic-test-key">'
        in r.text
    )


def test_shell_escapes_api_key_special_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Operator-owned key; escaping is defence against broken key config,
    # not a security boundary, but still verify we do the right thing.
    from urllib.parse import quote

    weird_key = 'ab"c<d>&e'
    monkeypatch.setattr(settings, "api_key", weird_key)
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)
    c = TestClient(_mk_app())
    r = c.get(f"/operator/?api_key={quote(weird_key)}")
    assert r.status_code == 200
    assert (
        '<meta name="operator-api-key" content="ab&quot;c&lt;d&gt;&amp;e">'
        in r.text
    )


def test_shell_requires_auth_when_key_configured(
    secured_client: TestClient,
) -> None:
    r = secured_client.get("/operator/")
    assert r.status_code == 401


def test_shell_accepts_query_param_auth(secured_client: TestClient) -> None:
    r = secured_client.get("/operator/?api_key=secret-key-42")
    assert r.status_code == 200


def test_shell_accepts_header_auth(secured_client: TestClient) -> None:
    r = secured_client.get(
        "/operator/", headers={"X-API-Key": "secret-key-42"}
    )
    assert r.status_code == 200


def test_spa_fallback_serves_shell_for_deep_paths(
    client: TestClient,
) -> None:
    for path in [
        "/operator/traces",
        "/operator/traces/HARN-123",
        "/operator/autonomy",
        "/operator/autonomy/xcsf30",
        "/operator/learning",
        "/operator/pr/PR-1184",
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} should serve shell"
        assert r.headers["content-type"].startswith("text/html"), path
        assert "<div id=\"app\"></div>" in r.text, path


def test_spa_fallback_rejects_asset_lookalikes(client: TestClient) -> None:
    # A path like /operator/imagined.js shouldn't serve the HTML shell with a
    # .js extension; it would break the browser's script parser and could
    # shadow future real assets. 404 is correct.
    for path in [
        "/operator/nonexistent.js",
        "/operator/foo.css",
        "/operator/bar.json",
        "/operator/map.map",
    ]:
        r = client.get(path)
        assert r.status_code == 404, f"{path} should 404 not serve shell"


def test_operator_js_served_with_js_mime(client: TestClient) -> None:
    r = client.get("/operator/operator.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_tokens_css_served_with_css_mime(client: TestClient) -> None:
    r = client.get("/operator/tokens.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_build_json_served_with_json_mime(client: TestClient) -> None:
    r = client.get("/operator/build.json")
    assert r.status_code == 200
    assert "json" in r.headers["content-type"]


def test_static_assets_do_not_require_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "api_key", "secret-key-42")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)
    c = TestClient(_mk_app())
    # No API key → shell 401, assets still 200.
    assert c.get("/operator/").status_code == 401
    assert c.get("/operator/operator.js").status_code == 200
    assert c.get("/operator/tokens.css").status_code == 200


def test_shell_sets_no_store_cache_header(client: TestClient) -> None:
    """The API key is injected per request — the shell must never be cached."""
    r = client.get("/operator/")
    assert r.headers.get("cache-control") == "no-store"


def test_missing_static_dir_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a deployment where the SPA bundle was never built.
    import operator_api

    monkeypatch.setattr(
        operator_api, "OPERATOR_STATIC_DIR", tmp_path / "never-built"
    )
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    c = TestClient(_mk_app())
    r = c.get("/operator/")
    assert r.status_code == 503
    assert "npm run build" in r.text.lower()
