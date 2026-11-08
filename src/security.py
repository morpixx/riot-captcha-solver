"""Redaction and log-safe identifiers.

Proxy credentials must never reach logs, SQLite, webhooks or HTTP responses.
All code that reports an exception or stores an error message should pass it
through :func:`redact` first.
"""

from __future__ import annotations

import hashlib
import re

_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@")
_SENSITIVE_PAIR = re.compile(
    r"(?i)(?P<prefix>['\"]?(?:password|passwd|username|user|secret|"
    r"api[_-]?key|authorization)['\"]?\s*[:=]\s*)"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s,;}]+)"
)
_TOKEN_PAIR = re.compile(
    r"(?i)(?<!no )(?P<prefix>['\"]?token['\"]?\s*[:=]\s*)"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)(\bauthorization\s*[:=]\s*)bearer\s+\S+")


def mask_proxy(raw: str | None) -> str:
    """Return a proxy URL with credentials replaced by ***.

    Accepts both full URLs and the shorthand user:pass@host:port used by
    the CLI. None/empty values render as off.
    """
    value = str(raw or "").strip()
    if not value:
        return "off"
    if "://" not in value:
        value = "socks5://" + value
    return _URL_USERINFO.sub(r"\g<scheme>***@", value)


def _mask_sensitive_pair(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{match.group('prefix')}{quote}***{quote}"


def redact(text: object, *secrets: str) -> str:
    """Remove credentials from arbitrary text.

    secrets may contain raw proxy URLs that appear verbatim in an
    exception. They are replaced by their masked form before the generic
    patterns run.
    """
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), mask_proxy(str(secret)))
    value = _BEARER.sub(r"\1Bearer ***", value)
    value = _URL_USERINFO.sub(r"\g<scheme>***@", value)
    value = _SENSITIVE_PAIR.sub(_mask_sensitive_pair, value)
    return _TOKEN_PAIR.sub(_mask_sensitive_pair, value)


def safe_error(exc: BaseException, *secrets: str, limit: int = 500) -> str:
    """Format an exception without leaking credentials, capped for storage."""
    return redact(f"{type(exc).__name__}: {exc}", *secrets)[:limit]


def email_tag(email: str) -> str:
    """Short, stable log identifier for an email address."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:10]


def email_domain(email: str) -> str:
    """Return only the domain part, safe to use for aggregate diagnostics."""
    local, sep, domain = str(email).rpartition("@")
    return domain.strip().lower() if sep and local and domain else "invalid"


__all__ = ["email_domain", "email_tag", "mask_proxy", "redact", "safe_error"]
