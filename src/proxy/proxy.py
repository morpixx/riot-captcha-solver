from __future__ import annotations

from urllib.parse import unquote, urlparse

ALLOWED = {"socks5", "socks5h", "http", "https"}


def parse_proxy(url: str | None) -> dict | None:
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "socks5://" + raw

    p = urlparse(raw)
    scheme = (p.scheme or "").lower()
    if scheme not in ALLOWED:
        raise ValueError(f"unsupported proxy scheme: {scheme!r} (use socks5/http/https)")
    if not p.hostname or not p.port:
        raise ValueError("proxy must be host:port")

    proxy: dict = {"server": f"{scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = unquote(p.username)
    if p.password:
        proxy["password"] = unquote(p.password)
    return proxy


def describe(proxy: dict | None) -> str:
    if not proxy:
        return "off"
    server = proxy.get("server", "")
    if proxy.get("username"):
        return server.replace("://", "://***@", 1)
    return server
