"""Startup URL checker — validates reference documentation URLs are still accessible.

Runs once at service startup as a background task. Does not block the server.
Logs warnings for broken or redirected URLs so stale references are caught early.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger()

PROFILES_DIR = Path(__file__).resolve().parents[2] / "runtime" / "platform-profiles"
RESULTS_PATH = Path(__file__).resolve().parents[2] / "data" / "url-check.json"

# Domains that block automated HEAD requests (403) but work in browsers
_BOT_BLOCKED_DOMAINS = {"help.salesforce.com"}


def _extract_urls(text: str) -> list[str]:
    """Extract https URLs from markdown text."""
    return re.findall(r"https?://[^\s)>]+", text)


def _collect_reference_urls() -> dict[str, list[str]]:
    """Scan all platform profile REFERENCE_URLS.md files and extract URLs.

    Returns a dict of {profile_name: [url, ...]}
    """
    results: dict[str, list[str]] = {}
    for ref_file in PROFILES_DIR.glob("*/REFERENCE_URLS.md"):
        profile = ref_file.parent.name
        urls = _extract_urls(ref_file.read_text())
        if urls:
            results[profile] = urls
    return results


async def check_reference_urls() -> None:
    """Check all reference URLs and log/save results.

    Called once at startup. Uses HEAD requests with a short timeout
    to avoid blocking. Writes results to data/url-check.json.
    """
    url_map = _collect_reference_urls()
    if not url_map:
        logger.info("url_check_skipped", reason="no REFERENCE_URLS.md files found")
        return

    total = sum(len(urls) for urls in url_map.values())
    logger.info("url_check_started", profiles=list(url_map.keys()), total_urls=total)

    results: list[dict] = []
    broken = 0
    redirected = 0

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=10.0,
        headers={"User-Agent": "AgentHarness-URLChecker/1.0"},
    ) as client:
        for profile, urls in url_map.items():
            for url in urls:
                status = "unknown"
                status_code = 0
                redirect_url = ""
                try:
                    resp = await client.head(url)
                    status_code = resp.status_code
                    # Check if this domain is known to block bots
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc
                    is_bot_blocked = domain in _BOT_BLOCKED_DOMAINS

                    if 200 <= status_code < 300:
                        status = "ok"
                    elif status_code == 403 and is_bot_blocked:
                        status = "ok_bot_blocked"
                    elif 300 <= status_code < 400:
                        status = "redirect"
                        redirect_url = resp.headers.get("location", "")
                        redirected += 1
                        logger.warning(
                            "url_check_redirect",
                            profile=profile, url=url,
                            status_code=status_code,
                            redirect_to=redirect_url,
                        )
                    else:
                        status = "broken"
                        broken += 1
                        logger.warning(
                            "url_check_broken",
                            profile=profile, url=url,
                            status_code=status_code,
                        )
                except httpx.HTTPError as exc:
                    status = "error"
                    broken += 1
                    logger.warning(
                        "url_check_error",
                        profile=profile, url=url,
                        error=str(exc)[:200],
                    )

                results.append({
                    "profile": profile,
                    "url": url,
                    "status": status,
                    "status_code": status_code,
                    "redirect_url": redirect_url,
                })

    # Save results
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps({
        "checked_at": datetime.now(UTC).isoformat(),
        "total": total,
        "ok": total - broken - redirected,
        "broken": broken,
        "redirected": redirected,
        "results": results,
    }, indent=2))

    if broken or redirected:
        logger.warning(
            "url_check_complete",
            total=total, ok=total - broken - redirected,
            broken=broken, redirected=redirected,
            details=str(RESULTS_PATH),
        )
    else:
        logger.info("url_check_complete", total=total, all_ok=True)
