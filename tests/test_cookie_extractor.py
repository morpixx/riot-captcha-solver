"""Тесты чистого хранилища и нормализации cookie (без браузера)."""

from __future__ import annotations

import json

import pytest

import cookie_extractor as ce


def _cookie(name="ssid", value="secret", domain=".riotgames.com", path="/", **extra):
    item = {"name": name, "value": value, "domain": domain, "path": path}
    item.update(extra)
    return item


def test_extract_filters_domains_and_normalizes():
    raw = [
        _cookie("ssid", "abc", ".riotgames.com", "/", httpOnly=True, secure=True, sameSite="Lax"),
        _cookie("csrftoken", "csrf", "account.riotgames.com", "/", expires=1893456000),
        _cookie("session", "session", "riotgames.com", "/", expirationDate=1893456000),
        _cookie("bad", "x", "evilriotgames.com"),
        _cookie("google", "x", ".google.com"),
        _cookie("ssid", "duplicate", ".riotgames.com", "/", httpOnly=True, secure=True),
    ]
    cookies = ce.extract_cookies(raw)

    assert {c["name"] for c in cookies} == {"csrftoken", "session", "ssid"}
    ssid = next(c for c in cookies if c["name"] == "ssid")
    assert ssid["value"] == "duplicate"
    assert ssid["httpOnly"] is True
    assert ssid["secure"] is True
    assert "sameSite" not in ssid
    assert "expires" not in ssid
    assert next(c for c in cookies if c["name"] == "csrftoken")["expires"] == 1893456000.0


def test_extract_requires_session_cookie():
    with pytest.raises(ce.ExtractorError, match="ssid"):
        ce.extract_cookies([_cookie("csrftoken", "x")])


def test_extract_session_cookie_can_be_disabled():
    cookies = ce.extract_cookies([_cookie("csrftoken", "x")], session_cookie="")
    assert len(cookies) == 1


def test_external_id_stable_and_collision_safe():
    first = ce.make_external_id("acc-01")
    assert first == "riot-acc-01"
    second = ce.make_external_id("acc-01", [first])
    assert second != first
    assert second.startswith("riot-acc-01-")
    assert ce.make_external_id("Аккаунт #1").startswith("riot-")


def test_store_upsert_updates_in_place_and_preserves_id(tmp_path):
    path = tmp_path / "accounts.jsonl"
    store = ce.CookieStore(path)
    record, created = store.upsert(
        {
            "label": "acc-01",
            "new_email": "one@example.com",
            "proxy": "socks5://1.2.3.4:1080",
            "cookies": [_cookie()],
        }
    )
    assert created is True
    assert record["external_id"] == "riot-acc-01"

    record2, created2 = store.upsert(
        {
            "label": "acc-01",
            "new_email": "two@example.com",
            "proxy": "socks5://5.6.7.8:1080",
            "cookies": [_cookie(value="new")],
        }
    )
    assert created2 is False
    assert record2["external_id"] == record["external_id"]
    assert record2["new_email"] == "two@example.com"

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["cookies"][0]["value"] == "new"


def test_store_loads_json_array_and_deduplicates_labels(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            [
                {"label": "a", "cookies": [], "external_id": "id-a"},
                {"label": "A", "cookies": [], "external_id": "id-b"},
                {"label": "b", "cookies": []},
            ]
        ),
        encoding="utf-8",
    )
    store = ce.CookieStore(path)
    assert [entry["label"] for entry in store.entries] == ["A", "b"]
    assert [entry["external_id"] for entry in store.entries] == ["id-b", "riot-b"]


def test_store_remove(tmp_path):
    store = ce.CookieStore(tmp_path / "accounts.jsonl")
    store.upsert(
        {
            "label": "a",
            "new_email": "a@example.com",
            "proxy": "http://1.2.3.4:8080",
            "cookies": [_cookie()],
        }
    )
    removed = store.remove("a")
    assert removed["label"] == "a"
    assert store.entries == []
    assert (tmp_path / "accounts.jsonl").read_text(encoding="utf-8") == ""


def test_export_batch_is_server_ready(tmp_path):
    store = ce.CookieStore(tmp_path / "accounts.jsonl")
    store.upsert(
        {
            "label": "acc-01",
            "new_email": "one@example.com",
            "proxy": "socks5://1.2.3.4:1080",
            "cookies": [_cookie()],
        }
    )
    out = tmp_path / "batch.json"
    count, path = ce.export_jobs(store, fmt="batch", out=out)
    assert count == 1
    assert path == out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["jobs"][0] == {
        "external_id": "riot-acc-01",
        "cookies": [
            {
                "name": "ssid",
                "value": "secret",
                "domain": ".riotgames.com",
                "path": "/",
            }
        ],
        "proxy": "socks5://1.2.3.4:1080",
        "new_email": "one@example.com",
        "headless": True,
    }


