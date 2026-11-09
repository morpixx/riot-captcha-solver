"""Orchestration of one Riot email-change run.

Browser startup, navigation, state checks, the SAVE AND VERIFY click and the
dry-run minter each live in their own module; this file only decides the
order and turns the result into the shared {status, shouldPoll, body} shape.
"""

from __future__ import annotations

import time

from ..browser.browser import load_cookies, open_browser
from ..browser.camoufox_boost import humanize_page
from ..config import Account, Config, env_flag
from ..proxy.proxy import describe, parse_proxy
from . import email_form
from .minter import mint_in_page
from .navigation import ACCOUNT, on_host, wait_for_account_page
from .state import email_verification_problem, pending_email_problem, verify_session


def _account_cookies(account: Account) -> list[dict]:
    if isinstance(account.cookies, list):
        return [dict(cookie) for cookie in account.cookies]
    return load_cookies(account.cookies)


def _require_account_origin(page, host: str) -> None:
    if not on_host(page.url, host):
        raise RuntimeError(f"Riot session is not valid: landed on {page.url[:80]}")


def _dry_run(page, cfg: Config, log) -> dict:
    minted = mint_in_page(
        page,
        sitekey=cfg.sitekey,
        host=cfg.host,
        timeout=cfg.captcha_timeout,
        log=log,
    )
    token = minted["token"]
    log("dry-run: token minted, POST skipped")
    return {
        "dry_run": True,
        "token_len": len(token),
        "host": minted.get("host"),
        "csrf": bool(minted.get("csrf")),
    }


def run(
    cfg: Config,
    account: Account,
    *,
    submit: bool = True,
    proxy: dict | None = None,
    headless: bool | str | None = None,
    log=print,
) -> dict:
    """Submit the email change through Riot's own UI and report shouldPoll."""
    started = time.perf_counter()
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
        wait_for_account_page(page, cfg.host)
        _require_account_origin(page, cfg.host)

        session = verify_session(page, log)
        _require_account_origin(page, cfg.host)
        email_problem = email_verification_problem(session)
        if email_problem:
            if submit:
                raise RuntimeError(email_problem)
            log(f"[!] {email_problem}; dry-run continues")

        if submit:
            pending_problem = pending_email_problem(page, log)
            if pending_problem:
                raise RuntimeError(pending_problem)

        # The real UI click is the gesture Riot's hCaptcha widget expects.
        # Synthetic mouse movement before it can taint the n-token, so the
        # humanizer is kept for the dry-run minter path only.
        if env_flag("CAMOUFOX_HUMANIZE", default=True) and not submit:
            humanize_page(page, log=log)
        _require_account_origin(page, cfg.host)
        if not submit:
            return _dry_run(page, cfg, log)

        result = email_form.submit_via_ui(
            page,
            account.new_email,
            timeout_ms=max(60, int(cfg.captcha_timeout)) * 1000,
            log=log,
        )

    body = result.get("body") or {}
    should_poll = body.get("shouldPoll", body.get("should_poll"))
    log(
        f"[post] status={result.get('status')} message={body.get('message')!r} "
        f"mode={body.get('mode')!r} shouldPoll={should_poll!r}"
    )
    log(f"[timing] total={time.perf_counter() - started:.1f}s")
    return {"status": result.get("status"), "shouldPoll": should_poll, "body": body}
