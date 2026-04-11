"""Unit tests for the three-pass redaction module.

Covers every line pattern (positive + negative), every block pattern,
the entropy-based fallback (thresholds on length and bits/char), idempotency,
and combination cases where multiple pattern types fire in the same text.
"""

from __future__ import annotations

from redaction import _shannon_entropy, redact

# ---------------------------------------------------------------------------
# Line pattern — positive + negative per pattern
# ---------------------------------------------------------------------------


def test_anthropic_key_redacted() -> None:
    text = "api=sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef-xyz"
    out, n = redact(text)
    assert "sk-ant-[REDACTED]" in out
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    assert n >= 1


def test_anthropic_key_negative_surrounding_text_preserved() -> None:
    text = "The prefix sk-ant- alone is too short and should stay."
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_github_classic_pat_redacted() -> None:
    text = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    out, n = redact(text)
    assert "ghp_[REDACTED]" in out
    assert n >= 1


def test_github_classic_pat_negative() -> None:
    out, n = redact("ghp_short")
    assert out == "ghp_short"
    assert n == 0


def test_github_fine_grained_pat_redacted() -> None:
    text = "PAT=github_pat_11ABCDEFG0ABCDEFGHIJKL_0123456789abcdefghijklmnopqrstuvwxyzABCDEF"
    out, n = redact(text)
    assert "github_pat_[REDACTED]" in out
    assert n >= 1


def test_github_fine_grained_pat_negative() -> None:
    out, n = redact("github_pat_tooshort")
    assert out == "github_pat_tooshort"
    assert n == 0


def test_gitlab_pat_redacted() -> None:
    text = "GL=glpat-xxxxxxxxxxxxxxxxxxxx"
    out, n = redact(text)
    assert "glpat-[REDACTED]" in out
    assert n >= 1


def test_gitlab_pat_negative() -> None:
    out, n = redact("glpat-short")
    assert out == "glpat-short"
    assert n == 0


def test_slack_token_redacted() -> None:
    text = "slack=xoxb-1234567890-abcdefghij"
    out, n = redact(text)
    assert "xox[SLACK_REDACTED]" in out
    assert n >= 1


def test_slack_token_negative() -> None:
    out, n = redact("xox-short")
    assert out == "xox-short"
    assert n == 0


def test_salesforce_session_redacted() -> None:
    # 00D + 12 alphanumerics + ! + opaque token
    text = "Session: 00D5f000001abcD!ARwAQKq1abc.xyz_123"
    out, n = redact(text)
    assert "[SF_TOKEN_REDACTED]" in out
    assert n >= 1


def test_salesforce_session_with_base64_chars_fully_redacted() -> None:
    # Realistic Salesforce session token. The body after the ``!`` is
    # base64-ish and contains ``+``, ``/``, and ``=``. A character class
    # limited to ``[\w.]`` would truncate the match at the first ``+`` and
    # leave the tail in place — regression guard for finding #2.
    token_body = "ARwAQKq1+XYZ/abcdef=="
    text = f"Session: 00D5f000001abcD!{token_body}"
    out, n = redact(text)
    assert "[SF_TOKEN_REDACTED]" in out
    # None of the body characters should survive anywhere in the output.
    assert "ARwAQKq1" not in out
    assert "+XYZ" not in out
    assert "/abcdef" not in out
    assert "==" not in out
    assert n >= 1


def test_salesforce_session_negative() -> None:
    # Missing the ! separator — not a session token.
    out, n = redact("00Dabcdefghijkl")
    assert out == "00Dabcdefghijkl"
    assert n == 0


def test_bearer_header_redacted() -> None:
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
    out, n = redact(text)
    assert "Bearer [REDACTED]" in out
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out


def test_bearer_header_negative() -> None:
    out, n = redact("bearer short")
    # Short opaque string — no 20-char run, no match.
    assert out == "bearer short"
    assert n == 0


def test_bearer_all_caps_redacted() -> None:
    # Some shouty log formats emit ``BEARER`` in all caps — the pattern
    # must be case-insensitive so those lines don't leak. Regression guard
    # for finding #5.
    text = "Authorization: BEARER abcdefghijklmnopqrstuvwxyz012345"
    out, n = redact(text)
    assert "Bearer [REDACTED]" in out
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert n >= 1


def test_git_url_basic_auth_redacted() -> None:
    text = "origin https://alice:supersecret@github.com/foo/bar.git"
    out, n = redact(text)
    assert "https://[REDACTED]@github.com/foo/bar.git" in out
    assert "alice" not in out
    assert "supersecret" not in out


def test_git_url_without_credentials_negative() -> None:
    text = "origin https://github.com/foo/bar.git"
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_json_access_token_redacted() -> None:
    text = '{"access_token": "very-long-opaque-token-value"}'
    out, n = redact(text)
    assert '"access_token":"[REDACTED]"' in out
    assert "very-long-opaque-token-value" not in out