def test_export_refuses_to_overwrite_store(tmp_path):
    path = tmp_path / "accounts.jsonl"
    store = ce.CookieStore(path)
    store.upsert(
        {
            "label": "acc-01",
            "new_email": "one@example.com",
            "proxy": "socks5://1.2.3.4:1080",
            "cookies": [_cookie()],
        }
    )
    with pytest.raises(ce.ExtractorError, match="совпадать"):
        ce.export_jobs(store, out=path)


def test_export_rejects_incomplete(tmp_path):
    store = ce.CookieStore(tmp_path / "accounts.jsonl")
    store.upsert(
        {
            "label": "acc-01",
            "new_email": "one@example.com",
            "proxy": "",
            "cookies": [_cookie()],
        }
    )
    with pytest.raises(ce.ExtractorError, match="proxy"):
        ce.export_jobs(store, out=tmp_path / "x.jsonl")

    count, _ = ce.export_jobs(
        store, out=tmp_path / "x.jsonl", require_proxy=False, skip_incomplete=True
    )
    assert count == 1


def test_not_logged_in_detection():
    assert ce._is_not_logged_in({"status": 401, "body": None}) is True
    assert (
        ce._is_not_logged_in(
            {"status": 403, "body": {"httpStatus": 403, "errorCode": "not_logged_in"}}
        )
        is True
    )
    assert ce._is_not_logged_in({"status": 403, "body": {"errorCode": "other"}}) is False
    assert ce._is_not_logged_in({"status": 200, "body": {}}) is False
    assert ce._is_not_logged_in(None) is False


def test_next_label_skips_used(tmp_path):
    store = ce.CookieStore(tmp_path / "accounts.jsonl")
    store.upsert({"label": "acc-01", "new_email": "a@example.com", "cookies": [_cookie()]})
    store.upsert({"label": "acc-03", "new_email": "b@example.com", "cookies": [_cookie()]})
    assert ce.next_label(store) == "acc-02"
    assert ce.next_label(store, "riot") == "riot-01"


def test_add_batch_assigns_unique_proxies(monkeypatch, tmp_path):
    from argparse import Namespace

    from src.proxy.pool import ProxyProbe

    store_path = tmp_path / "accounts.jsonl"
    emails_path = tmp_path / "emails.txt"
    emails_path.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")

    probes = [
        ProxyProbe("socks5://1.1.1.1:1080", "key-1", 10, 20),
        ProxyProbe("socks5://2.2.2.2:1080", "key-2", 20, 30),
        ProxyProbe("socks5://3.3.3.3:1080", "key-3", 30, 40),
    ]
    monkeypatch.setattr(ce, "_rank_pool", lambda *a, **k: probes)

    seen = []

    def fake_collect(store, **kwargs):
        seen.append((kwargs["label"], kwargs["proxy"]))
        return store.upsert(
            {
                "label": kwargs["label"],
                "new_email": kwargs["email"],
                "proxy": kwargs["proxy"],
                "cookies": [_cookie()],
            }
        )[0]

    monkeypatch.setattr(ce, "_collect_account", fake_collect)

    args = Namespace(
        store=store_path,
        proxy_pool=tmp_path / "proxies.txt",
        emails=emails_path,
        count=0,
        label_prefix="acc",
        proxy_test_url="https://example.com/",
        proxy_timeout=1.0,
        proxy_workers=1,
        proxy_top=3,
        allow_unverified_proxy=False,
        url=ce.DEFAULT_LOGIN_URL,
        domain=ce.DEFAULT_DOMAIN,
        session_cookie=ce.DEFAULT_SESSION_COOKIE,
        timeout=30,
        headless=True,
    )
    assert ce.cmd_add_batch(args) == 0
    assert [item[0] for item in seen] == ["acc-01", "acc-02"]
    assert [item[1] for item in seen] == ["socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"]
    assert len(ce.CookieStore(store_path).entries) == 2


