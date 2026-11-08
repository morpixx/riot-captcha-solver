from __future__ import annotations

import time
from urllib.parse import urlsplit

from ..browser.browser import load_cookies, open_browser
from ..browser.camoufox_boost import humanize_page
from ..config import Account, Config, env_flag
from ..proxy.proxy import describe, parse_proxy
from ..security import email_domain
from .minter import mint_in_page

ACCOUNT = "https://account.riotgames.com"
LOGIN_HOST = "authenticate.riotgames.com"

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
    return {status: r.status, body: body};
  } catch (e) {
    return {status: 0, error: String(e)};
  }
}
"""

POST_JS = r"""
async ({email, token}) => {
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const r = await fetch('/api/account/v1/user/email', {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'csrf-token': csrf},
    body: JSON.stringify({email: email, captcha: {token: 'hcaptcha ' + token}}),
  });
  let body;
  try { body = await r.json(); } catch (e) { body = {text: (await r.text()).slice(0, 300)}; }
  return {status: r.status, body: body};
}
"""


def _account_cookies(account: Account) -> list[dict]:
    if isinstance(account.cookies, list):
        return [dict(cookie) for cookie in account.cookies]
    return load_cookies(account.cookies)


def _page_host(url: str) -> str:
    return (urlsplit(str(url)).hostname or "").lower()


def _on_host(url: str, host: str) -> bool:
    """Exact host match; a query string must not look like an origin."""
    expected = str(host or "").strip().lower()
    actual = _page_host(url)
    if not expected or not actual:
        return False
    return actual == expected or actual.endswith("." + expected)


def _session_is_rejected(info: dict | None) -> bool:
    """True when Riot's own API says the cookie session is not valid."""
    if not info:
        return False
    status = info.get("status")
    if status == 401:
        return True
    if status != 403:
        return False
    body = info.get("body")
    if not isinstance(body, dict):
        return True
    code = str(body.get("errorCode") or body.get("error_code") or "").lower()
    return code in ("not_logged_in", "invalid_session_id")


def _verify_session(page, log) -> None:
    """Reject a stale cookie session before spending time on hCaptcha.

    The account page can render first and redirect client-side afterwards, so
    an origin check alone is not enough: ask the API that the page uses.
    """
    try:
        info = page.evaluate(VERIFY_JS)
    except Exception as exc:
        log(f"[*] session check skipped: {exc}")
        return
    if _session_is_rejected(info):
        detail = f"{info.get('status')} {info.get('body')!r}"
        raise RuntimeError(
            ("Riot session is not valid: /api/account/v1/user returned " + detail)[:200]
        )


def _wait_for_account_page(page, host: str, timeout: float = 20.0) -> None:
    """Wait out the account -> auth -> account redirect chain.

    A valid session bounces through authenticate.riotgames.com and returns;
    an invalid one stays there.  Polling lets a healthy session finish the
    chain while a dead one still fails in a couple of seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = str(page.url)
        current = _page_host(url)
        if current == host:
            return
        if current == LOGIN_HOST:
            raise RuntimeError(f"Riot session is not valid: landed on {url[:80]}")
        page.wait_for_timeout(500)


def run(
    cfg: Config,
    account: Account,
    *,
    submit: bool = True,
    proxy: dict | None = None,
    headless: bool | str | None = None,
    log=print,
) -> dict:
    """Mint an hCaptcha token on Riot's page and submit the email change."""
    proxy = proxy if proxy is not None else parse_proxy(account.proxy)
    log(f"[*] backend=camoufox proxy={describe(proxy)} fingerprint=on geoip=on submit={submit}")

    cookies = _account_cookies(account)
    mode = cfg.headless if headless is None else headless
    with open_browser(
        cookies,
        headless=mode,
        proxy=proxy,
        label=account.name,
    ) as (_ctx, page):
        page.goto(ACCOUNT + "/", wait_until="domcontentloaded", timeout=60000)
        _wait_for_account_page(page, cfg.host)
        page.wait_for_timeout(3000)
        if not _on_host(page.url, cfg.host):
            raise RuntimeError(f"Riot session is not valid: landed on {page.url[:80]}")

        _verify_session(page, log)
        if not _on_host(page.url, cfg.host):
            raise RuntimeError(f"Riot session is not valid: landed on {page.url[:80]}")

        if env_flag("CAMOUFOX_HUMANIZE", default=True):
            humanize_page(page, log=log)
        if not _on_host(page.url, cfg.host):
            raise RuntimeError(f"Riot session is not valid: landed on {page.url[:80]}")

        minted = mint_in_page(
            page,
            sitekey=cfg.sitekey,
            host=cfg.host,
            timeout=cfg.captcha_timeout,
            log=log,
        )
        token = minted["token"]

        if not submit:
            log("dry-run: token minted, POST skipped")
            return {
                "dry_run": True,
                "token_len": len(token),
                "host": minted.get("host"),
                "csrf": bool(minted.get("csrf")),
            }

        log(f"[post] fetch from page -> domain={email_domain(account.new_email)}")
        result = page.evaluate(POST_JS, {"email": account.new_email, "token": token})

    body = result.get("body") or {}
    should_poll = body.get("shouldPoll", body.get("should_poll"))
    log(
        f"[post] status={result.get('status')} message={body.get('message')!r} "
        f"mode={body.get('mode')!r} shouldPoll={should_poll!r}"
    )
    return {"status": result.get("status"), "shouldPoll": should_poll, "body": body}
