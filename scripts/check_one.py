#!/usr/bin/env python3
"""Smoke-test one account: Camoufox headless and optional shouldPoll.

Default is a dry run: the browser and hCaptcha are exercised, but no email
change is POSTed. Pass --submit to make the real request.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.config import Account, load_flow_config  # noqa: E402
from src.console import configure_utf8_stdio  # noqa: E402
from src.jobs.result import interpret_result  # noqa: E402
from src.proxy.proxy import describe, parse_proxy  # noqa: E402
from src.riot.flow import run  # noqa: E402
from src.security import email_domain, safe_error  # noqa: E402

configure_utf8_stdio()
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
            if not line.strip():
                continue
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
    parser.add_argument("--email", help="override new_email")
    parser.add_argument("--proxy", help="override proxy URL")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the Camoufox window (default: headless)",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="actually POST the email change; default is dry-run",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = Path(args.store)
    if not store.is_absolute():
        store = ROOT / store

    proxy_raw = args.proxy or ""
    try:
        record = pick_record(load_records(store), args.label)
        label = str(record.get("label") or record.get("external_id") or "one")
        cookies = record.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("record has no cookies array")

        email = args.email or record.get("new_email") or record.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise ValueError("record has no valid new_email; pass --email")

        proxy_raw = args.proxy or str(record.get("proxy") or "")
        proxy = parse_proxy(proxy_raw) if proxy_raw else None
        account = Account(
            name=label,
            cookies=cookies,
            new_email=email,
            proxy=proxy_raw or None,
        )

        cfg = load_flow_config()
        headless = not args.headed
        print(
            f"account={label} headless={headless} submit={args.submit} "
            f"email_domain={email_domain(email)} proxy={describe(proxy)}"
        )
        if args.submit:
            print("WARNING: --submit sends the real Riot email-change request")
        else:
            print("dry-run: browser + hCaptcha only; use --submit for shouldPoll")

        result = run(
            cfg,
            account,
            submit=args.submit,
            proxy=proxy,
            headless=headless,
            log=print,
        )

        if args.submit:
            outcome = interpret_result(result)
            details = outcome.details or {}
            payload = {
                "status": result.get("status"),
                "shouldPoll": outcome.should_poll,
                "error_code": outcome.error_code,
                "error": outcome.error,
                "message": details.get("message"),
                "mode": details.get("mode"),
            }
        else:
            payload = result

        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {safe_error(exc, proxy_raw)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