def test_add_batch_reuses_one_proxy_for_every_account(monkeypatch, tmp_path):
    from argparse import Namespace

    store_path = tmp_path / "accounts.jsonl"
    emails_path = tmp_path / "emails.txt"
    emails_path.write_text(
        "one@example.com\ntwo@example.com\nthree@example.com\n",
        encoding="utf-8",
    )

    seen = []

    def fake_collect(store, **kwargs):
        seen.append((kwargs["label"], kwargs["proxy"], kwargs["email"]))
        return store.upsert(
            {
                "label": kwargs["label"],
                "new_email": kwargs["email"],
                "proxy": kwargs["proxy"],
                "cookies": [_cookie()],
            }
        )[0]

    monkeypatch.setattr(ce, "_collect_account", fake_collect)

    proxy = "http://user:pass@1.1.1.1:8080"
    args = Namespace(
        store=store_path,
        proxy=proxy,
        no_proxy=False,
        proxy_pool=None,
        emails=emails_path,
        count=0,
        label_prefix="fresh",
        url=ce.DEFAULT_LOGIN_URL,
        domain=ce.DEFAULT_DOMAIN,
        session_cookie=ce.DEFAULT_SESSION_COOKIE,
        timeout=30,
        headless=True,
    )

    assert ce.cmd_add_batch(args) == 0
    assert [item[0] for item in seen] == ["fresh-01", "fresh-02", "fresh-03"]
    assert [item[1] for item in seen] == [proxy, proxy, proxy]
    assert len(ce.CookieStore(store_path).entries) == 3


def test_add_batch_rejects_proxy_and_no_proxy_together(tmp_path):
    from argparse import Namespace

    args = Namespace(
        store=tmp_path / "accounts.jsonl",
        proxy="http://1.1.1.1:8080",
        no_proxy=True,
        proxy_pool=None,
        emails=None,
        count=1,
        label_prefix="acc",
    )

    try:
        ce.cmd_add_batch(args)
    except ce.ExtractorError as exc:
        assert "--proxy и --no-proxy" in str(exc)
    else:
        raise AssertionError("expected ExtractorError")


def test_cmd_add_auto_label_skips_used_proxy(monkeypatch, tmp_path):
    from argparse import Namespace

    from src.proxy.pool import ProxyProbe

    store_path = tmp_path / "accounts.jsonl"
    store = ce.CookieStore(store_path)
    store.upsert(
        {
            "label": "acc-01",
            "new_email": "old@example.com",
            "proxy": "socks5://1.1.1.1:1080",
            "cookies": [_cookie()],
        }
    )
    from src.proxy.pool import proxy_key

    probes = [
        ProxyProbe("socks5://1.1.1.1:1080", proxy_key("socks5://1.1.1.1:1080"), 10, 20),
        ProxyProbe("socks5://2.2.2.2:1080", proxy_key("socks5://2.2.2.2:1080"), 20, 30),
    ]
    monkeypatch.setattr(ce, "_rank_pool", lambda *a, **k: probes)
    seen = {}

    def fake_collect(store, **kwargs):
        seen.update(kwargs)
        return store.upsert(
            {
                "label": kwargs["label"],
                "new_email": kwargs["email"],
                "proxy": kwargs["proxy"],
                "cookies": [_cookie()],
            }
        )[0]

    monkeypatch.setattr(ce, "_collect_account", fake_collect)
    args = Namespace(
        store=store_path,
        label=None,
        auto_label=False,
        label_prefix="acc",
        email="new@example.com",
        proxy=None,
        new_proxy=False,
        external_id=None,
        proxy_pool=tmp_path / "proxies.txt",
        proxy_test_url="https://example.com/",
        proxy_timeout=1.0,
        proxy_workers=1,
        proxy_top=2,
        allow_unverified_proxy=False,
        url=ce.DEFAULT_LOGIN_URL,
        domain=ce.DEFAULT_DOMAIN,
        session_cookie=ce.DEFAULT_SESSION_COOKIE,
        timeout=30,
        headless=True,
    )
    assert ce.cmd_add(args) == 0
    assert seen["label"] == "acc-02"
    assert seen["proxy"] == "socks5://2.2.2.2:1080"


