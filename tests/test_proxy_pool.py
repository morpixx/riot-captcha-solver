"""Тесты пула прокси без реальной сети."""

from __future__ import annotations

import pytest

from src.proxy import pool as pp


def test_load_pool_deduplicates_and_normalizes(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "\n".join(
            [
                "# comment",
                "socks5://1.2.3.4:1080",
                "1.2.3.4:1080",
                "socks5h://1.2.3.4:1080",
                "http://user:pass@5.6.7.8:8080",
                "",
            ]
        ),
        encoding="utf-8",
    )
    proxies = pp.load_proxy_pool(path)
    assert proxies == ["socks5://1.2.3.4:1080", "http://user:pass@5.6.7.8:8080"]


def test_load_pool_reports_bad_line(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("host-without-port\n", encoding="utf-8")
    with pytest.raises(pp.ProxyPoolError, match=":1:"):
        pp.load_proxy_pool(path)


def test_proxy_key_ignores_socks5_variant_and_case():
    assert pp.proxy_key("socks5://1.2.3.4:1080") == pp.proxy_key("SOCKS5H://1.2.3.4:1080")
    assert pp.proxy_key("socks5://user:pass@1.2.3.4:1080") != pp.proxy_key(
        "socks5://other:pass@1.2.3.4:1080"
    )


def test_choose_proxy_skips_used():
    probes = [
        pp.ProxyProbe("socks5://1.1.1.1:1080", "a", 10, 20),
        pp.ProxyProbe("socks5://2.2.2.2:1080", "b", 20, 30),
    ]
    assert pp.choose_proxy(probes, {"a"}).key == "b"
    with pytest.raises(pp.ProxyPoolError, match="заняты"):
        pp.choose_proxy(probes, {"a", "b"})


def test_rank_proxies_orders_by_http(monkeypatch):
    tcp = {
        "socks5://1.1.1.1:1080": 10.0,
        "socks5://2.2.2.2:1080": 20.0,
        "socks5://3.3.3.3:1080": 30.0,
    }
    http = {
        "socks5://1.1.1.1:1080": 300.0,
        "socks5://2.2.2.2:1080": 100.0,
    }

    monkeypatch.setattr(pp, "_tcp_probe", lambda proxy, timeout: tcp.get(proxy))
    monkeypatch.setattr(
        pp,
        "_http_probe",
        lambda proxy, test_url, timeout: (
            (http.get(proxy), 200, None) if proxy in http else (None, 407, "auth")
        ),
    )

    result = pp.rank_proxies(tcp, top_n=3, log=None)
    assert [item.proxy for item in result] == ["socks5://2.2.2.2:1080", "socks5://1.1.1.1:1080"]
    assert result[0].http_ms == 100.0


def test_rank_proxies_respects_max_latency(monkeypatch):
    proxies = ["socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"]
    latencies = {"socks5://1.1.1.1:1080": 100.0, "socks5://2.2.2.2:1080": 5000.0}
    monkeypatch.setattr(pp, "_tcp_probe", lambda proxy, timeout: 10.0)
    monkeypatch.setattr(
        pp,
        "_http_probe",
        lambda proxy, test_url, timeout: (latencies[proxy], 200, None),
    )
    result = pp.rank_proxies(proxies, top_n=2, max_latency_ms=3000)
    assert [item.proxy for item in result] == ["socks5://1.1.1.1:1080"]


def test_rank_proxies_raises_when_all_tcp_dead(monkeypatch):
    monkeypatch.setattr(pp, "_tcp_probe", lambda proxy, timeout: None)
    with pytest.raises(pp.ProxyPoolError, match="TCP"):
        pp.rank_proxies(["socks5://1.1.1.1:1080"])


def test_rank_proxies_can_use_unverified(monkeypatch):
    monkeypatch.setattr(pp, "_tcp_probe", lambda proxy, timeout: 15.0)
    monkeypatch.setattr(pp, "_http_probe", lambda proxy, test_url, timeout: (None, None, "timeout"))
    result = pp.rank_proxies(["socks5://1.1.1.1:1080"], allow_unverified=True)
    assert result[0].http_ms is None
    assert result[0].error == "unverified"
