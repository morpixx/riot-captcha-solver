"""Riot account navigation: origins and the auth redirect chain."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

ACCOUNT = "https://account.riotgames.com"
LOGIN_HOST = "authenticate.riotgames.com"


def page_host(url: object) -> str:
    return (urlsplit(str(url)).hostname or "").lower()


def on_host(url: str, host: str) -> bool:
    """Exact host match; a query string must not look like an origin."""
    expected = str(host or "").strip().lower()
    actual = page_host(url)
    if not expected or not actual:
        return False
    return actual == expected or actual.endswith("." + expected)


def wait_for_account_page(page, host: str, timeout: float = 20.0) -> None:
    """Wait out the account -> auth -> account redirect chain.

    A valid session bounces through authenticate.riotgames.com and returns;
    an invalid one stays there. Polling lets a healthy session finish the
    chain while a dead one still fails in a couple of seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = str(page.url)
        current = page_host(url)
        if current == host:
            return
        if current == LOGIN_HOST:
            raise RuntimeError(f"Riot session is not valid: landed on {url[:80]}")
        page.wait_for_timeout(500)
