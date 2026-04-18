"""Tests for the SSRF guard module (adapters/_url_guard.py).

Covers:
  - Host allowlist (exact match, subdomain match, wrong host)
  - Scheme gating (https OK, http blocked except localhost)
  - Blocked IP ranges: RFC1918, link-local (169.254.x.x / IMDS),
    loopback, multicast
  - DNS failure path
  - Per-adapter integration tests (Jira, ADO, Figma): verify the
    adapter's ``download_attachment`` / ``_render_frames`` path
    actually rejects hostile URLs instead of leaking credentials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters._url_guard import UnsafeAttachmentUrl, validate_attachment_url
from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from config import Settings
from figma_extractor import FigmaExtractor
from models import Attachment

# ---------------------------------------------------------------------------
# Direct validator tests
# ---------------------------------------------------------------------------


async def test_https_allowed_host_passes() -> None:
    # atlassian.net resolves to a public IP — must pass.
    await validate_attachment_url(
        "https://acme.atlassian.net/secure/attachment/1/x.png",
        ["atlassian.net"],
    )


async def test_wrong_host_rejected() -> None:
    with pytest.raises(UnsafeAttachmentUrl, match="not in allowlist"):
        await validate_attachment_url(
            "https://evil.example.com/x.png", ["atlassian.net"]
        )


async def test_subdomain_match_works_with_dotted_prefix() -> None:
    # example.atlassian.net should match allowlist entry "atlassian.net".
    await validate_attachment_url(
        "https://example.atlassian.net/x.png", ["atlassian.net"]
    )


async def test_allowlist_prefix_injection_rejected() -> None:
    # Bug class: substring match without dot boundary would accept
    # "evilatlassian.net" as a match for "atlassian.net". The guard
    # requires a ``.`` boundary, so this is rejected.
    with pytest.raises(UnsafeAttachmentUrl, match="not in allowlist"):
        await validate_attachment_url(
            "https://evilatlassian.net/x.png", ["atlassian.net"]
        )


async def test_http_scheme_rejected_for_public_host() -> None:
    with pytest.raises(UnsafeAttachmentUrl, match="http scheme not allowed"):
        await validate_attachment_url(
            "http://acme.atlassian.net/x.png", ["atlassian.net"]
        )


async def test_non_http_scheme_rejected() -> None:
    with pytest.raises(UnsafeAttachmentUrl, match="unsupported scheme"):
        await validate_attachment_url(
            "file:///etc/passwd", ["atlassian.net"]
        )


async def test_imds_link_local_ip_rejected() -> None:
    # Cloud metadata service IP — must be rejected even if a bogus
    # allowlist accepted the hostname.
    with pytest.raises(UnsafeAttachmentUrl, match="link_local"):
        await validate_attachment_url(
            "https://169.254.169.254/latest/meta-data/",
            ["169.254.169.254"],
        )


async def test_rfc1918_private_ip_rejected() -> None:
    # 10.0.0.5 is RFC1918 private — reject even if allowlisted.
    with pytest.raises(UnsafeAttachmentUrl, match="rfc1918"):
        await validate_attachment_url(
            "https://10.0.0.5/x.png", ["10.0.0.5"]
        )


async def test_loopback_ip_rejected_without_http_opt_in() -> None:
    # IPv4 loopback under https — host allowed, but it's loopback
    # so the guard rejects.
    with pytest.raises(UnsafeAttachmentUrl, match="loopback"):
        await validate_attachment_url(
            "https://127.0.0.1/x.png", ["127.0.0.1"]
        )


async def test_dns_failure_rejected() -> None:
    # A host that doesn't resolve — guard returns dns_failed.
    with pytest.raises(UnsafeAttachmentUrl, match="dns_failed"):
        await validate_attachment_url(
            "https://nope.does-not-exist-xyz123abc.com/x.png",
            ["nope.does-not-exist-xyz123abc.com"],
        )


# ---------------------------------------------------------------------------
# Adapter integration — verify PAT leak prevention end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def jira_settings() -> Settings:
    return Settings(
        jira_base_url="https://acme.atlassian.net",
        jira_api_token="super-secret-pat",
        jira_user_email="bot@acme.com",
    )


@pytest.fixture
def ado_settings() -> Settings:
    return Settings(
        ado_org_url="https://dev.azure.com/acme",
        ado_pat="super-secret-ado-pat",
    )


async def test_jira_download_blocks_imds_url(
    jira_settings: Settings, tmp_path: Path
) -> None:
    """Bug class: a webhook payload pointing a 'Jira attachment' at
    the cloud metadata IP would have caused the Jira adapter to POST
    the authenticated httpx client (with the PAT in the Authorization
    header) at IMDS. The SSRF guard must reject the URL without any
    HTTP call happening.
    """
    # Build an adapter with a mock attachment_client — if the guard
    # fails to fire, ``stream`` will be invoked and the test will
    # notice. Using AsyncMock spec=httpx.AsyncClient is safer than a
    # real client — we explicitly assert no request was made.
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    adapter = JiraAdapter(
        settings=jira_settings,
        attachment_client=mock_client,
    )
    evil = Attachment(
        filename="payload.png",
        url="http://169.254.169.254/latest/meta-data/iam/",
        content_type="image/png",
    )
    result = await adapter.download_attachment(evil, str(tmp_path))
    # Guard ran, fetch skipped.
    assert result.local_path == ""
    mock_client.stream.assert_not_called()


async def test_jira_download_blocks_off_allowlist_host(
    jira_settings: Settings, tmp_path: Path
) -> None:
    """A Jira attachment URL on an attacker-owned domain (not under
    ``atlassian.net``) must be rejected without the adapter ever
    making an outbound request — the PAT would otherwise ride along
    if a raw httpx.get were used."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    adapter = JiraAdapter(
        settings=jira_settings,
        attachment_client=mock_client,
    )
    evil = Attachment(
        filename="payload.png",
        url="https://attacker.example.com/stealer.png",
        content_type="image/png",
    )
    result = await adapter.download_attachment(evil, str(tmp_path))
    assert result.local_path == ""
    mock_client.stream.assert_not_called()


