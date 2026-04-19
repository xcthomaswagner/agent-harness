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
    out, _n = redact(text)
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
    out, _n = redact(text)
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
    out, _n = redact(text)
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
    out, _n = redact(text)
    assert '"password":"[REDACTED]"' in out
    assert "hunter2" not in out


def test_json_password_negative() -> None:
    text = '{"password_reset_link": "https://example.com/reset"}'
    out, _n = redact(text)
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
    out, _n = redact(text)
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
    out, _n = redact(text)
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
    out, _n = redact(text)
    assert "[PRIVATE_KEY_REDACTED]" in out
    assert "b3BlbnNzaC1rZXk" not in out


def test_jwt_redacted() -> None:
    text = (
        "token=eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "abcdefghijklmnopqrstuvwxyz012345"
    )
    out, _n = redact(text)
    assert "[JWT_REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_aws_access_key_id_redacted() -> None:
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    out, _n = redact(text)
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
    out, _n = redact(text)
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
    first, _first_n = redact(text)
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


def test_idempotent_json_field_wrapping_entropy_placeholder() -> None:
    # Bug 2 regression guard: a JSON field (access_token / password /
    # api_key) that first gets flagged by the entropy pass — so the value
    # becomes e.g. ``[FLAGGED_ENTROPY_44_REDACTED]`` — must NOT be re-flagged
    # by the line pass on a second run. Previously the negative lookahead
    # only matched ``[REDACTED]`` literally, so a value wrapping an entropy
    # placeholder looked like an unredacted field and got re-redacted,
    # breaking the ``/admin/re-redact`` idempotency canary.
    novel_blob = "aB3xYz0QRS7tUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz"
    # ``password`` is the cleanest case: no line pattern catches it, so
    # only the entropy pass fires on the first run. This forces the second
    # run to rely purely on the updated negative lookahead.
    text = f'{{"password": "{novel_blob}"}}'
    first, first_n = redact(text)
    assert first_n >= 1
    assert "REDACTED" in first
    second, second_n = redact(first)
    assert second == first, (
        "JSON field wrapping an entropy placeholder must be a fixed point"
    )
    assert second_n == 0


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


# ---------------------------------------------------------------------------
# Phase 2 — new pattern coverage
# ---------------------------------------------------------------------------


def test_ado_pat_label_redacted() -> None:
    # ADO PATs are 52-char base64-ish strings. We anchor on the label.
    pat = "A" * 52
    text = f"ADO_PAT={pat} keep-this"
    out, n = redact(text)
    assert pat not in out
    assert "[ADO_PAT_REDACTED]" in out
    assert "keep-this" in out
    assert n >= 1


def test_ado_pat_azdo_label_also_caught() -> None:
    # The pattern is ``(?i)(ado[_-]?pat|azdo)[_=:\s]+...``. Verify AZDO
    # variant also fires.
    pat = "B" * 52
    text = f"AZDO_PAT: {pat}"
    out, _ = redact(text)
    assert pat not in out
    assert "[ADO_PAT_REDACTED]" in out


def test_jira_cloud_api_token_redacted() -> None:
    # ATATT prefix + 40+ base64url chars — real shape of a Jira Cloud
    # API token.
    token = "ATATT" + "x" * 48
    text = f"Authorization: Basic user:{token}"
    out, n = redact(text)
    assert token not in out
    assert "[JIRA_TOKEN_REDACTED]" in out
    assert n >= 1


def test_env_file_style_token_redacted() -> None:
    # KEY ending in TOKEN / SECRET / KEY / PAT / PASSWORD / CREDENTIAL
    # with any value → value replaced. Covers most ``.env`` leaks.
    text = (
        "MY_API_TOKEN=abc123secretvalue\n"
        "DB_PASSWORD=hunter2\n"
        "STRIPE_SECRET=not-really-a-stripe-key\n"
        "USER_CREDENTIAL=somestuff"
    )
    out, n = redact(text)
    assert "abc123secretvalue" not in out
    assert "hunter2" not in out
    assert "not-really-a-stripe-key" not in out
    assert "somestuff" not in out
    # Four assignments → four redactions.
    assert n >= 4


def test_env_file_negative_normal_assignment_preserved() -> None:
    # Var names that don't end in a secret suffix should pass through.
    text = "DEBUG=true\nLOG_LEVEL=info\nHOST=example.com"
    out, n = redact(text)
    assert out == text
    assert n == 0


def test_aws_secret_access_key_redacted() -> None:
    # 40-char base64 value anchored on the key name.
    secret = "A" * 40
    text = f"aws_secret_access_key = {secret}"
    out, n = redact(text)
    assert secret not in out
    assert "[AWS_SECRET_REDACTED]" in out
    assert n >= 1


def test_stripe_live_key_redacted() -> None:
    # Built at runtime so source-file scanners (GitHub push protection)
    # don't flag a 24-char payload after ``sk_live_`` as a real key.
    payload = "abcdefghijklmnopqrstuvwx"
    text = f"key=sk_{'live'}_{payload}"
    out, n = redact(text)
    assert "sk_live_[REDACTED]" in out
    assert payload not in out
    assert n >= 1


