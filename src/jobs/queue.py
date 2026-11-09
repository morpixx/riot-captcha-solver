"""Job queue: submission, scheduling, retries and terminal callbacks.

SQLite persistence lives in src.jobs.store; request validation and the
in-memory payload live in src.jobs.payload. This module owns policy: who runs
next, when a failure is retried, and which terminal events are sent.

Secrets (cookies, proxy credentials, email) never reach SQLite. After a
restart unfinished jobs become needs_resubmit; the client resubmits them with
the same external_id.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from ..config import ROOT, Account, Config
from ..proxy.proxy import parse_proxy
from ..security import email_domain, email_tag, redact
from .errors import (  # noqa: F401 - re-exported for API/backward compatibility
    Conflict,
    NotFound,
    PayloadMissing,
    QueueFull,
    RateLimited,
    classify_error,
)
from .models import ACTIVE, ALL_STATUSES, TERMINAL, Job, JobSpec
from .payload import Payload, build_payload, sanitize
from .result import EMAIL_CHANGE_NOT_ACCEPTED, interpret_result
from .runner import run_account
from .store import JobStore

log = logging.getLogger("riot.queue")


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * q)]


class JobQueue:
    """Workers, retries and queue admission on top of JobStore."""

    def __init__(
        self,
        cfg: Config,
        workers: int,
        *,
        db_path: str | Path | None = None,
        max_queue: int | None = None,
        batch_max: int | None = None,
        max_attempts: int | None = None,
        retry_base: float | None = None,
        ramp_interval: float | None = None,
        min_free_mb: int | None = None,
        max_per_client: int | None = None,
        rate_limit: int | None = None,
        runner: Callable[[Config, Account, dict | None, bool | None], dict] | None = None,
    ):
        self.cfg = cfg
        self.workers = max(1, int(workers))
        self.store = JobStore(
            Path(db_path or os.getenv("JOBS_DB", str(ROOT / "data" / "jobs.sqlite3")))
        )
        self.max_queue = int(max_queue if max_queue is not None else os.getenv("MAX_QUEUE", "5000"))
        self.batch_max = int(batch_max if batch_max is not None else os.getenv("BATCH_MAX", "500"))
        self.max_attempts = max(
            1, int(max_attempts if max_attempts is not None else os.getenv("JOB_ATTEMPTS", "3"))
        )
        self.retry_base = float(
            retry_base if retry_base is not None else os.getenv("RETRY_BASE", "30")
        )
        self.ramp_interval = float(
            ramp_interval if ramp_interval is not None else os.getenv("RAMP_INTERVAL", "1.0")
        )
        self.min_free_mb = int(
            min_free_mb if min_free_mb is not None else os.getenv("MIN_FREE_MB", "2048")
        )
        self.max_per_client = (
            int(max_per_client if max_per_client is not None else os.getenv("MAX_PER_CLIENT", "0"))
            or self.workers
        )
        self.rate_limit = int(
            rate_limit if rate_limit is not None else os.getenv("RATE_LIMIT_PER_MIN", "1200")
        )
        self.runner = runner or run_account

        self.paused_reason: str | None = None
        self.started_at = time.time()
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._payloads: dict[str, Payload] = {}
        # job_id -> webhook_url; lives only in memory with the payload.
        self._webhook_urls: dict[str, str] = {}
        self._proxy_busy: set[str] = set()
        self._rate: dict[str, deque] = {}
        self._rate_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.workers)
        self.terminal_callback: Callable[[Job], None] | None = None
        self._finalizing = 0  # terminal jobs that are still sending their callback

        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="riot-job")
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="riot-dispatch", daemon=True
        )

    # ------------------------------------------------------------------ setup

    @property
    def db_path(self) -> Path:
        return self.store.path

    def start(self) -> None:
        if not self._dispatcher.is_alive():
            self._dispatcher.start()
            log.info(
                "queue started: workers=%d db=%s max_queue=%d",
                self.workers,
                self.db_path,
                self.max_queue,
            )

    def stop(self, *, wait: bool = False, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=5)
        self.executor.shutdown(wait=False, cancel_futures=True)
        if wait:
            deadline = time.time() + timeout
            while time.time() < deadline and (self.running_count() or self._finalizing_count()):
                time.sleep(0.1)

    def close(self, *, timeout: float = 5.0) -> None:
        self.stop(wait=True, timeout=timeout)
        self.store.close()

    # ------------------------------------------------------------------ public

    def check_rate(self, client_id: str) -> None:
        if self.rate_limit <= 0:
            return
        now = time.time()
        with self._rate_lock:
            q = self._rate.setdefault(client_id, deque())
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= self.rate_limit:
                raise RateLimited(math.ceil(60 - (now - q[0])))
            q.append(now)

    def submit(
        self,
        client_id: str,
        specs: list[JobSpec],
        *,
        priority: int = 0,
    ) -> tuple[list[Job], int, list[dict]]:
        """Validate and enqueue specs; return (accepted, duplicates, errors)."""
        if self.queue_depth() + len(specs) > self.max_queue:
            raise QueueFull(f"queue is full (max {self.max_queue})")

        now = time.time()
        accepted: list[Job] = []
        duplicates = 0
        errors: list[dict] = []

        for index, spec in enumerate(specs):
            try:
                payload, proxy_key_value = build_payload(spec)
            except ValueError as exc:
                errors.append(
                    {
                        "index": index,
                        "external_id": spec.external_id,
                        "error": sanitize(str(exc), spec.proxy),
                    }
                )
                continue

            if not spec.allow_shared_proxy:
                owner = self.store.proxy_owner(client_id, proxy_key_value, spec.external_id)
                if owner is not None:
                    errors.append(
                        {
                            "index": index,
                            "external_id": spec.external_id,
                            "error": (
                                f"proxy is already assigned to {owner}; "
                                "use a different proxy for each account"
                            ),
                        }
                    )
                    continue

            existing = (
                self.store.find(client_id, spec.external_id) if spec.external_id else None
            )
            if self._is_duplicate(existing):
                duplicates += 1
                # A new URL is accepted while the job is still active, but a
                # completed job is never notified twice.
                if existing is not None and spec.webhook_url and existing.status in ACTIVE:
                    with self._state_lock:
                        self._webhook_urls[existing.id] = spec.webhook_url
                    existing.webhook_url = spec.webhook_url
                if existing is not None:
                    accepted.append(existing)
                continue

            if existing is None:
                job = self._create_job(client_id, spec, payload, proxy_key_value, priority, now)
            else:
                job = self._reset_job(existing, spec, payload, proxy_key_value, priority, now)
            accepted.append(job)

        return accepted, duplicates, errors

    @staticmethod
    def _is_duplicate(existing: Job | None) -> bool:
        if existing is None:
            return False
        # Old versions recorded shouldPoll=false as succeeded. Treat that row as
        # a reason to run again, not as a completed duplicate.
        legacy_rejection = existing.status == "succeeded" and existing.should_poll is False
        return existing.status in (*ACTIVE, "succeeded") and not legacy_rejection

    def _remember_payload(self, job_id: str, payload: Payload, webhook_url: str | None) -> None:
        with self._state_lock:
            self._payloads[job_id] = payload
            if webhook_url:
                self._webhook_urls[job_id] = webhook_url
            else:
                self._webhook_urls.pop(job_id, None)

    def _create_job(
        self,
        client_id: str,
        spec: JobSpec,
        payload: Payload,
        proxy_key_value: str,
        priority: int,
        now: float,
    ) -> Job:
        job_id = uuid.uuid4().hex
        # Publish the payload before INSERT: the dispatcher may claim the row
        # immediately after the statement commits.
        self._remember_payload(job_id, payload, spec.webhook_url)
        try:
            job = self.store.insert(
                job_id=job_id,
                client_id=client_id,
                spec=spec,
                proxy_key=proxy_key_value,
                priority=priority,
                now=now,
            )
        except BaseException:
            with self._state_lock:
                self._payloads.pop(job_id, None)
                self._webhook_urls.pop(job_id, None)
            raise
        job.webhook_url = spec.webhook_url
        return job

    def _reset_job(
        self,
        existing: Job,
        spec: JobSpec,
        payload: Payload,
        proxy_key_value: str,
        priority: int,
        now: float,
    ) -> Job:
        self._remember_payload(existing.id, payload, spec.webhook_url)
        self.store.reset(
            job_id=existing.id,
            spec=spec,
            proxy_key=proxy_key_value,
            priority=priority,
            now=now,
        )
        job = self.get(existing.id, existing.client_id)
        job.webhook_url = spec.webhook_url
        return job

    def get(self, job_id: str, client_id: str) -> Job:
        job = self.store.get(job_id, client_id)
        with self._state_lock:
            job.webhook_url = self._webhook_urls.get(job_id)
        return job

    def list_jobs(
        self,
        client_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        updated_since: float | None = None,
    ) -> tuple[list[Job], int]:
        return self.store.list(
            client_id,
            statuses=statuses,
            limit=limit,
            offset=offset,
            updated_since=updated_since,
        )

    def cancel(self, job_id: str, client_id: str) -> Job:
        job = self.get(job_id, client_id)
        if job.status == "running":
            raise Conflict("running job cannot be canceled safely; wait for it to finish")
        if job.status in TERMINAL:
            raise Conflict(f"job is already {job.status}")
        if not self.store.cancel_queued(job_id, time.time()):
            current = self.get(job_id, client_id)
            raise Conflict(f"job is already {current.status}")
        with self._state_lock:
            self._payloads.pop(job_id, None)
        updated = self.get(job_id, client_id)
        self._notify_terminal(job_id, client_id)
        return updated

    def stats(self, *, client_id: str | None = None) -> dict:
        counts = {status: 0 for status in ALL_STATUSES}
        counts.update(self.store.status_counts(client_id))
        durations = self.store.successful_durations(client_id)
        errors = self.store.dead_error_counts(client_id)
        dead_proxies = self.store.dead_proxy_count()

        free_mb = None
        try:
            import psutil

            free_mb = round(psutil.virtual_memory().available / (1024 * 1024))
        except Exception as exc:
            log.debug("memory statistics unavailable: %s", exc)

        with self._state_lock:
            cookies_in_memory = len(self._payloads)

        return {
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "workers": self.workers,
            "busy": counts.get("running", 0),
            "paused": self.paused_reason,
            "free_mem_mb": free_mb,
            "jobs": counts,
            "queue_depth": counts.get("queued", 0) + counts.get("retry_wait", 0),
            "durations_ms": {
                "count": len(durations),
                "avg": round(sum(durations) / len(durations), 1) if durations else None,
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "max": max(durations) if durations else None,
            },
            "errors": errors,
            "dead_proxies": int(dead_proxies),
            "cookies_in_memory": cookies_in_memory,
        }

    def queue_depth(self) -> int:
        return self.store.queue_depth()

    def average_duration_ms(self, *, client_id: str | None = None) -> float | None:
        return self.store.average_duration_ms(client_id)

    def batch_stats(self, batch_id: str | None) -> dict[str, int | float | None]:
        return self.store.batch_stats(batch_id)

    def running_count(self) -> int:
        return self.store.running_count()

    def _finalizing_count(self) -> int:
        with self._state_lock:
            return self._finalizing

    # ------------------------------------------------------------------ internals

    def _pause_reason(self) -> str | None:
        if self.min_free_mb <= 0:
            return None
        try:
            import psutil

            available_mb = psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return None
        if available_mb < self.min_free_mb:
            return f"low_memory ({int(available_mb)} MB free < {self.min_free_mb} MB)"
        return None

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            self.paused_reason = self._pause_reason()
            if self.paused_reason:
                self._stop.wait(1.0)
                continue
            if not self.slots.acquire(timeout=0.5):
                continue
            if self._stop.is_set():
                self.slots.release()
                break
            job = self._claim_next()
            if job is None:
                self.slots.release()
                self._stop.wait(0.2)
                continue
            try:
                future = self.executor.submit(self._run_job, job)
            except Exception as exc:
                log.exception("could not submit job %s: %s", job.id, exc)
                self.store.requeue(job.id)
                self.slots.release()
                continue
            future.add_done_callback(lambda fut, j=job: self._on_done(j, fut))
            if self.ramp_interval:
                self._stop.wait(self.ramp_interval)

    def _claim_next(self) -> Job | None:
        job = self.store.claim_next(
            time.time(),
            max_per_client=self.max_per_client,
            is_proxy_busy=self._proxy_is_busy,
        )
        if job is not None and job.proxy_key:
            with self._state_lock:
                self._proxy_busy.add(job.proxy_key)
        return job

    def _proxy_is_busy(self, key: str) -> bool:
        with self._state_lock:
            return key in self._proxy_busy

    def _run_job(self, job: Job) -> dict:
        with self._state_lock:
            payload = self._payloads.get(job.id)
        if payload is None:
            raise PayloadMissing(
                "cookies for this job are not in memory; resubmit with the same external_id"
            )

        account = Account(
            name=job.external_id or f"job-{job.id[:8]}",
            cookies=[dict(cookie) for cookie in payload.cookies],
            new_email=payload.new_email,
            proxy=payload.proxy,
        )
        proxy = parse_proxy(payload.proxy)
        flow_cfg = self.cfg
        if payload.sitekey and payload.sitekey != self.cfg.sitekey:
            flow_cfg = replace(self.cfg, sitekey=payload.sitekey)
        log.info(
            "job=%s account=%s email_tag=%s domain=%s batch=%s start",
            job.id[:8],
            job.external_id or "-",
            email_tag(payload.new_email),
            email_domain(payload.new_email),
            job.batch_id or "-",
        )
        return self.runner(flow_cfg, account, proxy, payload.headless)

    def _on_done(self, job: Job, future) -> None:
        with self._state_lock:
            self._finalizing += 1
        try:
            try:
                result = future.result()
            except Exception as exc:
                self._finish_error(job, exc)
            else:
                self._finish_success(job, result)
        except Exception:
            log.exception("failed to finalize job %s", job.id)
        finally:
            with self._state_lock:
                self._proxy_busy.discard(job.proxy_key)
                self._finalizing -= 1
            self.slots.release()

    def _notify_terminal(self, job_id: str, client_id: str) -> None:
        with self._state_lock:
            webhook_url = self._webhook_urls.pop(job_id, None)
        callback = self.terminal_callback
        if callback is None:
            return
        try:
            job = self.get(job_id, client_id)
            job.webhook_url = webhook_url
            callback(job)
        except Exception:
            log.exception("terminal callback failed for job %s", job_id)

    def _finish_success(self, job: Job, result: object) -> None:
        now = time.time()
        if not isinstance(result, dict):
            self._finish_error(job, RuntimeError(f"unexpected run() result: {result!r}"))
            return

        outcome = interpret_result(result)
        should_poll = outcome.should_poll
        error_code = outcome.error_code
        # Riot's 200 + shouldPoll=false means the change was not accepted; the
        # client must confirm the pending address and resubmit explicitly.
        status = "needs_resubmit" if error_code == EMAIL_CHANGE_NOT_ACCEPTED else "succeeded"

        with self._state_lock:
            payload = self._payloads.get(job.id)
        proxy_secret = payload.proxy if payload else ""
        details_json = self._details_json(outcome.details, proxy_secret)
        duration_ms = int((now - (job.started_at or now)) * 1000)
        self.store.finish(
            job.id,
            status=status,
            now=now,
            duration_ms=duration_ms,
            should_poll=should_poll,
            riot_status=result.get("status"),
            details_json=details_json,
            error_code=error_code,
            error=outcome.error,
        )
        self.store.mark_proxy(job.proxy_key, True, None)
        with self._state_lock:
            self._payloads.pop(job.id, None)
        log_fn = log.warning if status == "needs_resubmit" else log.info
        log_fn(
            "job=%s account=%s status=%s shouldPoll=%s duration=%.1fs%s",
            job.id[:8],
            job.external_id or "-",
            status,
            should_poll,
            duration_ms / 1000,
            f" error_code={error_code}" if error_code else "",
        )
        self._notify_terminal(job.id, job.client_id)

    @staticmethod
    def _details_json(details: dict | None, proxy_secret: str) -> str | None:
        if details is None:
            return None
        try:
            raw = json.dumps(details, ensure_ascii=False, default=str)
        except Exception:
            raw = json.dumps({"body": str(details)})
        raw = redact(raw, proxy_secret)
        if len(raw) > 20000:
            raw = json.dumps({"truncated": True, "text": raw[:19000]})
        return raw

    def _finish_error(self, job: Job, exc: BaseException) -> None:
        now = time.time()
        if isinstance(exc, PayloadMissing):
            duration_ms = int((now - (job.started_at or now)) * 1000)
            self.store.finish(
                job.id,
                status="needs_resubmit",
                now=now,
                duration_ms=duration_ms,
                error_code="payload_missing",
                error=sanitize(str(exc)),
            )
            with self._state_lock:
                self._payloads.pop(job.id, None)
            log.error(
                "job=%s account=%s batch=%s no cookies in memory -> needs_resubmit",
                job.id[:8],
                job.external_id or "-",
                job.batch_id or "-",
            )
            self._notify_terminal(job.id, job.client_id)
            return

        code, retryable = classify_error(exc)
        with self._state_lock:
            payload = self._payloads.get(job.id)
        error = sanitize(f"{type(exc).__name__}: {exc}", payload.proxy if payload else None)

        if code == "proxy_error":
            self.store.mark_proxy(job.proxy_key, False, error)

        if retryable and job.attempts < self.max_attempts:
            delay = self.retry_base * (2 ** (job.attempts - 1)) * (1 + random.random() * 0.2)
            self.store.mark_retry(
                job.id,
                now=now,
                next_run_at=now + max(0.0, delay),
                error_code=code,
                error=error,
            )
            log.warning(
                "job=%s account=%s attempt=%d/%d code=%s retry_in=%.0fs batch=%s",
                job.id[:8],
                job.external_id or "-",
                job.attempts,
                self.max_attempts,
                code,
                delay,
                job.batch_id or "-",
            )
            return

        duration_ms = int((now - (job.started_at or now)) * 1000)
        self.store.finish(
            job.id,
            status="dead",
            now=now,
            duration_ms=duration_ms,
            error_code=code,
            error=error,
        )
        with self._state_lock:
            self._payloads.pop(job.id, None)
        log.error(
            "job=%s account=%s status=dead attempts=%d code=%s batch=%s error=%s",
            job.id[:8],
            job.external_id or "-",
            job.attempts,
            code,
            job.batch_id or "-",
            error,
        )
        self._notify_terminal(job.id, job.client_id)
