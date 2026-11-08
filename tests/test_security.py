from __future__ import annotations

from src.security import email_domain, mask_proxy, redact, safe_error


def test_mask_proxy_hides_credentials():
    assert mask_proxy("http://user:pass@gw.example:10000") == "http://***@gw.example:10000"
    assert mask_proxy("user:pass@gw.example:10000") == "socks5://***@gw.example:10000"
    assert mask_proxy("http://gw.example:10000") == "http://gw.example:10000"
    assert mask_proxy("") == "off"


def test_redact_removes_urls_and_dict_credentials():
    text = "failed http://user:pass@gw.example:10000 {'username': 'user', 'password': 'pass'}"
    cleaned = redact(text)

    assert "user:pass" not in cleaned
    assert "gw.example" in cleaned
    assert "pass'" not in cleaned
    assert "***" in cleaned


def test_redact_replaces_verbatim_and_bearer_secret():
    raw = "socks5://user:pass@gw.example:1080"
    cleaned = redact(f"proxy={raw} authorization: Bearer abc123", raw)

    assert "user:pass" not in cleaned
    assert "abc123" not in cleaned
    assert "***" in cleaned


def test_email_domain_is_safe_for_logs():
    assert email_domain("User@Gmail.COM") == "gmail.com"
    assert email_domain("not-an-email") == "invalid"
    assert email_domain("@example.com") == "invalid"


def test_safe_error_formats_and_caps():
    error = safe_error(ValueError("boom http://u:p@host:1"), limit=20)

    assert error.startswith("ValueError:")
    assert "u:p@" not in error
    assert len(error) <= 20


def test_redact_preserves_hcaptcha_error_after_no_token():
    cleaned = redact("RuntimeError: hCaptcha issued no token: 'challenge-error'")
    assert "challenge-error" in cleaned
    assert "no token" in cleaned


def test_redact_still_hides_token_values():
    cleaned = redact("token: 'P1_secret-value'")
    assert "P1_secret-value" not in cleaned
    assert "***" in cleaned
