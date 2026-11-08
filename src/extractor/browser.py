"""Interactive browser capture for cookie_extractor.

The browser layer guarantees fingerprint + GeoIP; the user only completes the
Riot login (including 2FA/hCaptcha) manually.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

from ..browser.browser import open_browser
from ..proxy.proxy import parse_proxy
from .data import (
    DEFAULT_DOMAIN,
    DEFAULT_SESSION_COOKIE,
    ExtractorError,
    describe_proxy,
    domain_matches,
    extract_cookies,
)

log = logging.getLogger("riot.extractor.browser")

DEFAULT_LOGIN_URL = os.environ.get("LOGIN_URL", "https://account.riotgames.com/")
DEFAULT_TIMEOUT = 900

Log = Callable[[str], None]

VERIFY_JS = r"""
async () => {
  try {
    const r = await fetch('/api/account/v1/user', {
      method: 'GET',
      credentials: 'include',
      headers: {'Accept': 'application/json'},
      cache: 'no-store',
    });
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    return {status: r.status, ok: r.ok, body: body};
  } catch (e) {
    return {status: 0, ok: false, error: String(e)};
  }
}
"""


def _has_session_cookie(cookies: Iterable[dict[str, Any]], name: str, root_domain: str) -> bool:
    if not name:
        return True
    return any(
        c.get("name") == name
        and str(c.get("value") or "")
        and domain_matches(str(c.get("domain") or ""), root_domain)
        for c in cookies or []
    )


def _find_account_page(ctx: Any, fallback: Any, verify_host: str) -> Any:
    """After OAuth the account page sometimes ends up in a neighboring tab."""
    try:
        for candidate in ctx.pages:
            if not candidate.is_closed() and verify_host in str(candidate.url):
                return candidate
    except Exception as exc:
        log.debug("could not inspect browser pages: %s", exc)
    return fallback


def _verify_page_session(page: Any, verify_host: str) -> dict[str, Any] | None:
    try:
        if page.is_closed():
            return None
        if verify_host not in str(page.url):
            return None
        result = page.evaluate(VERIFY_JS)
        return result if isinstance(result, dict) else None
    except Exception:
        return None


BLOCK_MARKERS = (
    "sorry, you have been blocked",
    "you are unable to access",
    "attention required",
)


def _page_is_blocked(page: Any) -> bool:
    try:
        if page.is_closed():
            return False
        title = str(page.title()).lower()
        if "attention required" in title:
            return True
        body = page.locator("body").inner_text(timeout=1500)[:4000].lower()
        return any(marker in body for marker in BLOCK_MARKERS)
    except Exception:
        return False


def _is_not_logged_in(info: dict[str, Any] | None) -> bool:
    """Riot returns 403 not_logged_in on GET /api/account/v1/user without a session."""
    if not info:
        return False
    if info.get("status") == 401:
        return True
    if info.get("status") != 403:
        return False
    body = info.get("body")
    if not isinstance(body, dict):
        return False
    code = str(body.get("errorCode") or body.get("error_code") or "").lower()
    return code == "not_logged_in"


def capture_account(
    *,
    login_url: str = DEFAULT_LOGIN_URL,
    root_domain: str = DEFAULT_DOMAIN,
    session_cookie: str = DEFAULT_SESSION_COOKIE,
    proxy: str = "",
    headless: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    max_page_load: float = 30.0,
    log: Log = print,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Open a browser, wait for login and return ``(cookies, session_info)``."""
    parsed_proxy = parse_proxy(proxy) if proxy else None
    verify_host = urlparse(login_url).hostname or "account.riotgames.com"
    deadline = time.monotonic() + int(timeout) if int(timeout) > 0 else None
    session_seen_at: float | None = None
    last_report = 0.0
    last_verify = 0.0
    last_block_check = 0.0
    last_info: dict[str, Any] | None = None

    log("")
    log(
        f"[*] browser: camoufox ({'headless' if headless else 'headed'}), "
        f"fingerprint=on geoip=on, proxy={describe_proxy(proxy)}"
    )
    log(f"[*] открываю {login_url}")
    try:
        with open_browser(
            [],
            headless=headless,
            proxy=parsed_proxy,
        ) as (ctx, page):
            goto_timeout = max(5000, int(max_page_load * 2000)) if max_page_load > 0 else 90000
            goto_started = time.perf_counter()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=goto_timeout)
            except Exception as exc:
                raise ExtractorError(
                    f"proxy: страница входа не открылась за {goto_timeout / 1000:.1f} с: {exc}"
                ) from exc
            goto_elapsed = time.perf_counter() - goto_started
            if max_page_load > 0 and goto_elapsed > max_page_load:
                raise ExtractorError(
                    f"медленный proxy: страница входа грузилась {goto_elapsed:.1f} с "
                    f"(лимит {max_page_load:.1f} с)"
                )
            with suppress(Exception):
                page.bring_to_front()

            if _page_is_blocked(page):
                raise ExtractorError(
                    "Cloudflare заблокировал IP proxy (Sorry, you have been blocked). "
                    "Смените proxy или подключение; скрипт не ждёт 15 минут."
                )

            log("[*] войдите в аккаунт в открытом окне браузера.")
            log("[*] hCaptcha/2FA проходите руками; после входа cookie сохранятся сами.")

            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    try:
                        raw_cookies = ctx.cookies()
                    except Exception:
                        raw_cookies = []
                    if _has_session_cookie(raw_cookies, session_cookie, root_domain):
                        cookies = extract_cookies(raw_cookies, root_domain, session_cookie)
                        log(
                            "[+] время ожидания истекло, но session cookie найдена — "
                            f"сохраняю {len(cookies)} cookie без проверки API"
                        )
                        return cookies, last_info
                    raise ExtractorError(
                        f"не дождался входа за {timeout} с (cookie {session_cookie!r} не появилась)"
                    )
                if page.is_closed():
                    raise ExtractorError("окно браузера закрыто до завершения входа")

                if now - last_block_check >= 5:
                    if _page_is_blocked(page):
                        raise ExtractorError(
                            "Cloudflare заблокировал IP proxy (Sorry, you have been blocked). "
                            "Смените proxy или подключение."
                        )
                    last_block_check = now

                try:
                    raw_cookies = ctx.cookies()
                except Exception as exc:
                    raise ExtractorError(f"не удалось прочитать cookie из браузера: {exc}") from exc

                has_session = _has_session_cookie(raw_cookies, session_cookie, root_domain)
                if has_session:
                    if session_seen_at is None:
                        session_seen_at = now
                        log(f"[+] cookie {session_cookie!r} появилась, проверяю сессию...")

                    account_page = _find_account_page(ctx, page, verify_host)
                    if now - last_verify >= 2.0:
                        last_info = _verify_page_session(account_page, verify_host)
                        last_verify = now
                    if last_info and last_info.get("status") == 200:
                        time.sleep(1.2)
                        with suppress(Exception):
                            raw_cookies = ctx.cookies()
                        cookies = extract_cookies(raw_cookies, root_domain, session_cookie)
                        log(f"[+] сессия валидна (API 200), cookie: {len(cookies)}")
                        return cookies, last_info

                    # If the API is unavailable/blocked by Cloudflare but ssid exists
                    # and we are on account.riotgames.com, do not wait forever.
                    # 401/403 not_logged_in definitely means login is incomplete.
                    if (
                        session_seen_at is not None
                        and now - session_seen_at >= 10
                        and last_info is not None
                        and not _is_not_logged_in(last_info)
                    ):
                        time.sleep(1.0)
                        with suppress(Exception):
                            raw_cookies = ctx.cookies()
                        cookies = extract_cookies(raw_cookies, root_domain, session_cookie)
                        log(
                            f"[+] сессия принята по cookie (API не ответил), cookie: {len(cookies)}"
                        )
                        return cookies, last_info

                if now - last_report >= 15:
                    if deadline is None:
                        log("[*] жду вход... (без таймаута)")
                    else:
                        left = max(0, int(deadline - now))
                        log(f"[*] жду вход... осталось {left} с")
                    last_report = now
                time.sleep(1.0)
    except ExtractorError:
        raise
    except Exception as exc:
        raise ExtractorError(f"браузер: {exc}") from exc
