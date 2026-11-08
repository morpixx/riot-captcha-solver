from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import DATA_OUTPUT, Config, load_config
from ..proxy.proxy import parse_proxy
from ..riot.flow import run

RESULTS = DATA_OUTPUT / "results.jsonl"


def _worker(account, cfg: Config, args, log) -> dict:
    proxy = parse_proxy(account.proxy) if account.proxy else None
    if proxy is None and not args.allow_shared_ip:
        return {
            "name": account.name,
            "error": "no proxy configured (use --allow-shared-ip to override)",
        }
    mode = {"off": False, "true": True, "virtual": "virtual"}[args.headless]
    if args.delay:
        time.sleep(random.uniform(0, args.delay))
    t0 = time.time()
    try:
        res = run(
            cfg,
            account,
            submit=not args.dry_run,
            proxy=proxy,
            headless=mode,
            log=lambda m: log(f"[{account.name}] {m}"),
        )
        res.update(name=account.name, seconds=round(time.time() - t0, 1))
    except Exception as exc:
        res = {
            "name": account.name,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.time() - t0, 1),
        }
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="local batch runner (no network API)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--delay", type=float, default=8.0, help="pause between accounts per worker")
    ap.add_argument("--stagger", type=float, default=2.0, help="stagger between worker starts")
    ap.add_argument("--dry-run", action="store_true", help="mint only, no POST")
    ap.add_argument(
        "--headless",
        choices=["off", "true", "virtual"],
        default="true",
        help="true=headless (default), off=headed, virtual=Linux Xvfb",
    )
    ap.add_argument("--allow-shared-ip", action="store_true", help="run without per-account proxy")
    ap.add_argument("--only", default="", help="comma-separated account names")
    args = ap.parse_args(argv)

    cfg = load_config()
    accounts = cfg.accounts
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        accounts = [a for a in accounts if a.name in wanted]
    if not accounts:
        print("no accounts selected")
        return 2

    lock = threading.Lock()

    def log(msg: str) -> None:
        with lock:
            print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    log(
        f"batch: {len(accounts)} accounts, backend=camoufox, "
        f"workers={args.workers}, headless={args.headless}, dry_run={args.dry_run}"
    )
    t0 = time.time()
    results = []
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = []
        for i, account in enumerate(accounts):
            if i and args.stagger:
                time.sleep(args.stagger)
            futures.append(pool.submit(_worker, account, cfg, args, log))
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            with lock:
                with RESULTS.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                log(
                    f"done {res.get('name')}: shouldPoll={res.get('shouldPoll')} error={res.get('error')}"
                )

    ok = sum(1 for r in results if r.get("shouldPoll") is True)
    log(f"finished {len(results)} accounts in {round(time.time() - t0, 1)}s; shouldPoll=true: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
