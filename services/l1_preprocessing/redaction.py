"""Secret redaction for trace bundles and log lines.

This module implements a three-pass redactor used before trace data leaves the
L1 service — either via the bundle endpoint or when consolidated traces are
written to disk. The passes run in a fixed order:

1. **Block pass** — multi-line credential shapes (PEM private keys, JWTs,
   AWS key pairs, GCP service-account JSON). Run against the full text with
   ``re.DOTALL`` so patterns can span newlines.
2. **Line pass** — single-line credential shapes (Anthropic/GitHub/GitLab/
   Slack tokens, Bearer headers, embedded basic auth in URLs, JSON fields).
   Run against each line individually to keep replacements scoped.
3. **Entropy pass** — a fallback that flags any remaining ≥40-char substring
   with Shannon entropy ≥4.5 bits/char as a likely secret. Catches new
   credential shapes we haven't written explicit patterns for yet.

Passes 2 and 3 are fused into a single per-line loop in ``redact()``: there's
no cross-line state between them and the entropy pass takes each already-line-
redacted line as-is. Keeping them conceptually separate in the docstring is
intentional — new contributors should reason about them as distinct stages
even though the implementation runs both on each line before moving on.

The redactor is designed to be **idempotent**: ``redact(redact(x)[0])`` must
produce the same text as ``redact(x)`` and report zero new redactions on the
second call. The POST /admin/re-redact admin endpoint depends on this — it
reruns redaction over already-redacted bundles after pattern updates.

Idempotency is achieved by ensuring every replacement placeholder contains the
substring ``REDACTED`` and is short enough that it cannot re-match any line
pattern. The entropy pass explicitly skips candidate substrings containing
``REDACTED`` — the check is per-candidate, not per-line, so the entropy pass
still runs on novel high-entropy tokens that happen to share a line with an
already-redacted placeholder.

When adding new patterns:
- Keep the replacement shorter than the minimum match length of the pattern.
- Make sure the replacement contains ``REDACTED``.
- Add both a positive and a negative test in ``test_redaction.py``.
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple

__all__ = ["redact"]


# ---------------------------------------------------------------------------
# Block patterns — run with re.DOTALL against the full text
# ---------------------------------------------------------------------------


class _BlockPattern(NamedTuple):
    pattern: re.Pattern[str]
    replacement: str


_BLOCK_PATTERNS: list[_BlockPattern] = [
    # PEM private keys: RSA, EC, OpenSSH, or plain. The backreference forces
    # the BEGIN and END markers to agree on the key type.
    _BlockPattern(
        re.compile(
            r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"
            r".*?"
            r"-----END \1PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[PRIVATE_KEY_REDACTED]",
    ),
    # GCP service-account JSON blobs. We look for ``"type": "service_account"``
    # paired with a ``"private_key"`` field anywhere in the enclosing object.
    # This is intentionally greedy within the JSON object braces.
    _BlockPattern(
        re.compile(
            r"\{[^{}]*?\"type\"\s*:\s*\"service_account\"[^{}]*?"
            r"\"private_key\"\s*:\s*\"[^\"]*\"[^{}]*?\}",
            re.DOTALL,
        ),
        "[GCP_SERVICE_ACCOUNT_REDACTED]",
    ),
    # Alt ordering: private_key appears before the type marker.
    _BlockPattern(
        re.compile(
            r"\{[^{}]*?\"private_key\"\s*:\s*\"[^\"]*\"[^{}]*?"
            r"\"type\"\s*:\s*\"service_account\"[^{}]*?\}",
            re.DOTALL,
        ),
        "[GCP_SERVICE_ACCOUNT_REDACTED]",
    ),
    # JWTs — base64url header.payload.signature. Min-length constraints on
    # each segment avoid matching things like ``eyJ.a.b``.
    _BlockPattern(
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "[JWT_REDACTED]",
    ),
    # AWS access key IDs. The secret-key pairing is handled by the line pass
    # below, but the access key ID alone is enough of a signal to redact.
    _BlockPattern(
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "[AWS_KEY_REDACTED]",
    ),
]


# ---------------------------------------------------------------------------
# Line patterns — run per-line
# ---------------------------------------------------------------------------


class _LinePattern(NamedTuple):
    pattern: re.Pattern[str]
    replacement: str


_LINE_PATTERNS: list[_LinePattern] = [
    # Anthropic API keys.
    _LinePattern(re.compile(r"sk-ant-[\w-]{30,}"), "sk-ant-[REDACTED]"),
    # Classic GitHub personal-access tokens.
    _LinePattern(re.compile(r"ghp_[A-Za-z0-9]{30,}"), "ghp_[REDACTED]"),
    # Fine-grained GitHub PATs.
    _LinePattern(re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "github_pat_[REDACTED]"),
    # GitHub OAuth tokens (gho_) and user-to-server (ghu_, ghs_, ghr_).
    _LinePattern(re.compile(r"gh[ousr]_[A-Za-z0-9]{30,}"), "gh_[REDACTED]"),
    # GitLab personal-access tokens.
    _LinePattern(re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "glpat-[REDACTED]"),
    # Slack tokens — xoxb, xoxa, xoxp, xoxr, xoxs.
    _LinePattern(re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox[SLACK_REDACTED]"),
    # Salesforce session IDs (OrgID!SessionToken). Org ID prefix is 00D.
    # The token body after the ``!`` is base64-ish and can include
    # ``+``, ``/``, ``=``, ``-`` — the character class has to admit all of
    # them or the match truncates and leaks the tail.
    _LinePattern(
        re.compile(r"00D[A-Za-z0-9]{12}![A-Za-z0-9+/=._-]+"),
        "[SF_TOKEN_REDACTED]",
    ),
    # Bearer authentication headers. Case-insensitive — covers ``Bearer``,
    # ``bearer``, and shouty ``BEARER``.
    _LinePattern(
        re.compile(r"bearer\s+[A-Za-z0-9+/._=-]{20,}", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    # Git / HTTP URLs with embedded basic auth credentials.
    _LinePattern(
        re.compile(r"https://[^@\s/:]+:[^@\s/]+@"),
        "https://[REDACTED]@",
    ),
    # JSON ``"access_token"`` / ``"accessToken"`` / ``"AccessToken"`` fields.
    # Case-insensitive so PascalCase (.NET / Azure) payloads are covered.
    # The negative lookahead matches any value whose contents already include
    # ``REDACTED`` anywhere. This keeps the pattern idempotent across ALL
    # placeholder shapes the redactor ever emits — the literal ``[REDACTED]``,
    # the block placeholders like ``[JWT_REDACTED]``, and entropy-pass
    # placeholders like ``[FLAGGED_ENTROPY_40_REDACTED]``. A narrower
    # lookahead would silently violate idempotency when a field was first
    # redacted by the entropy pass and later re-scanned by the line pass.
    _LinePattern(
        re.compile(
            r'"access_?token"\s*:\s*"(?![^"]*REDACTED[^"]*")[^"]+"',
            re.IGNORECASE,
        ),
        '"access_token":"[REDACTED]"',
    ),
    # JSON ``"password"`` fields, case-insensitive.
    _LinePattern(
        re.compile(
            r'"password"\s*:\s*"(?![^"]*REDACTED[^"]*")[^"]+"',
            re.IGNORECASE,
        ),
        '"password":"[REDACTED]"',
    ),
    # Generic ``"api_key"`` / ``"apiKey"`` / ``"ApiKey"`` JSON fields,
    # case-insensitive.
    _LinePattern(
        re.compile(
            r'"api_?key"\s*:\s*"(?![^"]*REDACTED[^"]*")[^"]+"',
            re.IGNORECASE,
        ),
        '"api_key":"[REDACTED]"',
    ),
    # Google API keys (AIza...).
    _LinePattern(re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[GOOGLE_API_KEY_REDACTED]"),
]


# ---------------------------------------------------------------------------
# Entropy pass
# ---------------------------------------------------------------------------


_ENTROPY_MIN_LEN = 40
_ENTROPY_THRESHOLD = 4.5

# Substrings of at least _ENTROPY_MIN_LEN consisting of token-ish characters
# (no whitespace, no quotes, no brackets). We scan each candidate for Shannon
# entropy and replace those over threshold.
_ENTROPY_CANDIDATE_RE = re.compile(rf"[A-Za-z0-9+/=._-]{{{_ENTROPY_MIN_LEN},}}")


def _shannon_entropy(s: str) -> float:
    """Return the Shannon entropy of ``s`` in bits/char.

    Empty strings return 0. A uniformly random 64-character base64 string
    scores around 5.8; hex strings around 4.0; UUIDs around 4.2; natural
    language around 3.5.
    """
    if not s:
        return 0.0
    length = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def _redact_entropy(line: str) -> tuple[str, int]:
    """Replace high-entropy substrings on a single line.

    Skips **individual candidate substrings** that are themselves redaction
    placeholders (they contain the literal ``REDACTED``). This is
    per-candidate, not per-line: a line like
    ``api_key=sk-ant-[REDACTED] session=<novel-high-entropy-blob>``
    still has the novel blob flagged by the entropy pass even though the
    line already contains a placeholder. An earlier version of this function
    skipped the entire line on ``"[REDACTED]" in line``, which was too
    coarse and let novel tokens leak when they shared a line with a
    previously-redacted token.
    """
    replacements: list[tuple[int, int, str]] = []
    for match in _ENTROPY_CANDIDATE_RE.finditer(line):
        candidate = match.group(0)
        # Per-candidate idempotency guard: if the candidate overlaps with
        # an existing placeholder, it already contains ``REDACTED`` and
        # must not be re-flagged.
        if "REDACTED" in candidate:
            continue
        if _shannon_entropy(candidate) >= _ENTROPY_THRESHOLD:
            placeholder = f"[FLAGGED_ENTROPY_{len(candidate)}_REDACTED]"
            replacements.append((match.start(), match.end(), placeholder))

    if not replacements:
        return line, 0

    # Apply in reverse so earlier offsets remain valid.
    out = line
    for start, end, placeholder in reversed(replacements):
        out = out[:start] + placeholder + out[end:]
    return out, len(replacements)


# TODO: File-type awareness. The reviewer (finding #6) noted that callers
# should be able to pass a ``source_path`` hint so the redactor can apply
# stricter handling to known sensitive files (``.env``, ``*.pem``,
# ``credentials.json``, etc.). That is a broader API change that ripples
# into commit 6 (bundle writer), so it is deferred. See the post-mortem
# observability plan, section on sensitive-file handling.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def redact(text: str) -> tuple[str, int]:
    """Redact known-credential shapes from ``text``.

    Returns ``(redacted_text, redaction_count)``. The count is the total
    number of patterns matched across all three passes — callers use it to
    set a "contained secrets" flag on the trace summary. Returns
    ``(text, 0)`` when nothing matched.

    This function is **idempotent**: running it a second time on its own
    output returns the same text and a count of 0. The re-redact admin
    endpoint relies on this property.
    """
    if not text:
        return text, 0

    total = 0

    # --- Pass 1: block patterns over the whole text ---
    current = text
    for block in _BLOCK_PATTERNS:
        new_text, n = block.pattern.subn(block.replacement, current)
        if n:
            total += n
            current = new_text

    # --- Passes 2 + 3: line patterns then entropy fallback, per line ---
    #
    # Previously split into two separate ``for line in ...`` loops with an
    # intermediate ``out_lines`` list. There's no cross-line state between
    # them — the entropy pass runs independently on each already-line-
    # redacted line — so one loop that runs both per line produces the
    # same output, allocates one list instead of two, and halves the
    # Python-level per-line loop overhead on the redaction hot path
    # (consolidation + bundle export + /admin/re-redact all go through
    # here on files that can reach thousands of lines).
    final_lines: list[str] = []
    for line in current.split("\n"):
        redacted_line = line
        for lp in _LINE_PATTERNS:
            redacted_line, n = lp.pattern.subn(lp.replacement, redacted_line)
            if n:
                total += n
        flagged, n = _redact_entropy(redacted_line)
        if n:
            total += n
        final_lines.append(flagged)

    return "\n".join(final_lines), total
