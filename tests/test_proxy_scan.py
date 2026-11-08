"""Тесты постепенного поиска рабочих proxy."""

from __future__ import annotations

import pytest

from src.proxy import scan as proxy_scan


def test_scan_until_stops_after_enough(monkeypatch):
    calls = {"tcp": 0, "http": 0}
    good = {
        "socks5://1.1.1.1:1080": (10, 100),
        "socks5://2.2.2.2:1080": (20, 50),
        "socks5://3.3.3.3:1080": (30, 300),
    }

    def fake_tcp(proxy, timeout):
        calls["tcp"] += 1
        return good.get(proxy, (99, 0))[0]

    def fake_http(proxy, test_url, timeout):
        calls["http"] += 1
        if proxy in good:
            return good[proxy][1], 200, None
        return None, 407, "auth"

    monkeypatch.setattr(proxy_scan, "_tcp_probe", fake_tcp)
    monkeypatch.setattr(proxy_scan, "_http_probe", fake_http)

    result = proxy_scan.scan_until(
        [*good, "socks5://4.4.4.4:1080"],
        need=2,
        batch_size=4,
        log=None,
    )
    assert [item.proxy for item in result] == [
        "socks5://2.2.2.2:1080",
        "socks5://1.1.1.1:1080",
    ]
    assert calls["tcp"] == 4
    assert calls["http"] == 4


def test_scan_until_excludes_used(monkeypatch):
    from src.proxy.pool import ProxyProbe, proxy_key

    known = ProxyProbe("socks5://1.1.1.1:1080", proxy_key("socks5://1.1.1.1:1080"), 10, 10)
    used = "socks5://2.2.2.2:1080"
    candidates = [
        used,
        "socks5://3.3.3.3:1080",
        "socks5://4.4.4.4:1080",
    ]
    monkeypatch.setattr(proxy_scan, "_tcp_probe", lambda proxy, timeout: 10.0)
    monkeypatch.setattr(proxy_scan, "_http_probe", lambda proxy, url, timeout: (10.0, 200, None))

    result = proxy_scan.scan_until(
        candidates,
        known=[known],
        exclude=[used],
        need=3,
        batch_size=3,
        log=None,
    )
    assert [item.proxy for item in result] == [
        "socks5://1.1.1.1:1080",
        "socks5://3.3.3.3:1080",
        "socks5://4.4.4.4:1080",
    ]


def test_scan_until_raises_if_short(monkeypatch):
    monkeypatch.setattr(proxy_scan, "_tcp_probe", lambda proxy, timeout: 10.0)
    monkeypatch.setattr(proxy_scan, "_http_probe", lambda proxy, url, timeout: (None, 407, "auth"))
    with pytest.raises(Exception, match="не нашёл"):
        proxy_scan.scan_until(["socks5://1.1.1.1:1080"], need=2, batch_size=1)
