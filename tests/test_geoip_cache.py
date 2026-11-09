"""GeoIP probe caching for sticky proxies (no network)."""

from __future__ import annotations

from src.browser import camoufox_browser as cb


class FakeResponse:
    status_code = 200
    text = "203.0.113.10"


class FakeSession:
    def __init__(self):
        self.get_calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, timeout):
        self.get_calls.append(url)
        return FakeResponse()


def _install_fake_sessions(monkeypatch) -> list[FakeSession]:
    sessions: list[FakeSession] = []

    def factory():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(cb.requests, "Session", factory)
    return sessions


def test_exit_ip_is_cached_for_the_same_sticky_proxy(monkeypatch):
    sessions = _install_fake_sessions(monkeypatch)
    monkeypatch.setenv("GEOIP_CACHE_TTL", "60")
    cb._GEOIP_CACHE.clear()
    proxy = {"server": "socks5://10.0.0.1:1080", "username": "u", "password": "sticky-a"}

    assert cb._resolve_exit_ip(proxy) == "203.0.113.10"
    assert cb._resolve_exit_ip(proxy) == "203.0.113.10"

    assert len(sessions) == 1
    assert len(sessions[0].get_calls) == 1


def test_exit_ip_cache_separates_sticky_sessions(monkeypatch):
    sessions = _install_fake_sessions(monkeypatch)
    monkeypatch.setenv("GEOIP_CACHE_TTL", "60")
    cb._GEOIP_CACHE.clear()
    first = {"server": "socks5://10.0.0.1:1080", "username": "u", "password": "sticky-a"}
    second = {"server": "socks5://10.0.0.1:1080", "username": "u", "password": "sticky-b"}

    cb._resolve_exit_ip(first)
    cb._resolve_exit_ip(second)

    assert len(sessions) == 2


def test_exit_ip_cache_can_be_disabled(monkeypatch):
    sessions = _install_fake_sessions(monkeypatch)
    monkeypatch.setenv("GEOIP_CACHE_TTL", "0")
    cb._GEOIP_CACHE.clear()
    proxy = {"server": "socks5://10.0.0.2:1080", "username": "u", "password": "sticky-c"}

    cb._resolve_exit_ip(proxy)
    cb._resolve_exit_ip(proxy)

    assert len(sessions) == 2
