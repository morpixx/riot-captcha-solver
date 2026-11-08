"""Run one account synchronously.

The browser layer already guarantees fingerprint + GeoIP; there is no
fallback that disables either of them.  A launch failure is reported as-is.
"""

from __future__ import annotations

import logging

from ..config import Account, Config
from ..riot.flow import run

log = logging.getLogger("riot.runner")


def run_account(
    cfg: Config,
    account: Account,
    proxy: dict | None,
    headless: bool | None = None,
) -> dict:
    return run(
        cfg,
        account,
        submit=True,
        proxy=proxy,
        headless=headless,
        log=log.info,
    )
