"""Cookie loading and the Camoufox browser entrypoint.

Camoufox is the only supported browser backend.  It always generates a real
fingerprint and aligns timezone/locale/geolocation through GeoIP; there is no
code path that disables either of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from .camoufox_browser import open_browser

BACKEND = "camoufox"


def load_cookies(path: str | Path) -> list[dict]:
    """Load a Playwright-style cookie array from disk."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("cookie file must be a JSON array")

    cookies: list[dict] = []
    for item in raw:
        cookie = {
            "name": item["name"],
            "value": item["value"],
            "domain": item.get("domain", ".riotgames.com"),
            "path": item.get("path", "/"),
        }
        if item.get("expires") is not None:
            cookie["expires"] = item["expires"]
        if item.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(item["httpOnly"])
        if item.get("secure") is not None:
            cookie["secure"] = bool(item["secure"])
        cookies.append(cookie)
    return cookies


__all__ = ["BACKEND", "load_cookies", "open_browser"]
