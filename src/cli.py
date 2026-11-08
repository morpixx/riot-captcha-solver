"""Command-line runner.

Examples:
    python -m src.cli --account bodyatymkiv
    python -m src.cli --cookies cookies.json --email new@gmail.com --submit
    python -m src.cli --cookies ... --email ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Account, load_config
from .proxy.proxy import parse_proxy
from .riot.flow import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", help="account name from accounts.json")
    parser.add_argument("--cookies", help="ad-hoc mode: JSON cookie file")
    parser.add_argument("--email", help="ad-hoc mode: new email address")
    parser.add_argument("--submit", action="store_true", help="actually POST the email change")
    parser.add_argument("--dry-run", action="store_true", help="mint token only, no POST")
    parser.add_argument("--proxy", help="socks5://user:pass@host:port or http://host:port")
    parser.add_argument(
        "--headless", action="store_true", help="run Camoufox without a visible window"
    )
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    args = parser.parse_args(argv)

    cfg = load_config()

    if args.cookies and args.email:
        account = Account(
            name="adhoc",
            cookies=Path(args.cookies),
            new_email=args.email,
            proxy=args.proxy,
        )
    elif args.account:
        account = cfg.account(args.account)
    else:
        parser.error("use --account NAME or both --cookies and --email")

    if isinstance(account.cookies, Path) and not account.cookies.is_file():
        parser.error(f"cookie file not found: {account.cookies}")

    submit = (args.submit or cfg.allow_email_change) and not args.dry_run
    result = run(
        cfg,
        account,
        submit=submit,
        proxy=parse_proxy(args.proxy or account.proxy),
        headless=True if args.headless else None,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
