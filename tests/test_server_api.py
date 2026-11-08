"""HTTP-тесты job-API с подменённым runner (без браузера)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import types

from fastapi.testclient import TestClient

ME = "u1@example.com"


def _payload(proxy="socks5://1.2.3.4:1080", external_id="http-1"):
    return {
        "external_id": external_id,
        "cookies": [{"name": "ssid", "value": "v", "domain": ".riotgames.com", "path": "/"}],
        "proxy": proxy,
        "new_email": ME,
        "headless": False,
    }


def test_single_job_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    def fake_runner(cfg, account, proxy, headless):
        return {"status": 200, "shouldPoll": True, "body": {"message": "email_verification_sent"}}

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "QUEUE_RUNNER", fake_runner)
    headers = {"X-API-Key": "test-key"}

    with TestClient(server.app) as client:
        assert client.post("/api/v1/job", json=_payload()).status_code == 401

        response = client.post("/api/v1/job", json=_payload(), headers=headers)
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] in ("queued", "running", "succeeded"), job
        assert job["external_id"] == "http-1"

        deadline = time.time() + 5
        current = job
        while time.time() < deadline:
            current = client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()
            if current["status"] in ("succeeded", "dead"):
                break
            time.sleep(0.02)
        assert current["status"] == "succeeded", current
        assert current["shouldPoll"] is True
        assert current["details"]["message"] == "email_verification_sent"

        duplicate = client.post("/api/v1/job", json=_payload(), headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == job["id"]

        bad_proxy = client.post(
            "/api/v1/job",
            json=_payload(proxy="socks5://127.0.0.1:1080", external_id="http-2"),
            headers=headers,
        )
        assert bad_proxy.status_code == 400, bad_proxy.text

        stats = client.get("/api/v1/stats", headers=headers).json()
        assert stats["jobs"]["succeeded"] == 1, stats
        assert client.get("/api/v1/stats", headers={"X-API-Key": "nope"}).status_code == 401


def test_build_response_flags_should_poll_false():
    import server

    rejected = server.build_response(
        {
            "status": 200,
            "shouldPoll": False,
            "body": {"message": "email_verification_sent", "mode": "update"},
        }
    )
    assert rejected.shouldPoll is False
    assert rejected.status == 200
    assert rejected.error_code == "email_change_not_accepted"
    assert "shouldPoll=false" in rejected.error

    accepted = server.build_response({"status": 200, "shouldPoll": True, "body": {"message": "ok"}})
    assert accepted.shouldPoll is True
    assert accepted.error_code is None
    assert accepted.error is None

    leaked = server.build_response(
        {
            "status": 500,
            "error": "proxy socks5://user:pass@1.2.3.4:1080 failed",
        }
    )
    assert "user:pass" not in leaked.error
    assert "***@" in leaked.error


def _payload_with_webhook(webhook_url: str, external_id: str = "http-wh") -> dict:
    payload = _payload(
        proxy="socks5://proxy-user:proxy-secret@1.2.3.4:1080", external_id=external_id
    )
    payload["webhook_url"] = webhook_url
    return payload


def test_job_webhook_gets_terminal_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    deliveries: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        deliveries.append(
            {
                "url": url,
                "raw": data,
                "payload": json.loads(data.decode("utf-8")),
                "headers": dict(headers or {}),
            }
        )
        return types.SimpleNamespace(status_code=200)

    def fake_runner(cfg, account, proxy, headless):
        return {
            "status": 200,
            "shouldPoll": False,
            "body": {"message": "email_verification_sent", "mode": "update"},
        }

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "QUEUE_RUNNER", fake_runner)
    monkeypatch.setattr(server, "WEBHOOK_URL", "")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr(server, "WEBHOOK_RETRIES", 1)
    monkeypatch.setattr(server.requests, "post", fake_post)
    headers = {"X-API-Key": "test-key"}
    hook = "http://127.0.0.1:9000/hook"

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/job", json=_payload_with_webhook(hook, "wh-1"), headers=headers
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]

        deadline = time.time() + 5
        while time.time() < deadline and not deliveries:
            time.sleep(0.02)

        bad = client.post(
            "/api/v1/job",
            json=_payload_with_webhook("ftp://example.com/hook", "wh-bad"),
            headers=headers,
        )
        assert bad.status_code == 422, bad.text

    assert len(deliveries) == 1, deliveries
    delivered = deliveries[0]
    assert delivered["url"] == hook
    payload = delivered["payload"]
    assert payload["event"] == "job.terminal"
    assert payload["id"] == job_id
    assert payload["external_id"] == "wh-1"
    assert payload["status"] == "needs_resubmit"
    assert "average_duration_ms" in payload
    assert payload["batch_id"]
    assert payload["batch_total"] == 1
    assert payload["batch_terminal"] == 1
    assert payload["batch_succeeded"] == 0
    assert payload["batch_failed"] == 1
    assert payload["batch_remaining"] == 0
    assert payload["batch_average_duration_ms"] is not None
    assert payload["shouldPoll"] is False
    assert payload["riot_status"] == 200
    assert payload["error_code"] == "email_change_not_accepted"
    assert "shouldPoll=false" in payload["error"]
    assert payload["details"]["message"] == "email_verification_sent"
    assert "proxy" not in payload
    assert "proxy-secret" not in delivered["raw"].decode("utf-8")

    expected = "sha256=" + hmac.new(b"hook-secret", delivered["raw"], hashlib.sha256).hexdigest()
    assert delivered["headers"]["X-Webhook-Signature"] == expected
    assert delivered["headers"]["X-Webhook-Event"] == "job.terminal"
    assert delivered["headers"]["X-Webhook-Id"] == job_id


def test_batch_webhook_url_and_item_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    deliveries: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        deliveries.append(
            {
                "url": url,
                "payload": json.loads(data.decode("utf-8")),
            }
        )
        return types.SimpleNamespace(status_code=200)

    def fake_runner(cfg, account, proxy, headless):
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "QUEUE_RUNNER", fake_runner)
    monkeypatch.setattr(server, "WEBHOOK_URL", "")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(server, "WEBHOOK_RETRIES", 1)
    monkeypatch.setattr(server.requests, "post", fake_post)
    headers = {"X-API-Key": "test-key"}

    first = _payload(proxy="socks5://1.2.3.4:1080", external_id="batch-1")
    second = _payload(proxy="socks5://5.6.7.8:1080", external_id="batch-2")
    second["webhook_url"] = "http://item.example/hook"
    batch = {
        "webhook_url": "http://batch.example/hook",
        "jobs": [first, second],
    }

    with TestClient(server.app) as client:
        response = client.post("/api/v1/jobs", json=batch, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 2
        deadline = time.time() + 5
        while time.time() < deadline and len(deliveries) < 2:
            time.sleep(0.02)

    assert len(deliveries) == 2, deliveries
    by_external = {item["payload"]["external_id"]: item["url"] for item in deliveries}
    assert by_external == {
        "batch-1": "http://batch.example/hook",
        "batch-2": "http://item.example/hook",
    }
    batch_ids = {item["payload"]["batch_id"] for item in deliveries}
    assert len(batch_ids) == 1
    for item in deliveries:
        assert item["payload"]["batch_total"] == 2
        assert 1 <= item["payload"]["batch_terminal"] <= 2
        assert 1 <= item["payload"]["batch_succeeded"] <= 2
        assert item["payload"]["batch_average_duration_ms"] is not None


def test_job_webhook_falls_back_to_global_url(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    deliveries: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        deliveries.append(
            {
                "url": url,
                "payload": json.loads(data.decode("utf-8")),
            }
        )
        return types.SimpleNamespace(status_code=200)

    def fake_runner(cfg, account, proxy, headless):
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "QUEUE_RUNNER", fake_runner)
    monkeypatch.setattr(server, "WEBHOOK_URL", "http://global.example/hook")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(server, "WEBHOOK_RETRIES", 1)
    monkeypatch.setattr(server.requests, "post", fake_post)
    headers = {"X-API-Key": "test-key"}

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/job", json=_payload(external_id="wh-global"), headers=headers
        )
        assert response.status_code == 202, response.text
        deadline = time.time() + 5
        while time.time() < deadline and not deliveries:
            time.sleep(0.02)

    assert len(deliveries) == 1, deliveries
    assert deliveries[0]["url"] == "http://global.example/hook"
    assert deliveries[0]["payload"]["status"] == "succeeded"
    assert deliveries[0]["payload"]["shouldPoll"] is True


def test_sync_run_returns_shared_result_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("ENABLE_SYNC_RUN", "1")
    monkeypatch.setenv("ALLOW_NO_PROXY", "0")
    import server

    def fake_execute(cfg, account, proxy, headless):
        assert account.new_email == ME
        return {
            "status": 200,
            "shouldPoll": True,
            "body": {"message": "email_verification_sent", "mode": "update"},
        }

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "execute_run", fake_execute)
    headers = {"X-API-Key": "test-key"}

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/run",
            json=_payload(),
            headers=headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shouldPoll"] is True
    assert body["status"] == 200
    assert body["details"]["mode"] == "update"
    assert body["error_code"] is None


def test_test_mode_requires_server_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    monkeypatch.setattr(server, "API_KEYS_MAP", {"test-key": "client1"})
    monkeypatch.setattr(server, "ALLOW_SHARED_PROXY_TEST", False)
    payload = {"test_mode": True, "jobs": [_payload(external_id="shared-gate")]}

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"X-API-Key": "test-key"},
        )

    assert response.status_code == 400
    assert "ALLOW_SHARED_PROXY_TEST" in response.json()["detail"]


def test_webhook_proxy_forwards_to_local_receiver(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    seen: dict = {}

    def fake_post(url, *, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return types.SimpleNamespace(
            status_code=200,
            content=b'{"ok":true}',
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(server, "WEBHOOK_PROXY_URL", "http://127.0.0.1:9000/hook")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(server.requests, "post", fake_post)

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/webhook",
            content=b'{"event":"job.terminal"}',
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": "sha256=test",
            },
        )

    assert response.status_code == 200
    assert seen["url"] == "http://127.0.0.1:9000/hook"
    assert seen["data"] == b'{"event":"job.terminal"}'
    assert seen["headers"]["x-webhook-signature"] == "sha256=test"
    assert response.json() == {"ok": True}


def test_webhook_stores_to_journal_on_same_port(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    journal = tmp_path / "webhook-results.jsonl"
    monkeypatch.setattr(server, "WEBHOOK_PROXY_URL", "")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(server, "WEBHOOK_JOURNAL", journal)

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/webhook",
            json={"event": "job.terminal", "id": "abc", "external_id": "http-wh"},
        )

    assert response.status_code == 200
    assert response.json()["stored"] is True
    records = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records == [{"event": "job.terminal", "id": "abc", "external_id": "http-wh"}]


def test_webhook_signature_is_checked_when_secret_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    journal = tmp_path / "webhook-results.jsonl"
    monkeypatch.setattr(server, "WEBHOOK_PROXY_URL", "")
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(server, "WEBHOOK_JOURNAL", journal)
    body = b'{"event":"job.terminal"}'

    with TestClient(server.app) as client:
        rejected = client.post(
            "/api/v1/webhook",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        accepted = client.post(
            "/api/v1/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_webhook_rejects_forwarding_to_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    import server

    monkeypatch.setattr(server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(server, "WEBHOOK_PROXY_URL", "http://127.0.0.1:8000/api/v1/webhook")

    with TestClient(server.app) as client:
        response = client.post(
            "/api/v1/webhook",
            json={"event": "job.terminal"},
        )

    assert response.status_code == 503
    assert "point back" in response.json()["detail"]
