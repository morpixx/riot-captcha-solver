"""Webhook payload/signature and delivery worker tests (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace

from src.api.webhooks import WebhookDispatcher, WebhookSettings, build_body, build_headers


def test_build_headers_signs_the_exact_body():
    payload = {"id": "job-1", "status": "succeeded"}
    body = build_body(payload)
    headers = build_headers(payload, body, "secret")

    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert json.loads(body) == payload
    assert headers["X-Webhook-Signature"] == expected
    assert headers["X-Webhook-Id"] == "job-1"
    assert headers["X-Webhook-Event"] == "job.terminal"


def test_dispatcher_posts_payload_and_stops():
    calls: list[dict] = []

    def fake_post(url, *, data, headers, timeout):
        calls.append(
            {
                "url": url,
                "payload": json.loads(data),
                "headers": headers,
                "timeout": timeout,
            }
        )
        return SimpleNamespace(status_code=204)

    dispatcher = WebhookDispatcher(
        WebhookSettings(secret="", timeout=2.0, retries=1),
        post=fake_post,
    )
    dispatcher.start()
    dispatcher.enqueue("http://client.example/hook", {"id": "job-2", "status": "dead"})
    dispatcher.stop(drain_timeout=2.0)

    assert len(calls) == 1
    assert calls[0]["url"] == "http://client.example/hook"
    assert calls[0]["payload"]["id"] == "job-2"
    assert calls[0]["timeout"] == 2.0
    assert "X-Webhook-Signature" not in calls[0]["headers"]

def test_dispatcher_delivers_events_with_multiple_workers():
    calls: list[str] = []
    lock = threading.Lock()

    def fake_post(url, *, data, headers, timeout):
        time.sleep(0.05)
        with lock:
            calls.append(json.loads(data)["id"])
        return SimpleNamespace(status_code=204)

    dispatcher = WebhookDispatcher(
        WebhookSettings(secret="", timeout=1.0, retries=1, workers=4),
        post=fake_post,
    )
    dispatcher.start()
    for index in range(8):
        dispatcher.enqueue("http://client.example/hook", {"id": f"job-{index}", "status": "dead"})
    dispatcher.stop(drain_timeout=5.0)

    assert sorted(calls) == [f"job-{index}" for index in range(8)]
