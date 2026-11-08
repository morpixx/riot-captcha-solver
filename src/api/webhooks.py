"""Background delivery of terminal job events to client webhooks.

Webhook URLs and payloads live only in memory. Delivery retries transient
failures without blocking the job queue or one another.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("riot.webhook")


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class WebhookSettings:
    secret: str = ""
    timeout: float = 5.0
    retries: int = 3


def build_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_headers(payload: dict[str, Any], body: bytes, secret: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "riot-api-webhook/1.0",
        "ngrok-skip-browser-warning": "1",
        "X-Webhook-Event": "job.terminal",
        "X-Webhook-Id": str(payload.get("id") or ""),
    }
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={signature}"
    return headers


class WebhookDispatcher:
    """A small single-worker queue used by the API lifespan."""

    def __init__(
        self,
        settings: WebhookSettings,
        *,
        post: Callable[..., Any] = requests.post,
        log_fn: logging.Logger = log,
    ) -> None:
        self.settings = settings
        self._post = post
        self._log = log_fn
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker,
            name="riot-webhook",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def enqueue(self, url: str, payload: dict[str, Any]) -> None:
        self._queue.put({"url": url, "payload": payload})

    def stop(self, *, drain_timeout: float = 5.0) -> None:
        """Try to drain queued events, then stop the worker."""
        deadline = time.monotonic() + max(0.0, drain_timeout)
        while time.monotonic() < deadline and self._queue.unfinished_tasks:
            time.sleep(0.05)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(str(item.get("url") or ""), item.get("payload") or {})
            finally:
                self._queue.task_done()

    def _deliver(self, url: str, payload: dict[str, Any]) -> None:
        if not url:
            return
        body = build_body(payload)
        headers = build_headers(payload, body, self.settings.secret)
        job_id = str(payload.get("id") or "")[:8]
        for attempt in range(1, self.settings.retries + 1):
            if self._stop.is_set():
                return
            try:
                response = self._post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=self.settings.timeout,
                )
                if 200 <= response.status_code < 300:
                    self._log.info(
                        "[%s] WEBHOOK job=%s status=%s -> %s",
                        _ts(),
                        job_id,
                        payload.get("status"),
                        response.status_code,
                    )
                    return
                self._log.warning(
                    "[%s] WEBHOOK job=%s attempt %d/%d -> HTTP %s",
                    _ts(),
                    job_id,
                    attempt,
                    self.settings.retries,
                    response.status_code,
                )
            except Exception as exc:
                self._log.warning(
                    "[%s] WEBHOOK job=%s attempt %d/%d failed: %s",
                    _ts(),
                    job_id,
                    attempt,
                    self.settings.retries,
                    exc,
                )
            if attempt < self.settings.retries:
                self._stop.wait(min(10.0, 0.5 * (2 ** (attempt - 1))))
        self._log.error("[%s] WEBHOOK job=%s not delivered", _ts(), job_id)


__all__ = ["WebhookDispatcher", "WebhookSettings", "build_body", "build_headers"]
