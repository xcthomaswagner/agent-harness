"""SSRF guard for attachment / CDN fetches.

Before any HTTP request for attacker-controlled URLs (attachment links
in Jira/ADO webhooks, Figma CDN image URLs) the URL runs through
``validate_attachment_url``. Any URL that:

  * doesn't parse as http(s),
  * uses HTTP for a non-localhost host (unless ``ATTACHMENT_ALLOW_HTTP``
    is set — test-only escape hatch),
  * has a hostname not in the caller-supplied allowlist (exact match
    OR ``.endswith(".{host}")`` so a CDN under a vendor's apex domain
    is also accepted),
  * resolves (via DNS) to an RFC1918 / loopback / link-local / or other
    special-use IP range

is rejected with ``UnsafeAttachmentUrl``. Callers should catch the
exception, log the reason, and skip the fetch — do NOT swap in a
generic ``ValueError``, the adapter catchers rely on the specific
subclass to distinguish "URL blocked for safety" from a bogus URL
supplied by the webhook parser.

The guard exists because adapters used to fetch attachment URLs
through their authenticated httpx client — the one carrying
``Authorization: Basic email:PAT`` headers. An attacker who could
inject a link into a Jira issue (any external customer portal)
could point that link at an IMDS endpoint (``169.254.169.254``),
their own server, or a metadata service and receive the adapter's
PAT in the ``Authorization`` header on the outbound request.
Blocking the URL before the httpx call happens is the primary
mitigation. Separately, the adapters also use a second httpx client
without the auth headers for attachment fetches — so even if a URL
slipped past the guard, no credential would leak.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

__all__ = ["UnsafeAttachmentUrl", "validate_attachment_url"]


class UnsafeAttachmentUrl(ValueError):  # noqa: N818
    """Raised when an attachment URL fails the SSRF guard.

    Subclasses ValueError so existing try/except ValueError paths in
    adapters continue to work without needing a broader catch. Kept
    without the ``Error`` suffix because the class name encodes the
    positive condition (the URL is *unsafe*), not the exceptional
    condition (an *error* occurred). Ruff's N818 disagrees; the
    tradeoff favours readability at the raise sites.
    """


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Return True if ``host`` matches any entry in ``allowed_hosts``.

    A match is either:
      * exact case-insensitive string equality, or
      * ``host`` ends with ``.<allowed>``.

    The dot is required — accepting ``evilatlassian.net`` as a match
    for ``atlassian.net`` would defeat the whole point of the allowlist.
    Port numbers on the host are stripped before comparison; the
    caller's allowlist should contain hostnames, not netlocs.
    """
    if not host:
        return False
    lowered = host.lower()
    # Strip port number if present (e.g. "127.0.0.1:8765" → "127.0.0.1").
    if ":" in lowered:
        lowered = lowered.rsplit(":", 1)[0]
    for allowed in allowed_hosts:
        if not allowed:
            continue
        allow = allowed.lower()
        if lowered == allow or lowered.endswith("." + allow):
            return True
    return False


def _ip_is_safe(ip_str: str) -> tuple[bool, str]:
    """Return ``(is_safe, reason_if_unsafe)`` for a resolved IP literal.

    Rejects RFC1918 private ranges, loopback, link-local (including
    the cloud metadata IP ``169.254.169.254``), multicast, reserved,
    and unspecified addresses. Anything else is treated as
    internet-routable and accepted.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return (False, f"unparseable_ip:{ip_str}")
    # Order matters: ``169.254.169.254`` (cloud IMDS) is both
    # ``is_private`` and ``is_link_local`` under stdlib classification
    # — return the more specific label ``link_local`` so operators
    # reading logs can identify an IMDS bypass attempt vs. a plain
    # LAN target. Same for IPv6 link-local fe80::/10.
    if ip.is_loopback:
        return (False, "loopback")
    if ip.is_link_local:
        return (False, "link_local")
    if ip.is_private:
        return (False, "rfc1918")
    if ip.is_multicast:
        return (False, "multicast")
    if ip.is_reserved:
        return (False, "reserved")
    if ip.is_unspecified:
        return (False, "unspecified")
    return (True, "")


async def validate_attachment_url(
    url: str, allowed_hosts: list[str]
) -> None:
    """Validate ``url`` against the SSRF guard rules.

    Raises ``UnsafeAttachmentUrl`` on any of:
      - Empty URL
      - Non-http(s) scheme
      - HTTP scheme when the host isn't localhost and
        ``ATTACHMENT_ALLOW_HTTP`` env var is not truthy
      - Host not in ``allowed_hosts``
      - Any resolved IP falling in a blocked range

    The allowlist match happens BEFORE DNS resolution — if the
    host isn't in the allowlist, there's no reason to hit DNS.
    """
    if not url:
        raise UnsafeAttachmentUrl("empty url")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UnsafeAttachmentUrl(f"unparseable url: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeAttachmentUrl(f"unsupported scheme: {scheme!r}")

    host = parsed.hostname or ""
    if not host:
        raise UnsafeAttachmentUrl("missing host")

    # HTTP gate: only allow for loopback IPs during local testing, or
    # when an operator has explicitly opted in via env var.
    if scheme == "http":
        allow_http = os.environ.get("ATTACHMENT_ALLOW_HTTP", "").lower() in (
            "1", "true", "yes",
        )
        # Allow HTTP unconditionally for loopback — httpx's local test
        # fixtures use 127.0.0.1 and can't be made HTTPS without heroic
        # cert effort.
        is_loopback_host = host in ("127.0.0.1", "::1", "localhost")
        if not (allow_http or is_loopback_host):
            raise UnsafeAttachmentUrl("http scheme not allowed for non-localhost")

    # Host allowlist.
    if not _host_allowed(host, allowed_hosts):
        raise UnsafeAttachmentUrl(
            f"host {host!r} not in allowlist {allowed_hosts!r}"
        )

    # DNS resolution — check every IP the hostname resolves to, reject
    # if any falls in a blocked range. A hostname that resolves to
    # multiple IPs (round-robin DNS) must have ALL resolutions safe, so
    # an attacker can't register a domain with both a public IP and an
    # internal one and have the public IP pass guard while the request
    # actually connects to the internal one later.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeAttachmentUrl(f"dns_failed: {exc}") from exc

    seen_ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        # socket.getaddrinfo returns sockaddr tuples where element [0]
        # is the address as string for AF_INET / AF_INET6. The stdlib
        # type stub lists the first element as ``str | int`` because
        # the AF_PACKET variant returns an int, but we never pass
        # AF_PACKET here (getaddrinfo without family defaults to
        # AF_INET / AF_INET6). Coerce to str to satisfy mypy and
        # handle the unreachable int case without a surprise.
        raw_addr = sockaddr[0]
        ip_str = str(raw_addr)
        if ip_str in seen_ips:
            continue
        seen_ips.add(ip_str)
        ok, reason = _ip_is_safe(ip_str)
        if not ok:
            raise UnsafeAttachmentUrl(f"blocked_ip:{ip_str}:{reason}")