def test_stripe_test_key_redacted() -> None:
    payload = "abcdefghijklmnopqrstuvwx"
    text = f"key=sk_{'test'}_{payload}"
    out, _ = redact(text)
    assert "sk_test_[REDACTED]" in out


def test_twilio_sid_redacted() -> None:
    # AC + 32 hex chars.
    sid = "AC" + "a" * 32
    text = f"twilio_sid={sid}"
    out, n = redact(text)
    assert sid not in out
    assert "[TWILIO_SID_REDACTED]" in out
    assert n >= 1


def test_url_with_credentials_redacted() -> None:
    # ``https://user:password@host/path`` - password portion masked.
    url = "Cloning https://ado-agent:secretpat123@dev.azure.com/acme/_git/repo"
    out, n = redact(url)
    assert "secretpat123" not in out
    assert "https://[REDACTED]@dev.azure.com" in out
    assert n >= 1


def test_url_with_credentials_http_scheme_also_redacted() -> None:
    # The HTTP variant must also hit the pattern.
    url = "http://user:pass@example.com/"
    out, n = redact(url)
    assert "pass" not in out.split("@")[0].split(":", 2)[-1]
    assert n >= 1


def test_combined_bundle_with_ado_and_jira_tokens() -> None:
    """Diagnostic bundle regression: a mock bundle body containing an
    ADO PAT and a Jira Cloud API token must have both masked on a
    single call to redact()."""
    ado_pat = "P" * 52
    jira_token = "ATATT" + "J" * 60
    body = (
        "# Diagnostic dump\n"
        "error_context:\n"
        f"  ADO_PAT={ado_pat}\n"
        f"  Authorization: Basic user:{jira_token}\n"
    )
    out, n = redact(body)
    assert ado_pat not in out
    assert jira_token not in out
    assert "[ADO_PAT_REDACTED]" in out
    assert "[JIRA_TOKEN_REDACTED]" in out
    assert n >= 2


# ---------------------------------------------------------------------------
# Recursive tracer redaction (nested dict/list walking)
# ---------------------------------------------------------------------------


def test_tracer_recursive_redaction_nested_dict() -> None:
    """Regression: the previous tracer code only redacted a fixed list of
    top-level string fields. Secrets nested inside dicts (``error_context``
    → ``body`` → sk_live_...) went to disk verbatim. The recursive walker
    must catch them."""
    from tracer import redact_entry_in_place

    # Test fixtures built from components at runtime so GitHub's
    # secret-scanner doesn't flag this test file itself as leaking a
    # "real" Stripe / GitHub / bearer key.
    stripe_payload = "abcdefghijklmnopqrstuvwx"
    stripe_fake = f"sk_{'live'}_{stripe_payload}"
    bearer_payload = "abcdefghijklmnopqrstuvwxyz012345"
    bearer_fake = f"{'Bearer'} {bearer_payload}"
    ghp_payload = "abcdefghijklmnopqrstuvwxyz0123456789"
    ghp_fake = f"gh{'p'}_{ghp_payload}"
    entry: dict[str, object] = {
        "trace_id": "t-abc",
        "ticket_id": "TEST-1",
        "phase": "planning",
        "event": "error",
        "error_context": {
            "body": stripe_fake,
            "headers": {"authorization": bearer_fake},
        },
        "nested_list": [{"stderr": ghp_fake}],
    }
    n = redact_entry_in_place(entry)
    # Identifier fields preserved — never redacted.
    assert entry["trace_id"] == "t-abc"
    assert entry["ticket_id"] == "TEST-1"
    assert entry["phase"] == "planning"
    assert entry["event"] == "error"
    # All three secrets masked in place.
    ec = entry["error_context"]
    assert isinstance(ec, dict)
    assert stripe_fake not in str(ec)
    assert bearer_fake not in str(ec)
    nl = entry["nested_list"]
    assert isinstance(nl, list)
    assert ghp_fake not in str(nl)
    # At least three redactions (sk_live, bearer, ghp) happened.
    assert n >= 3


def test_tracer_recursive_redaction_skips_metadata_keys() -> None:
    """Ensure the skip-keys set guards against accidental mutation of
    identifier fields even if their values happen to look secret-ish
    to the entropy pass (high-entropy trace_ids are valid)."""
    from tracer import redact_entry_in_place

    # 12-hex trace_id — under the entropy threshold anyway, but if it
    # were a base64 UUID the skip set is the belt-and-braces guard.
    entry = {
        "trace_id": "ff1122334455",
        "ticket_id": "PROJ-999",
        "timestamp": "2026-04-17T12:00:00+00:00",
        "phase": "completion",
        "event": "Pipeline complete",
        "content": "no secrets here",
    }
    _ = redact_entry_in_place(entry)
    # Identifiers stable.
    assert entry["trace_id"] == "ff1122334455"
    assert entry["ticket_id"] == "PROJ-999"
    assert entry["timestamp"] == "2026-04-17T12:00:00+00:00"
    assert entry["phase"] == "completion"
    assert entry["event"] == "Pipeline complete"
