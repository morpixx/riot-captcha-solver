#!/usr/bin/env python3
"""Построчная отправка аккаунтов в очередь: один аккаунт — один POST /api/v1/job.

Пример:
    python scripts/submit_accounts.py \
        --url https://<tunnel> --api-key $API_KEY \
        --input data/input/accounts.jsonl --out data/output/job-results.jsonl \
        --concurrency 8 --rate 10 \
        --webhook-url https://client.example/hook

Скрипт только ставит job'ы. Финальные результаты приходят на webhook; GET
/jobs остаётся для ручной диагностики, а не для постоянного опроса.
Вход — JSONL (по одному объекту на строку) или JSON-массив. Поля объекта:
external_id, cookies, proxy, new_email, sitekey?, headless?, webhook_url?.
Cookies/прокси/email в выходной файл не пишутся.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

_thread_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["ngrok-skip-browser-warning"] = "1"
        _thread_local.session = session
    return session


class RateLimiter:
    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


def load_items(path: Path, limit: int = 0) -> Iterator[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(stripped)
        if not isinstance(data, list):
            raise ValueError("input must be a JSON array or JSONL")
        for index, item in enumerate(data):
            item.setdefault("external_id", f"line-{index + 1}")
            yield item
            if limit and index + 1 >= limit:
                return
        return

    count = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"line {lineno}: expected object")
        item.setdefault("external_id", f"line-{lineno}")
        yield item
        count += 1
        if limit and count >= limit:
            return


def build_payload(
    item: dict[str, Any],
    lineno: int,
    *,
    allow_empty_proxy: bool = False,
    test_mode: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    missing = [key for key in ("cookies", "new_email") if not item.get(key)]
    if not item.get("proxy") and not allow_empty_proxy:
        missing.append("proxy")
    if missing:
        raise ValueError(f"item {lineno}: missing {', '.join(missing)}")
    return {
        "cookies": item["cookies"],
        "proxy": item.get("proxy") or "",
        "new_email": item["new_email"],
        "external_id": item.get("external_id"),
        "batch_id": item.get("batch_id") or batch_id,
        "sitekey": item.get("sitekey"),
        "headless": item.get("headless"),
        "webhook_url": item.get("webhook_url"),
        "test_mode": test_mode,
    }


def post_one(args, limiter: RateLimiter, payload: dict[str, Any]) -> dict[str, Any]:
    url = args.url.rstrip("/") + "/api/v1/job"
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    attempt = 0
    while attempt < 6:
        attempt += 1
        limiter.wait()
        try:
            response = _session().post(
                url, json=payload, headers=headers, timeout=args.request_timeout
            )
        except requests.RequestException as exc:
            if attempt >= 6:
                return {
                    "external_id": payload.get("external_id"),
                    "status": "submit_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            time.sleep(min(30, 2**attempt))
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "5")
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 5.0
            time.sleep(max(1.0, min(60.0, delay)))
            continue
        if response.status_code >= 500:
            if attempt >= 6:
                return {
                    "external_id": payload.get("external_id"),
                    "status": "submit_failed",
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }
            time.sleep(min(30, 2**attempt))
            continue
        if response.status_code not in (200, 201, 202):
            return {
                "external_id": payload.get("external_id"),
                "status": "submit_failed",
                "error": f"HTTP {response.status_code}: {response.text[:300]}",
            }
        try:
            return response.json()
        except ValueError:
            return {
                "external_id": payload.get("external_id"),
                "status": "submit_failed",
                "error": "response is not JSON",
            }

    return {
        "external_id": payload.get("external_id"),
        "status": "submit_failed",
        "error": "retries exhausted",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One POST per account -> /api/v1/job")
    parser.add_argument("--url", required=True, help="base URL (tunnel or localhost)")
    parser.add_argument("--api-key", default=os.getenv("API_KEY", ""), help="X-API-Key")
    parser.add_argument(
        "--allow-empty-proxy",
        action="store_true",
        help="отправлять без proxy (только если на сервере ALLOW_NO_PROXY=1)",
    )
    parser.add_argument(
        "--input", required=True, type=Path, help="data/input/accounts.jsonl / .json"
    )
    parser.add_argument("--out", type=Path, default=Path("data/output/job-results.jsonl"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--rate", type=float, default=10.0, help="requests per second, 0 = unlimited"
    )
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument(
        "--webhook-url",
        default="",
        help="webhook_url для каждого job'а (приёмник на стороне клиента)",
    )
    parser.add_argument("--limit", type=int, default=0, help="only first N input items")
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="allow these jobs to share one proxy concurrently (server must opt in)",
    )
    parser.add_argument(
        "--headless",
        choices=["keep", "true", "false"],
        default="true",
        help="override headless for every job (default true, keep = value from input)",
    )
    args = parser.parse_args(argv)

    limiter = RateLimiter(args.rate)
    submitted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        items = list(load_items(args.input, args.limit))
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    if not items:
        print("no input items", file=sys.stderr)
        return 2

    batch_id = uuid.uuid4().hex[:12]
    payloads = []
    for lineno, item in enumerate(items, start=1):
        try:
            payload = build_payload(
                item,
                lineno,
                allow_empty_proxy=args.allow_empty_proxy,
                test_mode=args.test_mode,
                batch_id=batch_id,
            )
            if args.headless != "keep":
                payload["headless"] = args.headless == "true"
            if args.webhook_url:
                payload["webhook_url"] = args.webhook_url
            payloads.append(payload)
        except ValueError as exc:
            print(f"skip: {exc}", file=sys.stderr)

    print(
        f"submitting {len(payloads)} accounts to {args.url} "
        f"(concurrency={args.concurrency}, rate={args.rate}/s, batch={batch_id})",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(post_one, args, limiter, payload) for payload in payloads]
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result.get("status") == "submit_failed":
                errors.append(result)
            else:
                submitted.append(result)
            if done % 100 == 0 or done == len(futures):
                print(f"submit: {done}/{len(futures)} (errors={len(errors)})", flush=True)

    job_ids = {job["id"] for job in submitted if job.get("id")}
    print(f"accepted={len(job_ids)} submit_errors={len(errors)}", flush=True)
    if not args.webhook_url:
        print(
            "webhook_url is not set; final results are available only via GET /jobs",
            file=sys.stderr,
        )
    if errors:
        for error in errors[:5]:
            print(
                f"  submit error {error.get('external_id')}: {error.get('error')}", file=sys.stderr
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(job, ensure_ascii=False) for job in submitted + errors) + "\n",
        encoding="utf-8",
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
