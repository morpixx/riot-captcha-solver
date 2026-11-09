#!/usr/bin/env python3
"""Open a stored Riot account in a headed Camoufox window.

The saved cookies are loaded into a fresh profile, so a valid session needs no
login or password. Nothing is POSTed: the window stays open for manual work
(verify the current email, inspect the page, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.browser.browser import open_browser  # noqa: E402
from src.proxy.proxy import describe, parse_proxy  # noqa: E402
from src.riot.navigation import ACCOUNT  # noqa: E402
from src.riot.state import VERIFY_JS  # noqa: E402

load_dotenv(ROOT / ".env")


def load_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty store: {path}")
    if text.startswith("["):
        raw = json.loads(text)
        if not isinstance(raw, list):
            raise ValueError(f"store must be a JSON array: {path}")
        return raw
    records = []
    for line in text.splitlines():
        if line.strip():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"store lines must be JSON objects: {path}")
            records.append(item)
    if not records:
        raise ValueError(f"no records in {path}")
    return records


def pick_record(records: list[dict], label: str | None) -> dict:
    if not label:
        return records[0]
    for item in records:
        if label in {str(item.get("label") or ""), str(item.get("external_id") or "")}:
            return item
    known = [str(item.get("label") or item.get("external_id") or "?") for item in records]
    raise ValueError(f"label {label!r} not found; known: {known}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        default="data/input/accounts-test.jsonl",
        help="JSONL/JSON store with cookies and proxy",
    )
    parser.add_argument("--label", help="label/external_id; default: first record")
    parser.add_argument("--proxy", help="override proxy URL")
    parser.add_argument(
        "--block-resources",
        action="store_true",
        help="enable the bot's image/font blocking (off by default for manual inspection)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print console errors, page errors and failed HTTP responses",
    )
    args = parser.parse_args(argv)

    # The bot blocks images/fonts/media to save bandwidth, but that leaves Riot's
    # Material Icons font unloaded and shows ligature names like "arrow_drop_down".
    # Manual inspection should render the real page, so blocking is opt-in here.
    os.environ["CAMOUFOX_BLOCK_RESOURCES"] = "1" if args.block_resources else "0"

    store = Path(args.store)
    if not store.is_absolute():
        store = ROOT / store

    record = pick_record(load_records(store), args.label)
    label = str(record.get("label") or record.get("external_id") or "one")
    cookies = record.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        print("ERROR: record has no cookies array", file=sys.stderr)
        return 1

    proxy_raw = args.proxy or str(record.get("proxy") or "")
    proxy = parse_proxy(proxy_raw) if proxy_raw else None
    print(f"account={label} proxy={describe(proxy)}", flush=True)
    print(
        "Открываю headed Camoufox с сохранёнными cookie; логин/пароль не нужен.",
        flush=True,
    )

    with open_browser(cookies, headless=False, proxy=proxy, label=label) as (_ctx, page):
        if args.debug:

            def on_console(message):
                if message.type in ("error", "warning"):
                    print(f"[console:{message.type}] {message.text}", flush=True)

            def on_pageerror(exc):
                print(f"[pageerror] {exc}", flush=True)

            def on_requestfailed(request):
                print(
                    f"[requestfailed] {request.url} {getattr(request, 'failure', '')}",
                    flush=True,
                )

            def on_response(response):
                if response.status >= 400:
                    print(f"[http {response.status}] {response.url}", flush=True)

            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)

        page.goto(ACCOUNT + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        info = page.evaluate(VERIFY_JS)
        body = info.get("body") if isinstance(info, dict) else None
        body = body if isinstance(body, dict) else {}
        print(
            "session status={status} email={email!r} email_status={email_status!r} "
            "states={states!r}".format(
                status=info.get("status") if isinstance(info, dict) else None,
                email=body.get("email"),
                email_status=body.get("email_status"),
                states=body.get("derived_states"),
            ),
            flush=True,
        )
        print("Окно открыто. Закрой окно браузера, чтобы выйти.", flush=True)
        try:
            while not page.is_closed():
                page.wait_for_timeout(1000)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
