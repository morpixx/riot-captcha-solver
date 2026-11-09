#!/usr/bin/env python3
"""Read-only audit of email status for every account in a store.

No POST, no email change: each account is opened in Camoufox and queried via
Riot's /api/account/v1/user. Useful to find accounts stuck in
email_status=not_validated / derived_states=[email_verification].
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.browser.browser import open_browser  # noqa: E402
from src.proxy.proxy import describe, parse_proxy  # noqa: E402
from src.riot.state import VERIFY_JS  # noqa: E402
from src.security import safe_error  # noqa: E402

load_dotenv(ROOT / ".env")


def load_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty store: {path}")
    if text.startswith("["):
        raw = json.loads(text)
        if not isinstance(raw, list):
            raise ValueError(f"store must be a JSON array: {path}")
        records = raw
    else:
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


def mask_email(value: object) -> str:
    text = str(value or "")
    local, sep, domain = text.partition("@")
    if not sep:
        return text
    return f"{local[:3]}***@{domain}"


def audit_one(record: dict, *, headed: bool) -> dict:
    label = str(record.get("label") or record.get("external_id") or "?")
    cookies = record.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("record has no cookies array")

    proxy_raw = str(record.get("proxy") or "")
    proxy = parse_proxy(proxy_raw) if proxy_raw else None

    with open_browser(
        cookies,
        headless=not headed,
        proxy=proxy,
        label=label,
    ) as (_ctx, page):
        page.goto(
            "https://account.riotgames.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(3000)
        info = page.evaluate(VERIFY_JS)

    body = info.get("body") if isinstance(info, dict) else None
    if not isinstance(body, dict):
        body = {}
    mfa = body.get("mfa") if isinstance(body.get("mfa"), dict) else {}
    states = body.get("derived_states")
    if not isinstance(states, list):
        states = []

    return {
        "label": label,
        "status": info.get("status") if isinstance(info, dict) else None,
        "email": mask_email(body.get("email")),
        "email_status": body.get("email_status"),
        "derived_states": states,
        "parent_email_pending": body.get("parent_email_pending"),
        "mfa_verified": mfa.get("verified"),
        "proxy": describe(proxy),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        default="data/input/accounts-test.jsonl",
        help="JSONL/JSON store with cookies and proxy",
    )
    parser.add_argument("--label", help="only this label/external_id")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the Camoufox window (default: headless)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON array")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    store = Path(args.store)
    if not store.is_absolute():
        store = ROOT / store

    try:
        records = load_records(store)
    except Exception as exc:
        print(f"ERROR: {safe_error(exc)}", file=sys.stderr)
        return 1

    if args.label:
        records = [
            item
            for item in records
            if args.label in {str(item.get("label") or ""), str(item.get("external_id") or "")}
        ]
        if not records:
            print(f"ERROR: label {args.label!r} not found in {store}", file=sys.stderr)
            return 1

    rows: list[dict] = []
    for record in records:
        label = str(record.get("label") or record.get("external_id") or "?")
        try:
            row = audit_one(record, headed=args.headed)
        except Exception as exc:
            row = {
                "label": label,
                "status": "ERR",
                "email": "",
                "email_status": None,
                "derived_states": [],
                "parent_email_pending": None,
                "mfa_verified": None,
                "proxy": "",
                "error": safe_error(exc, str(record.get("proxy") or "")),
            }
        rows.append(row)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            states = ",".join(str(item) for item in row.get("derived_states") or [])
            print(
                f"{row['label']:12} status={row.get('status')!s:>3} "
                f"email_status={row.get('email_status')!s:16} "
                f"email={row.get('email')!s:24} "
                f"states={states}"
            )
        counts = Counter(str(row.get("email_status") or "error") for row in rows)
        print("---")
        print(", ".join(f"{key}={value}" for key, value in sorted(counts.items())))

    return 0


if __name__ == "__main__":
    sys.exit(main())
