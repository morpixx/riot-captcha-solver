"""Camoufox adapter helpers and the Camoufox-only flow (no real browser)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.browser import browser
from src.browser.camoufox_boost import (
    FIREFOX_PREFS,
    camoufox_options,
    should_block_request,
)
from src.browser.camoufox_browser import _headless_mode, _proxy_url
from src.config import Account, Config
from src.jobs.errors import classify_error
from src.riot import email_form
from src.riot import flow as riot_flow
from src.riot.minter import _token_problem
from src.riot.navigation import on_host
from src.riot.state import email_verification_problem, session_is_rejected


def test_browser_package_is_camoufox_only():
    assert browser.BACKEND == "camoufox"
    assert browser.open_browser is not None


def test_proxy_url_encodes_credentials():
    url = _proxy_url(
        {
            "server": "http://gw.example:10000",
            "username": "user@example",
            "password": "p:/?@#",
        }
    )
    assert url == "http://user%40example:p%3A%2F%3F%40%23@gw.example:10000"


def test_proxy_url_without_credentials_is_unchanged():
    assert _proxy_url({"server": "socks5://1.2.3.4:1080"}) == "socks5://1.2.3.4:1080"
    assert _proxy_url(None) is None


def test_headless_mode_maps_virtual_to_true():
    assert _headless_mode(False) is False
    assert _headless_mode(True) is True
    assert _headless_mode("virtual") is True
    assert _headless_mode("true") is True
    assert _headless_mode("0") is False


def test_camoufox_options_keep_fingerprint_geoip_and_boost():
    proxy = {"server": "socks5://1.2.3.4:1080", "username": "u", "password": "p"}
    options = camoufox_options(headless=True, proxy=proxy, geoip="203.0.113.7")

    assert options["headless"] is True
    assert options["persistent_context"] is True
    assert options["os"] == "windows"
    assert options["geoip"] == "203.0.113.7"
    assert options["block_webrtc"] is True
    assert options["proxy"] is proxy
    assert options["firefox_user_prefs"]["dom.ipc.processCount"] == 1
    assert options["firefox_user_prefs"] == FIREFOX_PREFS
    assert options["firefox_user_prefs"] is not FIREFOX_PREFS
    assert [addon.name for addon in options["exclude_addons"]] == ["UBO"]


def _request(url: str, resource_type: str) -> SimpleNamespace:
    return SimpleNamespace(url=url, resource_type=resource_type)


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        ("https://www.google-analytics.com/collect", "script"),
        ("https://cdn.example.com/logo.png", "image"),
        ("https://fonts.example.com/x.woff2", "font"),
        ("https://cdn.example.com/video.mp4", "media"),
    ],
)
def test_booster_blocks_heavy_and_tracking_requests(url, resource_type):
    assert should_block_request(_request(url, resource_type)) is True


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        ("https://newassets.hcaptcha.com/c/abc/hsw.js", "script"),
        ("https://newassets.hcaptcha.com/logo.png", "image"),
        ("https://fonts.hcaptcha.com/x.woff2", "font"),
        ("https://account.riotgames.com/", "document"),
        ("https://account.riotgames.com/app.js", "script"),
    ],
)
def test_booster_keeps_captcha_and_riot_assets(url, resource_type):
    assert should_block_request(_request(url, resource_type)) is False


def test_host_match_ignores_query_string_and_subdomains():
    assert on_host(
        "https://account.riotgames.com/?next=https://authenticate.riotgames.com",
        "account.riotgames.com",
    )
    assert on_host("https://account.riotgames.com/", "account.riotgames.com")
    assert on_host("https://sub.account.riotgames.com/", "account.riotgames.com")
    assert not on_host("https://authenticate.riotgames.com/", "account.riotgames.com")
    assert not on_host("https://account.riotgames.com.evil.test/", "account.riotgames.com")


def test_session_rejects_not_logged_in():
    assert session_is_rejected({"status": 401, "body": None}) is True
    assert session_is_rejected({"status": 403, "body": {"errorCode": "not_logged_in"}}) is True
    assert (
        session_is_rejected({"status": 403, "body": {"error_code": "invalid_session_id"}}) is True
    )


def test_session_keeps_ambiguous_answers():
    assert session_is_rejected(None) is False
    assert session_is_rejected({"status": 200, "body": {}}) is False
    assert session_is_rejected({"status": 404, "body": {"error": "not_found"}}) is False
    assert session_is_rejected({"status": 0, "error": "network"}) is False


def test_email_verification_problem_detects_unverified_account():
    problem = email_verification_problem(
        {
            "status": 200,
            "body": {
                "email": "CAC***@gm***.com",
                "email_status": "not_validated",
                "derived_states": ["email_verification"],
            },
        }
    )
    assert problem is not None
    assert "not verified" in problem
    assert "CAC***@gm***.com" in problem
    assert "not_validated" in problem


def test_email_verification_problem_ignores_valid_and_unknown_state():
    assert email_verification_problem(None) is None
    assert email_verification_problem({"status": 403, "body": {}}) is None
    assert email_verification_problem({"status": 200, "body": {"email_status": "validated"}}) is None
    assert email_verification_problem({"status": 200, "body": {}}) is None
    assert email_verification_problem({"status": 200, "body": {"derived_states": ["mfa"]}}) is None


def test_unverified_email_is_not_retryable():
    code, retryable = classify_error(
        RuntimeError(
            "Riot email is not verified (CAC***@gm***.com, email_status=not_validated); "
            "verify the current address before changing it"
        )
    )
    assert (code, retryable) == ("email_not_verified", False)


def test_origin_and_token_errors_are_classified():
    code, retryable = classify_error(
        RuntimeError(
            "hCaptcha was executed on 'authenticate.riotgames.com'; the page left the Riot account origin"
        )
    )
    assert (code, retryable) == ("session_invalid", False)

    code, retryable = classify_error(RuntimeError("hCaptcha returned an unusable token: prefix"))
    assert (code, retryable) == ("captcha_failed", True)


def test_token_problem_accepts_real_tokens():
    assert _token_problem("P1_" + "a" * 240, "sitekey") is None
    assert _token_problem("E0_" + "a" * 240, "sitekey") is None


def test_token_problem_rejects_placeholders():
    assert _token_problem("", "sitekey") == "empty token"
    assert (
        _token_problem("sitekey", "sitekey") == "hCaptcha returned the sitekey instead of a token"
    )
    assert "prefix" in _token_problem("10000000-aaaa", "sitekey")
    assert "too short" in _token_problem("P1_short", "sitekey")


class _FakePage:
    url = "https://account.riotgames.com/"

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, *_args) -> None:
        return None

    def evaluate(self, *_args, **_kwargs) -> dict:
        return {
            "status": 200,
            "body": {"message": "email_verification_sent", "shouldPoll": False},
        }


def test_run_keeps_shouldpoll_as_is(monkeypatch):
    seen: dict = {}

    @contextmanager
    def fake_open(cookies, **kwargs):
        seen.update(cookies=cookies, **kwargs)
        yield object(), _FakePage()

    monkeypatch.setattr(riot_flow, "open_browser", fake_open)
    monkeypatch.setattr(riot_flow, "humanize_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        email_form,
        "submit_via_ui",
        lambda *_args, **_kwargs: {
            "status": 200,
            "body": {"message": "email_verification_sent", "shouldPoll": False},
        },
    )

    cfg = Config(
        sitekey="sitekey",
        host="account.riotgames.com",
        headless=True,
        allow_email_change=False,
        captcha_timeout=1,
        accounts=[],
    )
    account = Account(name="acc", cookies=[], new_email="new@example.com")

    result = riot_flow.run(
        cfg,
        account,
        submit=True,
        proxy=None,
        log=lambda _message: None,
    )

    assert "backend" not in seen
    assert "addons" not in seen
    assert "bypass_csp" not in seen
    assert seen["headless"] is True
    assert result["shouldPoll"] is False
    assert result["body"]["shouldPoll"] is False


class _UnverifiedPage(_FakePage):
    def evaluate(self, *_args, **_kwargs) -> dict:
        return {
            "status": 200,
            "body": {
                "email": "CAC***@gm***.com",
                "email_status": "not_validated",
                "derived_states": ["email_verification"],
            },
        }


def test_run_rejects_unverified_email_before_captcha(monkeypatch):
    @contextmanager
    def fake_open(cookies, **kwargs):
        yield object(), _UnverifiedPage()

    monkeypatch.setattr(riot_flow, "open_browser", fake_open)
    monkeypatch.setattr(riot_flow, "humanize_page", lambda *_args, **_kwargs: None)

    def must_not_mint(*_args, **_kwargs):
        raise AssertionError("hCaptcha must not be minted for an unverified email")

    monkeypatch.setattr(riot_flow, "mint_in_page", must_not_mint)

    cfg = Config(
        sitekey="sitekey",
        host="account.riotgames.com",
        headless=True,
        allow_email_change=False,
        captcha_timeout=1,
        accounts=[],
    )
    account = Account(name="acc", cookies=[], new_email="new@example.com")

    with pytest.raises(RuntimeError, match="not verified"):
        riot_flow.run(
            cfg,
            account,
            submit=True,
            proxy=None,
            log=lambda _message: None,
        )


def test_run_allows_dry_run_for_unverified_email(monkeypatch):
    @contextmanager
    def fake_open(cookies, **kwargs):
        yield object(), _UnverifiedPage()

    monkeypatch.setattr(riot_flow, "open_browser", fake_open)
    monkeypatch.setattr(riot_flow, "humanize_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        riot_flow,
        "mint_in_page",
        lambda *_args, **_kwargs: {
            "token": "P1_test",
            "host": "account.riotgames.com",
            "csrf": "x",
        },
    )

    cfg = Config(
        sitekey="sitekey",
        host="account.riotgames.com",
        headless=True,
        allow_email_change=False,
        captcha_timeout=1,
        accounts=[],
    )
    account = Account(name="acc", cookies=[], new_email="new@example.com")

    result = riot_flow.run(
        cfg,
        account,
        submit=False,
        proxy=None,
        log=lambda _message: None,
    )

    assert result["dry_run"] is True
    assert result["token_len"] == len("P1_test")


class _PendingPage(_FakePage):
    def evaluate(self, *_args, **_kwargs) -> dict:
        return {
            "status": 200,
            "body": {
                "email": "sil***@gm***.com",
                "mode": "update",
                "message": "email_verification_pending",
            },
        }


def test_run_rejects_pending_change_before_ui(monkeypatch):
    @contextmanager
    def fake_open(cookies, **kwargs):
        yield object(), _PendingPage()

    monkeypatch.setattr(riot_flow, "open_browser", fake_open)
    monkeypatch.setattr(riot_flow, "humanize_page", lambda *_args, **_kwargs: None)

    def must_not_submit(*_args, **_kwargs):
        raise AssertionError("UI submit must not run while a change is pending")

    monkeypatch.setattr(email_form, "submit_via_ui", must_not_submit)

    cfg = Config(
        sitekey="sitekey",
        host="account.riotgames.com",
        headless=True,
        allow_email_change=False,
        captcha_timeout=1,
        accounts=[],
    )
    account = Account(name="acc", cookies=[], new_email="new@example.com")

    with pytest.raises(RuntimeError, match="pending email change"):
        riot_flow.run(
            cfg,
            account,
            submit=True,
            proxy=None,
            log=lambda _message: None,
        )


def test_pending_email_change_is_not_retryable():
    code, retryable = classify_error(
        RuntimeError(
            "Riot already has a pending email change to sil***@gm***.com; "
            "confirm it or wait for it to expire before submitting another"
        )
    )
    assert (code, retryable) == ("email_change_pending", False)
