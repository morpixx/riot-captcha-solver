#!/usr/bin/env python3
"""Приёмник webhook'ов от server.py.

Запуск:
  python scripts/webhook_receiver.py --port 9000 --out data/output/webhook-results.jsonl

URL приёмника можно передать двумя способами:
  1) клиент кладёт его в job: {"webhook_url": "http://127.0.0.1:9000/hook", ...}
  2) сервер использует его как общий fallback:
     WEBHOOK_URL=http://127.0.0.1:9000/hook

WEBHOOK_SECRET=...    # по желанию, но лучше задать; тем же секретом
                      # клиент проверяет X-Webhook-Signature.

Если сервер и приёмник на разных машинах, приёмник нужно выставить наружу
(ngrok/Tailscale), а в webhook_url/WEBHOOK_URL указать публичный URL.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTTP webhook receiver для riot-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--path", default="/hook")
    parser.add_argument("--out", type=Path, default=Path("data/output/webhook-results.jsonl"))
    parser.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))
    args = parser.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    secret = args.secret.encode("utf-8") if args.secret else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *values: Any) -> None:
            # не засоряем stdout стандартным логом каждого запроса
            return

        def _send(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != args.path:
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 2_000_000:
                self._send(400, {"error": "bad content length"})
                return
            body = self.rfile.read(length)
            if secret is not None:
                provided = self.headers.get("X-Webhook-Signature", "").strip()
                expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(provided, expected):
                    self._send(401, {"error": "bad signature"})
                    return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with (
                write_lock,
                args.out.open("a", encoding="utf-8", newline="\n") as handle,
            ):
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            job_id = str(payload.get("id") or "")[:8]
            print(
                f"[{now()}] {job_id} {payload.get('external_id')} "
                f"status={payload.get('status')} shouldPoll={payload.get('shouldPoll')} "
                f"riot_status={payload.get('riot_status')} error={payload.get('error_code')}",
                flush=True,
            )
            self._send(200, {"ok": True})

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[*] webhook receiver: http://{args.host}:{args.port}{args.path}")
    print(f"[*] results: {args.out.resolve()}")
    print(f"[*] signature: {'on' if secret else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