def test_json_access_token_negative() -> None:
    text = '{"access_point": "us-east-1"}'
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_json_access_token_pascalcase_redacted() -> None:
    # .NET / Azure / C# payloads commonly serialize with PascalCase field
    # names. The pattern must be case-insensitive so ``AccessToken`` is
    # caught along with ``access_token`` and ``accessToken``. Regression
    # guard for finding #3.
    text = '{"AccessToken": "very-long-opaque-pascalcase-token"}'
    out, n = redact(text)
    assert '"access_token":"[REDACTED]"' in out
    assert "very-long-opaque-pascalcase-token" not in out
    assert n >= 1


def test_json_password_redacted() -> None:
    text = '{"password": "hunter2"}'
    out, n = redact(text)
    assert '"password":"[REDACTED]"' in out
    assert "hunter2" not in out


def test_json_password_negative() -> None:
    text = '{"password_reset_link": "https://example.com/reset"}'
    out, n = redact(text)
    # The link itself may or may not match, but the field name alone
    # should not trigger the password-field replacement.
    assert '"password":"[REDACTED]"' not in out


def test_json_password_all_caps_redacted() -> None:
    # All-caps field names appear in some log formats — regression guard
    # for finding #3 (case-insensitive JSON field names).
    text = '{"PASSWORD": "hunter2"}'
    out, n = redact(text)
    assert '"password":"[REDACTED]"' in out
    assert "hunter2" not in out
    assert n >= 1


def test_json_api_key_redacted() -> None:
    text = '{"api_key": "service-account-abc-123"}'
    out, n = redact(text)
    assert '"api_key":"[REDACTED]"' in out
    assert "service-account-abc-123" not in out


def test_json_api_key_negative() -> None:
    text = '{"api_version": "2025-01-01"}'
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_json_api_key_pascalcase_redacted() -> None:
    # PascalCase ``ApiKey`` is common in .NET payloads. Regression guard
    # for finding #3 — must be case-insensitive.
    text = '{"ApiKey": "service-account-abc-123"}'
    out, n = redact(text)
    assert '"api_key":"[REDACTED]"' in out
    assert "service-account-abc-123" not in out
    assert n >= 1


def test_google_api_key_redacted() -> None:
    text = "key=AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    out, n = redact(text)
    assert "[GOOGLE_API_KEY_REDACTED]" in out
    assert "AAAAAAAAAAAAAAAAAAAAAAAAA" not in out


def test_google_api_key_negative() -> None:
    out, n = redact("key=AIza_too_short")
    assert out == "key=AIza_too_short"
    assert n == 0


# ---------------------------------------------------------------------------
# Block patterns
# ---------------------------------------------------------------------------


def test_rsa_private_key_block_redacted() -> None:
    text = (
        "config:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAx9abcdefghijklmnopqrstuv\n"
        "wXyZ0123456789/+ABCDEFGHIJKLMNOPQRSTUVWX\n"
        "-----END RSA PRIVATE KEY-----\n"
        "done"
    )
    out, n = redact(text)
    assert "[PRIVATE_KEY_REDACTED]" in out
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "done" in out
    assert n >= 1


def test_openssh_private_key_block_redacted() -> None:
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAG\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out, n = redact(text)
    assert "[PRIVATE_KEY_REDACTED]" in out
    assert "b3BlbnNzaC1rZXk" not in out


def test_jwt_redacted() -> None:
    text = (
        "token=eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "abcdefghijklmnopqrstuvwxyz012345"
    )
    out, n = redact(text)
    assert "[JWT_REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_aws_access_key_id_redacted() -> None:
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    out, n = redact(text)
    assert "[AWS_KEY_REDACTED]" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_truncated_rsa_key_body_entropy_pinned() -> None:
    # Pin behavior for reviewer finding #4 (truncated RSA keys without a
    # matching END marker). The block pattern requires paired BEGIN/END
    # markers, so a truncated key body falls through to the entropy pass.
    # That pass catches the long base64-ish body even though we have no
    # explicit truncated-key pattern. This test pins the current behavior
    # so we notice if it regresses before we add a dedicated pattern.
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA9aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYzabcdef\n"
        "(log truncated)"
    )
    out, n = redact(text)
    # The body should be replaced by the entropy fallback.
    assert "MIIEpAIBAAKCAQEA9aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYzabcdef" not in out
    assert "FLAGGED_ENTROPY_" in out
    assert n >= 1


def test_gcp_service_account_block_redacted() -> None:
    text = (
        '{"type": "service_account", '
        '"project_id": "my-project", '
        '"private_key_id": "abc123", '
        '"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----\\n"}'
    )
    out, n = redact(text)
    assert "[GCP_SERVICE_ACCOUNT_REDACTED]" in out
    assert "my-project" not in out


# ---------------------------------------------------------------------------
# Entropy pass
# ---------------------------------------------------------------------------


def test_entropy_plain_english_not_redacted() -> None:
    text = "The quick brown fox jumps over the lazy dog repeatedly throughout the afternoon."
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_entropy_high_entropy_base64_redacted() -> None:
    # 44-character base64-ish blob — well above threshold.
    blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    assert len(blob) >= 40
    assert _shannon_entropy(blob) >= 4.5
    text = f"mystery token {blob} end"
    out, n = redact(text)
    assert blob not in out
    assert "FLAGGED_ENTROPY_" in out
    assert "REDACTED" in out
    assert n >= 1