def test_add_batch_auto_continues_scanning(monkeypatch, tmp_path):
    from argparse import Namespace

    from src.proxy.pool import ProxyProbe, proxy_key

    emails_path = tmp_path / "emails.txt"
    emails_path.write_text(
        "one@example.com\ntwo@example.com\nthree@example.com\nfour@example.com\n",
        encoding="utf-8",
    )
    auto_pool = tmp_path / "auto.txt"
    auto_pool.write_text("socks5://9.9.9.9:1080\n", encoding="utf-8")
    initial = [
        ProxyProbe("socks5://1.1.1.1:1080", proxy_key("socks5://1.1.1.1:1080"), 10, 10),
        ProxyProbe("socks5://2.2.2.2:1080", proxy_key("socks5://2.2.2.2:1080"), 20, 20),
    ]
    expanded = [
        *initial,
        ProxyProbe("socks5://3.3.3.3:1080", proxy_key("socks5://3.3.3.3:1080"), 30, 30),
        ProxyProbe("socks5://4.4.4.4:1080", proxy_key("socks5://4.4.4.4:1080"), 40, 40),
    ]
    monkeypatch.setattr(ce, "DATA_INPUT", tmp_path)
    monkeypatch.setattr(ce, "_rank_pool", lambda *a, **k: initial)

    def fake_scan(candidates, **kwargs):
        assert kwargs["need"] == 4
        assert len(kwargs["known"]) == 2
        return expanded

    monkeypatch.setattr(ce, "scan_until", fake_scan)
    seen = []

    def fake_collect(store, **kwargs):
        seen.append(kwargs["label"])
        return store.upsert(
            {
                "label": kwargs["label"],
                "new_email": kwargs["email"],
                "proxy": kwargs["proxy"],
                "cookies": [_cookie()],
            }
        )[0]

    monkeypatch.setattr(ce, "_collect_account", fake_collect)

    args = Namespace(
        store=tmp_path / "accounts.jsonl",
        proxy_pool=tmp_path / "working.txt",
        auto_proxy_pool=auto_pool,
        emails=emails_path,
        count=4,
        label_prefix="acc",
        proxy_test_url="https://example.com/",
        proxy_timeout=1.0,
        proxy_workers=1,
        proxy_top=0,
        allow_unverified_proxy=False,
        url=ce.DEFAULT_LOGIN_URL,
        domain=ce.DEFAULT_DOMAIN,
        session_cookie=ce.DEFAULT_SESSION_COOKIE,
        timeout=30,
        headless=True,
    )
    assert ce.cmd_add_batch(args) == 0
    assert len(seen) == 4
    assert len(ce.CookieStore(args.store).entries) == 4
    assert (tmp_path / "proxies-working.txt").read_text(encoding="utf-8").count("\n") == 4


def test_cloudflare_block_page_detected():
    class FakePage:
        def is_closed(self):
            return False

        def title(self):
            return "Attention Required! | Cloudflare"

    assert ce._page_is_blocked(FakePage()) is True


def test_prune_and_remove_bad_proxy(tmp_path, monkeypatch):
    from src.proxy.pool import proxy_key

    working = tmp_path / "proxies-working.txt"
    good = tmp_path / "proxies-good.txt"
    proxy = "socks5://1.1.1.1:1080"
    working.write_text(
        "# comment\n" + proxy + "\nsocks5://2.2.2.2:1080\n",
        encoding="utf-8",
    )
    good.write_text(proxy + "\nsocks5://3.3.3.3:1080\n", encoding="utf-8")
    monkeypatch.setattr(ce, "DATA_INPUT", tmp_path)

    ce._prune_proxy_file(working, {proxy_key(proxy)})
    assert "1.1.1.1" in working.read_text(encoding="utf-8")
    assert "2.2.2.2" not in working.read_text(encoding="utf-8")
    assert "# comment" in working.read_text(encoding="utf-8")

    ce._mark_proxy_bad(working, proxy, "Cloudflare blocked")
    assert "1.1.1.1" not in working.read_text(encoding="utf-8")
    assert "1.1.1.1" not in good.read_text(encoding="utf-8")
    bad = (tmp_path / "proxies-bad.txt").read_text(encoding="utf-8")
    assert "socks5://1.1.1.1:1080" in bad
    assert "cloudflare" in bad


def test_load_bad_proxy_keys(tmp_path, monkeypatch):
    from src.proxy.pool import proxy_key

    monkeypatch.setattr(ce, "DATA_INPUT", tmp_path)
    (tmp_path / "proxies-bad.txt").write_text(
        "socks5://1.1.1.1:1080\tcloudflare\nsocks5://2.2.2.2:1080\tproxy_error\n",
        encoding="utf-8",
    )
    keys = ce._load_bad_proxy_keys()
    assert proxy_key("socks5://1.1.1.1:1080") in keys
    assert proxy_key("socks5://2.2.2.2:1080") in keys


def test_proxy_failure_classification():
    assert ce._is_proxy_failure("Cloudflare заблокировал IP proxy") is True
    assert ce._is_proxy_failure("SOCKS connection refused") is True
    assert ce._is_proxy_failure("не дождался входа за 900 с") is False
    assert ce._is_proxy_failure("email уже используется") is False


