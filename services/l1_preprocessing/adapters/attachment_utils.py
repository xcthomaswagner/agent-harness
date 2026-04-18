"""Shared attachment utilities used by Jira and ADO adapters."""

from __future__ import annotations

from pathlib import Path


def sanitize_attachment_filename(raw: str) -> str | None:
    """Turn an untrusted webhook filename into a safe basename or None.

    Rejects empty strings, ``.``, ``..``, anything containing NUL bytes,
    and (defence in depth) any value whose basename doesn't equal the
    input after slashes are stripped. The returned value is always a
    bare filename with no directory components — safe to use as the
    right-hand side of ``dest / name`` and write to disk.

    The caller should still verify the resolved path is inside the
    intended destination directory; this helper handles the common
    cases (path traversal via ``..``, absolute paths, NUL bytes) but
    isn't a substitute for the resolve()/relative_to() check.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if "\x00" in raw:
        return None
    # Take the last path component. ``Path("/etc/passwd").name`` ->
    # ``passwd``; ``Path("../foo").name`` -> ``foo``; ``Path("..").name``
    # -> ``..`` which we reject below.
    name = Path(raw).name
    if not name or name in (".", ".."):
        return None
    return name
