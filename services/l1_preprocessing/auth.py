"""Auth dependencies for FastAPI routes.

Extracted from ``main.py`` so router modules (``webhooks.py``,
``trace_bundle.py``, ``completion.py``) can attach the same
``Depends(_require_api_key)`` without importing from ``main`` — which
would circular-import back through the routers that ``main`` mounts.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency that enforces API key auth on internal control-plane endpoints.

    Skipped when API_KEY is not configured (local dev mode). Uses
    ``hmac.compare_digest`` for the comparison so the check is
    constant-time — a plain ``!=`` leaks byte-by-byte timing info
    about the configured secret because CPython short-circuits
    string equality on the common-prefix length.

    Settings are looked up via ``main`` at call time so
    ``patch("main.settings")`` is honored by every router module's
    dependency-injected calls.
    """
    import main  # local import dodges module-load circular import
    settings = main.settings
    if not settings.api_key:
        return  # No key configured — open access (local dev)
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _require_dashboard_auth(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency that enforces API key auth on dashboard / admin GET endpoints.

    Phase 1 fail-closed default for dashboards: when neither
    ``settings.api_key`` nor ``settings.dashboard_allow_anonymous``
    is configured, raise 503 so operators discover the unprotected
    state instead of silently exposing the UI. The escape hatch for
    local dev is setting ``DASHBOARD_ALLOW_ANONYMOUS=true`` (which
    logs a startup warning).

    * ``settings.api_key`` set — require the X-API-Key header (same
      constant-time compare as ``_require_api_key``).
    * ``settings.api_key`` unset AND ``dashboard_allow_anonymous`` true —
      open access (local dev opt-in, not the default).
    * Both unset — fail closed with 503.
    """
    import main
    settings = main.settings
    if settings.api_key:
        if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(
                status_code=401, detail="Invalid or missing X-API-Key"
            )
        return
    if settings.dashboard_allow_anonymous:
        return
    raise HTTPException(status_code=503, detail="Dashboard auth not configured")


def _require_dashboard_auth_query_or_header(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    """Same policy as ``_require_dashboard_auth`` but also accepts
    ``?api_key=...`` query string.

    The query-param path exists because the browser ``EventSource``
    API cannot send custom headers. It is less secure than the header
    form (the key lands in server access logs, browser history, and
    any intermediate proxy logs) — so use only for the live-stream
    endpoint and the HTML page that bootstraps it. Document the
    tradeoff in the route docstrings.

    Header wins when both are present. Comparison uses
    ``hmac.compare_digest`` so the check is constant-time.
    """
    import main  # local import dodges module-load circular import
    settings = main.settings
    effective = x_api_key or request.query_params.get("api_key")
    if settings.api_key:
        if not effective or not hmac.compare_digest(effective, settings.api_key):
            raise HTTPException(
                status_code=401, detail="Invalid or missing API key"
            )
        return
    if settings.dashboard_allow_anonymous:
        return
    raise HTTPException(status_code=503, detail="Dashboard auth not configured")