def test_add_batch_no_proxy(monkeypatch, tmp_path):
    from argparse import Namespace

    emails_path = tmp_path / "emails.txt"
    emails_path.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")
    seen = []

    def fake_collect(store, **kwargs):
        seen.append(kwargs)
        return store.upsert(
            {
                "label": kwargs["label"],
                "new_email": kwargs["email"],
                "proxy": kwargs["proxy"],
                "cookies": [_cookie()],
            }
        )[0]

    monkeypatch.setattr(ce, "_collect_account", fake_collect)
    args = Namespace(
        store=tmp_path / "accounts.jsonl",
        proxy_pool=None,
        auto_proxy_pool=None,
        emails=emails_path,
        count=2,
        no_proxy=True,
        label_prefix="retest",
        proxy_test_url="https://example.com/",
        proxy_timeout=1.0,
        proxy_workers=1,
        proxy_top=0,
        max_proxy_latency=3.0,
        allow_unverified_proxy=False,
        url=ce.DEFAULT_LOGIN_URL,
        domain=ce.DEFAULT_DOMAIN,
        session_cookie=ce.DEFAULT_SESSION_COOKIE,
        timeout=30,
        headless=True,
        max_page_load=0,
    )
    assert ce.cmd_add_batch(args) == 0
    assert [item["label"] for item in seen] == ["retest-01", "retest-02"]
    assert all(item["proxy"] == "" for item in seen)
    assert all(item["max_page_load"] == 0.0 for item in seen)
    assert len(ce.CookieStore(args.store).entries) == 2


def test_assign_proxies_to_empty_entries(monkeypatch, tmp_path):
    from argparse import Namespace

    from src.proxy.pool import ProxyProbe, proxy_key

    store_path = tmp_path / "accounts.jsonl"
    store = ce.CookieStore(store_path)
    store.upsert({"label": "a", "new_email": "a@example.com", "cookies": [_cookie()]})
    store.upsert({"label": "b", "new_email": "b@example.com", "cookies": [_cookie()]})
    probes = [
        ProxyProbe("socks5://1.1.1.1:1080", proxy_key("socks5://1.1.1.1:1080"), 10, 20),
        ProxyProbe("socks5://2.2.2.2:1080", proxy_key("socks5://2.2.2.2:1080"), 20, 30),
    ]
    monkeypatch.setattr(ce, "_rank_pool", lambda *a, **k: probes)
    monkeypatch.setattr(ce, "DATA_INPUT", tmp_path)
    args = Namespace(
        store=store_path,
        proxy_pool=tmp_path / "proxies-working.txt",
        auto_proxy_pool=None,
        proxy_test_url="https://example.com/",
        proxy_timeout=1.0,
        proxy_workers=1,
        proxy_top=0,
        max_proxy_latency=3.0,
        allow_unverified_proxy=False,
    )
    assert ce.cmd_assign_proxies(args) == 0
    entries = ce.CookieStore(store_path).entries
    assert [entry["proxy"] for entry in entries] == [
        "socks5://1.1.1.1:1080",
        "socks5://2.2.2.2:1080",
    ]


def test_invalid_email_and_proxy():
    with pytest.raises(ce.ExtractorError):
        ce.validate_email("not-an-email")
    with pytest.raises(ce.ExtractorError):
        ce.clean_proxy("socks5://host-without-port")
    assert ce.clean_proxy("") == ""


def test_cli_parser_exposes_public_commands():
    parser = ce.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    assert set(subparsers.choices) == {
        "add",
        "relogin",
        "assign-proxies",
        "proxy-scan",
        "add-batch",
        "list",
        "remove",
        "export",
    }


def test_scan_from_args_forwards_only_scan_parameters(monkeypatch):
    from argparse import Namespace

    captured: dict = {}

    def fake_scan(candidates, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(ce, "scan_until", fake_scan)
    args = Namespace(
        batch_size=32,
        proxy_test_url="https://example.com/",
        proxy_timeout=2.0,
        proxy_workers=4,
        proxy_top=9,
        allow_unverified_proxy=True,
        max_proxy_latency=1.5,
    )

    ce._scan_from_args(["socks5://1.1.1.1:1080"], args, need=1)

    assert captured["batch_size"] == 32
    assert captured["timeout"] == 2.0
    assert captured["workers"] == 4
    assert captured["max_latency_ms"] == 1500.0
    assert "top_n" not in captured
    assert "allow_unverified" not in captured
