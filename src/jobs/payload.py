"""Validation of one job submission and its in-memory payload.

The payload holds cookies and proxy credentials. It never reaches SQLite;
only the derived proxy key is persisted.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import blocked_email_domains, env_flag
from ..proxy.proxy import parse_proxy
from ..security import redact
from .models import JobSpec

ALLOW_NO_PROXY = env_flag("ALLOW_NO_PROXY")


@dataclass(frozen=True)
class Payload:
    """Secrets needed to execute one job; kept in memory only."""

    cookies: list[dict]
    proxy: str
    new_email: str
    sitekey: str | None
    headless: bool | None


def proxy_key(parsed: dict) -> str:
    """Stable, non-secret identity for a proxy session.

    Residential providers often encode a sticky-session id in the password
    while keeping host and username equal, so the password participates in
    the hash. Raw credentials are never stored or logged.
    """
    raw = "|".join(
        (
            str(parsed.get("server", "")),
            str(parsed.get("username", "")),
            str(parsed.get("password", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def assert_public_proxy(parsed: dict) -> None:
    """Reject loopback/private proxy hosts before a browser is started."""
    host = urlparse(parsed.get("server", "")).hostname
    if not host:
        raise ValueError("proxy must be host:port")
    lowered = host.lower()
    if lowered in ("localhost", "localhost.localdomain", "ip6-localhost") or lowered.endswith(
        ".local"
    ):
        raise ValueError("proxy host must be a public address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname: DNS-проверку делаем на уровне инфраструктуры, не здесь
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError("proxy host must be a public address")


def sanitize(text: object, proxy: str | None = None) -> str:
    """Redact credentials and cap the stored error message."""
    return redact(text, proxy or "")[:1500]


def build_payload(spec: JobSpec) -> tuple[Payload, str]:
    """Validate one spec and derive its proxy deduplication key.

    Raises ValueError with a client-safe message when the spec cannot run.
    """
    parsed = parse_proxy(spec.proxy)
    if parsed is None:
        if not ALLOW_NO_PROXY:
            raise ValueError("proxy must not be empty")
        key = ""
    else:
        assert_public_proxy(parsed)
        key = proxy_key(parsed)
    if not spec.new_email or "@" not in spec.new_email:
        raise ValueError("new_email is invalid")
    domain = spec.new_email.rpartition("@")[2].strip().lower()
    blocked = blocked_email_domains()
    if any(domain == item or domain.endswith("." + item) for item in blocked):
        raise ValueError(f"new_email domain is blocked: {domain}")
    if not spec.cookies:
        raise ValueError("cookies must not be empty")
    payload = Payload(
        cookies=[dict(cookie) for cookie in spec.cookies],
        proxy=spec.proxy,
        new_email=spec.new_email,
        sitekey=spec.sitekey,
        headless=spec.headless,
    )
    if spec.allow_shared_proxy:
        key = ""
    return payload, key
