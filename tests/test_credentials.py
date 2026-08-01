"""Cookie normalization and token masking diagnostics."""

from __future__ import annotations

from init_config import (
    _is_masked_token,
    credential_shape,
    normalize_cookie,
)


def test_normalize_cookie_semicolon_format_is_unchanged():
    raw = "rand_info=abc; slave_sid=xyz; slave_bizuin=123"
    assert normalize_cookie(raw) == raw


def test_normalize_cookie_devtools_table_format():
    raw = "rand_info\tabc\nslave_sid\txyz\nslave_bizuin\t123"
    normalized = normalize_cookie(raw)
    assert normalized == "rand_info=abc; slave_sid=xyz; slave_bizuin=123"


def test_normalize_cookie_colon_and_quoted_values():
    raw = 'rand_info: "abc"; slave_sid: xyz'
    assert normalize_cookie(raw) == "rand_info=abc; slave_sid=xyz"


def test_normalize_cookie_deduplicates_names_keeping_first():
    raw = "rand_info=first; rand_info=second"
    assert normalize_cookie(raw) == "rand_info=first"


def test_normalize_cookie_drops_malformed_rows():
    raw = "rand_info=abc; not-a-pair; =empty"
    assert normalize_cookie(raw) == "rand_info=abc"


def test_masked_token_is_detected():
    for value in ("***", "*****", "<redacted>", "redacted", "[REDACTED]"):
        assert _is_masked_token(value), value
    assert _is_masked_token("1326459676") is False
    assert _is_masked_token("abc123") is False


def test_credential_shape_reports_masked_token():
    shape = credential_shape("rand_info=abc", "***")
    assert shape["token_masked"] is True
    assert shape["token_is_numeric"] is False
    assert shape["values_echoed"] is False
