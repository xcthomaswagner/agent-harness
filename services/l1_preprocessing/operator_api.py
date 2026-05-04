"""Operator dashboard mount — serves the Preact SPA at ``/operator``.

The SPA lives at ``services/l1_preprocessing/operator_static/`` (built by
esbuild from ``services/operator_ui/src/``). FastAPI serves three kinds of
requests under ``/operator``:

1. ``GET /operator/`` and ``GET /operator/<anything>`` → renders
   ``index.html`` with ``DASHBOARD_API_KEY`` injected into the
   ``<meta name="operator-api-key">`` tag. Any path that is not a known
   static file returns the shell so client-side routing works
   (``/operator/traces/HARN-123`` serves the SPA, then the SPA's router
   shows the Trace Detail view).

2. ``GET /operator/operator.js`` / ``/operator/tokens.css`` /
   ``/operator/build.json`` → serves the static asset from disk.

3. ``/api/operator/*`` → JSON endpoints for the dashboard. Those land
   in subsequent commits alongside the views that consume them.

Auth: the shell route accepts ``?api_key=`` (same as the SSE route)
because a browser page load cannot attach custom headers. The key is
injected into the HTML at render time and cached in the SPA's meta
tag for subsequent fetch/SSE calls. Static assets are unprotected so
browsers can load them after the shell establishes the session.

Key handling: the key is injected as literal HTML text content, which
means any ``<`` / ``&`` / ``"`` in a malformed configured key would break
the meta tag. We escape the key before injection to protect against
that, not against prompt-style injection (the key is operator-owned).
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from auth import _require_dashboard_auth_query_or_header

router = APIRouter()

OPERATOR_STATIC_DIR = Path(__file__).resolve().parent / "operator_static"

_JS_MIME = "application/javascript; charset=utf-8"
_CSS_MIME = "text/css; charset=utf-8"
_JSON_MIME = "application/json; charset=utf-8"
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _static_file(path: Path, media_type: str) -> FileResponse:
    if not path.is_file():
        return FileResponse(path, status_code=404)
    return FileResponse(
        path,
        media_type=media_type,
        headers=_NO_STORE_HEADERS,
    )


def _render_shell(api_key: str) -> HTMLResponse:
    """Return the SPA shell with the API key injected into the meta tag.

    ``operator_static/index.html`` carries an empty
    ``<meta name="operator-api-key" content="">`` placeholder that this
    function rewrites with the escaped key. Cached never — the key must
    reflect the current env var.
    """
    template_path = OPERATOR_STATIC_DIR / "index.html"
    if not template_path.is_file():
        return HTMLResponse(
            status_code=503,
            content=(
                "<!doctype html><html><body><p>Operator SPA is not built. "
                "Run <code>npm run build</code> in "
                "<code>services/operator_ui/</code>.</p></body></html>"
            ),
        )
    shell = template_path.read_text(encoding="utf-8")
    injected = shell.replace(
        '<meta name="operator-api-key" content="">',
        f'<meta name="operator-api-key" content="{html.escape(api_key)}">',
    )
    return HTMLResponse(
        content=injected,
        headers=_NO_STORE_HEADERS,
    )


def _current_api_key() -> str:
    """Resolve the API key that the SPA should carry on subsequent calls.

    Reads ``main.settings.api_key`` at call time so settings changes during
    tests are picked up. Returns empty string when no key is configured
    (local-dev open-access path).
    """
    import main  # local import avoids module-load circular chain

    return getattr(main.settings, "api_key", "") or ""


@router.get(
    "/operator/operator.js",
    response_class=FileResponse,
    include_in_schema=False,
)
def _operator_js() -> FileResponse:
    return _static_file(OPERATOR_STATIC_DIR / "operator.js", _JS_MIME)


@router.get(
    "/operator/operator.css",
    response_class=FileResponse,
    include_in_schema=False,
)
def _operator_bundled_css() -> FileResponse:
    return _static_file(OPERATOR_STATIC_DIR / "operator.css", _CSS_MIME)


@router.get(
    "/operator/tokens.css",
    response_class=FileResponse,
    include_in_schema=False,
)
def _operator_tokens_css() -> FileResponse:
    # Tokens-only sheet kept for potential standalone consumers; the
    # real dashboard uses operator.css (which @imports tokens + every
    # component sheet).
    return _static_file(OPERATOR_STATIC_DIR / "tokens.css", _CSS_MIME)


@router.get(
    "/operator/build.json",
    response_class=FileResponse,
    include_in_schema=False,
)
def _operator_build() -> FileResponse:
    return _static_file(OPERATOR_STATIC_DIR / "build.json", _JSON_MIME)


@router.get(
    "/operator",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_dashboard_auth_query_or_header)],
)
@router.get(
    "/operator/",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_dashboard_auth_query_or_header)],
)
def _operator_index(_request: Request) -> Response:
    return _render_shell(_current_api_key())


# SPA fallback — any deep path under /operator/ that is not one of the
# known static assets returns the shell so client-side routing resolves.
# Defined LAST so FastAPI matches the explicit routes above first.
@router.get(
    "/operator/{path:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_dashboard_auth_query_or_header)],
)
def _operator_spa_fallback(path: str, _request: Request) -> Response:
    # Known asset names are handled by their explicit routes; any other
    # path falls through to the shell. Reject obvious asset look-alikes
    # that would otherwise serve HTML with a JS/CSS extension and
    # confuse the browser.
    lower = path.lower()
    if lower.endswith((".js", ".css", ".json", ".map", ".ico", ".png", ".svg")):
        return Response(status_code=404)
    return _render_shell(_current_api_key())
