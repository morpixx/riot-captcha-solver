"""Минимальный webhook-клиент для одного job.

Локальный запуск (API и клиент на одной машине):

    set RIOT_API_URL=http://127.0.0.1:8000
    set RIOT_API_KEY=...
    python scripts/client_example.py job.json

Клиент поднимает приёмник http://127.0.0.1:9000/hook, передаёт его серверу в
webhook_url и ждёт POST с результатом. Сервер пришлёт shouldPoll, riot_status,
details, average_duration_ms и ошибки, когда job завершится.

Если API на другой машине, приёмник должен быть доступен снаружи; укажите
публичный URL (туннель/свой домен), сам приёмник останется локальным:

    python scripts/client_example.py job.json --public-url https://client.example/hook

Подпись X-Webhook-Signature проверяется, если задан RIOT_WEBHOOK_SECRET
(должен совпадать с WEBHOOK_SECRET на сервере).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

TERMINAL = {"succeeded", "dead", "canceled", "needs_resubmit"}


class WebhookState:
    def __init__(self, secret: str = ""):
        self.secret = secret.encode("utf-8") if secret else None
        self.job_id: str | None = None
        self.payload: dict[str, Any] | None = None
        self.event = threading.Event()
        self.lock = threading.Lock()


def receive_webhook(
    state: WebhookState, listen: str
) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    host, _, raw_port = listen.rpartition(":")
    if not host:
        host = "127.0.0.1"
    port = int(raw_port or "9000")
    path = "/hook"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *values: Any) -> None:
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
            if self.path != path:
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

            if state.secret is not None:
                provided = self.headers.get("X-Webhook-Signature", "").strip()
                expected = "sha256=" + hmac.new(state.secret, body, hashlib.sha256).hexdigest()
                # Проверяем подпись до разбора JSON.
                if not hmac.compare_digest(provided, expected):
                    self._send(401, {"error": "bad signature"})
                    return

            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send(400, {"error": "bad json"})
                return

            with state.lock:
                if state.job_id and payload.get("id") != state.job_id:
                    # Чужой/старый webhook: подтверждаем, но не считаем своим результатом.
                    self._send(202, {"ok": True, "ignored": True})
                    return
                state.payload = payload
                state.event.set()
            self._send(200, {"ok": True})

    server = ThreadingHTTPServer((host, port), Handler)
    actual_port = server.server_address[1]
    advertise_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    local_url = f"http://{advertise_host}:{actual_port}{path}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, local_url, thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Один job + ожидание webhook")
    parser.add_argument("job", help="JSON-файл с полями cookies/proxy/new_email")
    parser.add_argument(
        "--listen", default="127.0.0.1:9000", help="адрес локального webhook-приёмника (host:port)"
    )
    parser.add_argument(
        "--public-url", default="", help="URL, который передать серверу (если приёмник за туннелем)"
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="сколько ждать webhook, сек")
    parser.add_argument(
        "--secret",
        default=os.getenv("RIOT_WEBHOOK_SECRET", ""),
        help="WEBHOOK_SECRET сервера для проверки подписи",
    )
    args = parser.parse_args(argv)

    base = os.environ.get("RIOT_API_URL", "").rstrip("/")
    key = os.environ.get("RIOT_API_KEY", "")
    if not base or not key:
        sys.exit("set RIOT_API_URL and RIOT_API_KEY")

    with open(args.job, encoding="utf-8") as fh:
        job = json.load(fh)

    headers = {"X-API-Key": key, "ngrok-skip-browser-warning": "1"}
    state = WebhookState(args.secret)
    server, local_url, _thread = receive_webhook(state, args.listen)
    webhook_url = args.public_url or local_url
    job["webhook_url"] = webhook_url
    print(f"webhook listener: {local_url}")
    print(f"webhook_url sent: {webhook_url}")

    try:
        response = requests.post(f"{base}/api/v1/job", json=job, headers=headers, timeout=30)
        response.raise_for_status()
        created = response.json()
        job_id = created["id"]
        with state.lock:
            state.job_id = job_id
        print("queued:", job_id, created["status"])

        if created.get("status") in TERMINAL:
            result: dict[str, Any] | None = created
        elif state.event.wait(args.timeout):
            with state.lock:
                result = state.payload
        else:
            result = None
    finally:
        server.shutdown()
        server.server_close()

    if result is None:
        print("timeout waiting for webhook", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"shouldPoll={result.get('shouldPoll')}")
    return 0 if result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