async def test_ado_download_blocks_imds_url(
    ado_settings: Settings, tmp_path: Path
) -> None:
    """Same SSRF guard for ADO — IMDS URL rejected without hitting
    the download client (which carries the ADO PAT)."""
    adapter = AdoAdapter(settings=ado_settings)
    # Stash a mock client as _download_client so we can assert the
    # guard runs before any stream call is attempted.
    mock_download = AsyncMock(spec=httpx.AsyncClient)
    adapter._download_client = mock_download

    evil = Attachment(
        filename="payload.png",
        url="http://169.254.169.254/metadata/instance",
        content_type="image/png",
    )
    result = await adapter.download_attachment(evil, str(tmp_path))
    assert result.local_path == ""
    mock_download.stream.assert_not_called()


async def test_figma_cdn_download_blocks_off_allowlist_host(
    tmp_path: Path,
) -> None:
    """Figma's image-API response could point to an attacker-controlled
    URL. The CDN-download path must run the SSRF guard against the
    URL before the cdn_client fetches it."""
    # Mock the Figma API calls to return an evil CDN URL.
    api_client = AsyncMock(spec=httpx.AsyncClient)
    dummy_req = httpx.Request("GET", "https://api.figma.com")

    file_resp = httpx.Response(
        200,
        json={
            "name": "Test",
            "document": {
                "children": [
                    {
                        "id": "0:1",
                        "type": "PAGE",
                        "children": [
                            {"id": "1:10", "type": "FRAME", "name": "Header"},
                        ],
                    }
                ]
            },
        },
        request=dummy_req,
    )
    image_api_resp = httpx.Response(
        200,
        json={
            "err": None,
            "images": {
                "1:10": "https://evil.attacker.com/steal.png",
            },
        },
        request=dummy_req,
    )

    async def routed_get(url: str, **kw: object) -> httpx.Response:
        if "/v1/files/" in url:
            return file_resp
        if "/v1/images/" in url:
            return image_api_resp
        raise AssertionError(f"unexpected api url: {url!r}")

    api_client.get = AsyncMock(side_effect=routed_get)

    cdn_client = AsyncMock(spec=httpx.AsyncClient)

    extractor = FigmaExtractor(
        api_token="test-token",
        client=api_client,
        cdn_client=cdn_client,
    )
    spec = await extractor.extract(
        "https://www.figma.com/file/abc/Test",
        image_dest_dir=str(tmp_path),
    )
    assert spec is not None
    # URL was off-allowlist, guard blocked — no CDN call made.
    assert spec.rendered_frames == []
    cdn_client.get.assert_not_called()