def test_entropy_hex_hash_not_redacted() -> None:
    # 40-char SHA-1 hex digest — entropy ~4.0, below threshold.
    digest = "a94a8fef8c17ca4a3b62afe6f3e9b6dcab27d8e2"
    assert len(digest) == 40
    assert _shannon_entropy(digest) < 4.5
    text = f"commit {digest} landed"
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_entropy_short_high_entropy_not_redacted() -> None:
    # 39 chars — below length threshold even if entropy is high.
    blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStU"
    assert len(blob) == 39
    text = f"key {blob} end"
    out, n = redact(text)
    assert blob in out
    assert n == 0


def test_entropy_skips_lines_with_existing_redaction_placeholder() -> None:
    # A line containing [REDACTED] should not have its placeholder re-flagged
    # by the entropy pass. (The check is per-candidate: any candidate
    # substring that contains ``REDACTED`` is skipped, so the placeholder
    # survives idempotent rerun.)
    text = "token=sk-ant-" + "A" * 35 + " trailing"
    first, first_n = redact(text)
    second, second_n = redact(first)
    assert first == second
    assert second_n == 0


def test_entropy_fires_after_line_pattern_on_same_line() -> None:
    # Regression guard for finding #1. An earlier version of the entropy
    # pass bailed out on the entire line as soon as it contained the string
    # ``[REDACTED]``. That was too coarse: a line carrying both a known-shape
    # token (caught by the line pass) and a novel high-entropy token (only
    # catchable by the entropy pass) would have the novel token leak.
    novel_blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    assert len(novel_blob) >= 40
    assert _shannon_entropy(novel_blob) >= 4.5
    text = f"api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA session={novel_blob}"
    out, n = redact(text)
    # The Anthropic key must be replaced by the line pass.
    assert "sk-ant-[REDACTED]" in out
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in out
    # The novel 44-char session blob must be caught by the entropy pass
    # even though the same line already contains a placeholder.
    assert novel_blob not in out
    assert "FLAGGED_ENTROPY_" in out
    # Two redactions on the one line: the line-pattern hit and the entropy hit.
    assert n >= 2


def test_entropy_idempotent_on_mixed_line_pattern_and_entropy() -> None:
    # Second-pass guarantee for the finding-#1 case: once the line has
    # been redacted, running ``redact`` on the output must be a fixed point.
    novel_blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    text = f"api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA session={novel_blob}"
    first, _ = redact(text)
    second, second_n = redact(first)
    assert first == second
    assert second_n == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_line_pattern() -> None:
    text = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    first, _ = redact(text)
    second, n = redact(first)
    assert first == second
    assert n == 0


def test_idempotent_block_pattern() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabcdefghij\n"
        "-----END RSA PRIVATE KEY-----"
    )
    first, _ = redact(text)
    second, n = redact(first)
    assert first == second
    assert n == 0


def test_idempotent_entropy_flag() -> None:
    blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    text = f"secret {blob}"
    first, _ = redact(text)
    second, n = redact(first)
    assert first == second
    assert n == 0


def test_idempotent_across_multiple_pattern_types() -> None:
    text = (
        "key=sk-ant-" + "A" * 40 + "\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
        '{"password": "hunter2"}'
    )
    first, _ = redact(text)
    second, n = redact(first)
    assert first == second
    assert n == 0


# ---------------------------------------------------------------------------
# Combination + edge cases
# ---------------------------------------------------------------------------


def test_combination_rsa_and_bearer_and_entropy() -> None:
    blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabc\n"
        "-----END RSA PRIVATE KEY-----\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
        "X-Auth: Bearer zyxwvutsrqponmlkjihgfedcba987654\n"
        f"blob {blob} end"
    )
    out, n = redact(text)
    assert "[PRIVATE_KEY_REDACTED]" in out
    # Two Bearer tokens + one RSA key + one entropy flag = 4 redactions.
    assert n >= 4
    assert "MIIEpAIBAAKCAQEAabc" not in out
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert blob not in out


def test_empty_string() -> None:
    out, n = redact("")
    assert out == ""
    assert n == 0


def test_whitespace_only() -> None:
    out, n = redact("   \n\t  \n")
    assert out == "   \n\t  \n"
    assert n == 0


def test_no_secrets_count_is_zero() -> None:
    text = "Just a normal log line with no secrets in it whatsoever."
    out, n = redact(text)
    assert out == text
    assert n == 0


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_shannon_entropy_empty() -> None:
    assert _shannon_entropy("") == 0.0


def test_shannon_entropy_uniform_chars() -> None:
    # All same character → 0 entropy.
    assert _shannon_entropy("aaaaaaaa") == 0.0


def test_shannon_entropy_ordering() -> None:
    # Natural language has lower entropy than a random base64-ish blob.
    english = "the quick brown fox jumps over the lazy dog"
    blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    assert _shannon_entropy(english) < _shannon_entropy(blob)
