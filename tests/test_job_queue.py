"""Тесты очереди job'ов (без браузера, с подменённым runner)."""

from __future__ import annotations

import threading
import time

import src.jobs.queue as job_queue_module
from src.config import Config
from src.jobs.payload import proxy_key
from src.jobs.queue import JobQueue, JobSpec
from src.proxy.proxy import parse_proxy

CFG = Config(
    sitekey="test",
    host="account.riotgames.com",
    headless=False,
    allow_email_change=True,
    captcha_timeout=150,
    accounts=[],
)


def _spec(i: int, webhook_url: str | None = None) -> JobSpec:
    return JobSpec(
        cookies=[{"name": "ssid", "value": f"v-{i}", "domain": ".riotgames.com", "path": "/"}],
        proxy=f"socks5://p{i}.example.com:1080",
        new_email=f"u{i}@example.com",
        external_id=f"e{i}",
        webhook_url=webhook_url,
    )


def _wait_done(queue: JobQueue, client: str, total: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = queue.stats(client_id=client)
        jobs = stats["jobs"]
        settled = jobs["succeeded"] + jobs["dead"] + jobs["canceled"] + jobs["needs_resubmit"]
        if (
            jobs["running"] == 0
            and jobs["queued"] == 0
            and jobs["retry_wait"] == 0
            and settled >= total
        ):
            return stats
        time.sleep(0.02)
    raise AssertionError(f"queue did not finish: {queue.stats(client_id=client)}")


def _queue(tmp_path, runner, **kwargs) -> JobQueue:
    return JobQueue(
        CFG,
        2,
        db_path=tmp_path / "jobs.sqlite3",
        ramp_interval=0,
        min_free_mb=0,
        runner=runner,
        **kwargs,
    )


def test_success_and_idempotency(tmp_path):
    seen: list[str] = []

    def runner(cfg, account, proxy, headless):
        seen.append(account.new_email)
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    queue = _queue(tmp_path, runner)
    queue.start()
    try:
        jobs, duplicates, errors = queue.submit("client", [_spec(1), _spec(2)])
        assert not errors and duplicates == 0 and len(jobs) == 2
        stats = _wait_done(queue, "client", 2)
        assert stats["jobs"]["succeeded"] == 2
        assert stats["durations_ms"]["count"] == 2
        assert stats["durations_ms"]["avg"] is not None
        assert sorted(seen) == ["u1@example.com", "u2@example.com"]

        # повторный submit того же external_id — не новая джоба
        again, duplicates, errors = queue.submit("client", [_spec(1)])
        assert duplicates == 1 and not errors and len(again) == 1
        assert again[0].status == "succeeded"
        assert queue.get(again[0].id, "client").should_poll is True
    finally:
        queue.close()


def test_retry_then_success(tmp_path):
    calls = {"n": 0}

    def runner(cfg, account, proxy, headless):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("page load timed out")
        return {"status": 200, "shouldPoll": True, "body": {"message": "retry ok"}}

    queue = _queue(tmp_path, runner, retry_base=0.01)
    queue.start()
    try:
        jobs, _, errors = queue.submit("client", [_spec(3)])
        assert not errors and len(jobs) == 1
        stats = _wait_done(queue, "client", 1)
        assert stats["jobs"]["succeeded"] == 1
        job = queue.get(jobs[0].id, "client")
        assert job.attempts == 2
        assert calls["n"] == 2
        assert job.status == "succeeded"
    finally:
        queue.close()


def test_should_poll_false_needs_resubmit(tmp_path):
    def runner(cfg, account, proxy, headless):
        return {
            "status": 200,
            "shouldPoll": False,
            "body": {"message": "email_verification_sent", "mode": "update"},
        }

    events = []
    queue = _queue(tmp_path, runner)
    queue.terminal_callback = events.append
    queue.start()
    try:
        jobs, _, errors = queue.submit(
            "client", [_spec(5, webhook_url="http://client.example/hook")]
        )
        assert not errors and len(jobs) == 1
        stats = _wait_done(queue, "client", 1)
        assert stats["jobs"]["needs_resubmit"] == 1

        job = queue.get(jobs[0].id, "client")
        assert job.status == "needs_resubmit"
        assert job.should_poll is False
        assert job.riot_status == 200
        assert job.error_code == "email_change_not_accepted"
        assert "shouldPoll=false" in job.error
        assert job.details["message"] == "email_verification_sent"
        assert job.finished_at is not None
        assert job.attempts == 1
    finally:
        queue.close()

    assert len(events) == 1
    assert events[0].status == "needs_resubmit"
    assert events[0].error_code == "email_change_not_accepted"
    assert events[0].webhook_url == "http://client.example/hook"


def test_legacy_succeeded_should_poll_false_is_resubmitted(tmp_path):
    calls = {"n": 0}

    def runner(cfg, account, proxy, headless):
        calls["n"] += 1
        return {
            "status": 200,
            "shouldPoll": False,
            "body": {"message": "email_verification_sent"},
        }

    queue = _queue(tmp_path, runner, retry_base=0.01)
    queue.start()
    try:
        jobs, _, errors = queue.submit("client", [_spec(6)])
        assert not errors and len(jobs) == 1
        _wait_done(queue, "client", 1)
        assert queue.get(jobs[0].id, "client").status == "needs_resubmit"

        # Симулируем строку, созданную старой версией: false был записан как succeeded.
        with queue.store.lock:
            queue.store.connection.execute(
                "UPDATE jobs SET status='succeeded', error_code=NULL, error=NULL WHERE id=?",
                (jobs[0].id,),
            )

        again, duplicates, errors = queue.submit("client", [_spec(6)])
        assert not errors and duplicates == 0 and len(again) == 1
        assert again[0].status in ("queued", "running")
        _wait_done(queue, "client", 1)
        assert calls["n"] == 2
    finally:
        queue.close()


def test_terminal_callback_gets_webhook_only_after_retry(tmp_path):
    calls = {"n": 0}
    events = []

    def runner(cfg, account, proxy, headless):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("page load timed out")
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    queue = _queue(tmp_path, runner, retry_base=0.01)
    queue.terminal_callback = events.append
    queue.start()
    try:
        _, _, errors = queue.submit("client", [_spec(4, webhook_url="http://client.example/hook")])
        assert not errors
        _wait_done(queue, "client", 1)
    finally:
        queue.close()

    # Первая попытка упала в retry_wait — webhook'а быть не должно.
    assert calls["n"] == 2
    assert len(events) == 1
    event = events[0]
    assert event.status == "succeeded"
    assert event.should_poll is True
    assert event.webhook_url == "http://client.example/hook"


def test_default_database_lives_in_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBS_DB", raising=False)
    monkeypatch.setattr(job_queue_module, "ROOT", tmp_path)

    queue = JobQueue(CFG, 1, min_free_mb=0)
    try:
        assert queue.db_path == tmp_path / "data" / "jobs.sqlite3"
        assert queue.db_path.exists()
    finally:
        queue.close()


def test_blocked_email_domain_is_rejected_before_queueing(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKED_EMAIL_DOMAINS", "outlook.com,hotmail.com")
    queue = _queue(tmp_path, lambda *args: {"status": 200, "shouldPoll": True})
    try:
        spec = JobSpec(
            cookies=[{"name": "ssid", "value": "v", "domain": ".riotgames.com", "path": "/"}],
            proxy="socks5://p1.example.com:1080",
            new_email="user@mail.outlook.com",
            external_id="blocked-1",
        )
        jobs, duplicates, errors = queue.submit("client", [spec])
        assert jobs == []
        assert duplicates == 0
        assert "domain is blocked" in errors[0]["error"]
    finally:
        queue.close()


def test_shared_proxy_test_mode_runs_concurrently(tmp_path):
    active = 0
    max_active = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def runner(cfg, account, proxy, headless):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                both_started.set()
        release.wait(5)
        with lock:
            active -= 1
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    queue = _queue(tmp_path, runner, max_attempts=1)
    queue.start()
    try:
        shared = "socks5://shared.example.com:1080"
        specs = [
            JobSpec(
                cookies=[{"name": "ssid", "value": "v1", "domain": ".riotgames.com", "path": "/"}],
                proxy=shared,
                new_email="u1@example.com",
                external_id="shared-1",
                allow_shared_proxy=True,
            ),
            JobSpec(
                cookies=[{"name": "ssid", "value": "v2", "domain": ".riotgames.com", "path": "/"}],
                proxy=shared,
                new_email="u2@example.com",
                external_id="shared-2",
                allow_shared_proxy=True,
            ),
        ]
        jobs, duplicates, errors = queue.submit("client", specs)
        assert len(jobs) == 2 and not duplicates and not errors
        started = both_started.wait(3)
        release.set()
        assert started, "jobs with a shared proxy did not start together"
        assert max_active == 2
        _wait_done(queue, "client", 2)
    finally:
        release.set()
        queue.close()


def test_proxy_key_separates_residential_sessions():
    first = proxy_key(parse_proxy("socks5://user:session-a@gw.example:1080"))
    second = proxy_key(parse_proxy("socks5://user:session-b@gw.example:1080"))
    same = proxy_key(parse_proxy("socks5://user:session-a@gw.example:1080"))

    assert first == same
    assert first != second


def test_proxy_reuse_across_accounts_is_rejected(tmp_path):
    queue = _queue(tmp_path, lambda *args: {"status": 200, "shouldPoll": True})
    shared = "socks5://shared.example.com:1080"
    try:
        jobs, duplicates, errors = queue.submit(
            "client",
            [
                JobSpec(
                    cookies=[
                        {"name": "ssid", "value": "v1", "domain": ".riotgames.com", "path": "/"}
                    ],
                    proxy=shared,
                    new_email="one@example.com",
                    external_id="account-1",
                ),
                JobSpec(
                    cookies=[
                        {"name": "ssid", "value": "v2", "domain": ".riotgames.com", "path": "/"}
                    ],
                    proxy=shared,
                    new_email="two@example.com",
                    external_id="account-2",
                ),
            ],
        )
        assert len(jobs) == 1
        assert duplicates == 0
        assert len(errors) == 1
        assert errors[0]["external_id"] == "account-2"
        assert "different proxy" in errors[0]["error"]
    finally:
        queue.close()


def test_needs_resubmit_can_change_email(tmp_path):
    seen: list[str] = []
    calls = {"n": 0}

    def runner(cfg, account, proxy, headless):
        calls["n"] += 1
        seen.append(account.new_email)
        if calls["n"] == 1:
            return {"status": 200, "shouldPoll": False, "body": {"message": "not accepted"}}
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    queue = _queue(tmp_path, runner, max_attempts=1)
    queue.start()
    try:
        spec = JobSpec(
            cookies=[{"name": "ssid", "value": "v", "domain": ".riotgames.com", "path": "/"}],
            proxy="socks5://p-retry.example.com:1080",
            new_email="first@outlook.com",
            external_id="resubmit-1",
        )
        jobs, _, errors = queue.submit("client", [spec])
        assert jobs and not errors
        _wait_done(queue, "client", 1)
        assert queue.get(jobs[0].id, "client").status == "needs_resubmit"

        replacement = JobSpec(
            cookies=spec.cookies,
            proxy=spec.proxy,
            new_email="second@gmx.net",
            external_id=spec.external_id,
        )
        jobs, duplicates, errors = queue.submit("client", [replacement])
        assert jobs and not duplicates and not errors
        _wait_done(queue, "client", 1)
        assert queue.get(jobs[0].id, "client").status == "succeeded"
        assert seen == ["first@outlook.com", "second@gmx.net"]
    finally:
        queue.close()


def test_batch_stats_aggregate_duration(tmp_path):
    def runner(cfg, account, proxy, headless):
        time.sleep(0.02)
        return {"status": 200, "shouldPoll": True, "body": {"message": "ok"}}

    queue = _queue(tmp_path, runner)
    queue.start()
    try:
        specs = []
        for i in (1, 2):
            spec = _spec(i)
            specs.append(
                JobSpec(
                    cookies=spec.cookies,
                    proxy=spec.proxy,
                    new_email=spec.new_email,
                    external_id=spec.external_id,
                    batch_id="batch-a",
                )
            )
        jobs, _duplicates, errors = queue.submit("client", specs)
        assert not errors and len(jobs) == 2
        _wait_done(queue, "client", 2)

        stats = queue.batch_stats("batch-a")
        assert stats["batch_total"] == 2
        assert stats["batch_terminal"] == 2
        assert stats["batch_succeeded"] == 2
        assert stats["batch_failed"] == 0
        assert stats["batch_remaining"] == 0
        assert stats["batch_average_duration_ms"] is not None
        assert stats["batch_average_success_duration_ms"] is not None
    finally:
        queue.stop()


def test_proxy_key_separates_ports():
    first = proxy_key(parse_proxy("http://user:pass@gw.example:10100"))
    second = proxy_key(parse_proxy("http://user:pass@gw.example:10101"))

    assert first != second


def test_distinct_ports_are_accepted_for_distinct_accounts(tmp_path):
    queue = _queue(tmp_path, lambda *args: {"status": 200, "shouldPoll": True})
    specs = [
        JobSpec(
            cookies=[{"name": "ssid", "value": f"v{i}", "domain": ".riotgames.com"}],
            proxy=f"http://user:pass@gw.example:{10100 + i}",
            new_email=f"user{i}@example.com",
            external_id=f"port-{i}",
        )
        for i in range(2)
    ]
    jobs, duplicates, errors = queue.submit("client", specs)

    assert not errors and duplicates == 0
    assert len(jobs) == 2
