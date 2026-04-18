"""Auth dependencies for FastAPI routes.

Extracted from ``main.py`` so router modules (``webhooks.py``,
``trace_bundle.py``, ``completion.py``) can attach the same
``Depends(_require_api_key)`` without importing from ``main`` — which
would circular-import back through the routers that ``main`` mounts.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException


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
