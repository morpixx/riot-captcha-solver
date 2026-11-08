"""Очередь job'ов для массовых прогонов: SQLite-метаданные + in-memory cookies.

Секреты (cookies, прокси-креды, email) в SQLite не попадают. После рестарта
процесса незавершённые джобы помечаются needs_resubmit — клиент присылает их
повторно с тем же external_id.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import ROOT, Account, Config, blocked_email_domains, env_flag
from ..proxy.proxy import parse_proxy
from ..security import email_domain, email_tag, redact
from .errors import classify_error
from .models import ACTIVE, ALL_STATUSES, TERMINAL, Job, JobSpec
from .result import EMAIL_CHANGE_NOT_ACCEPTED, interpret_result
from .runner import run_account

log = logging.getLogger("riot.queue")

ALLOW_NO_PROXY = env_flag("ALLOW_NO_PROXY")


class QueueFull(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(f"rate limited, retry after {self.retry_after}s")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class PayloadMissing(Exception):
    """Джоба есть в SQLite, но cookies уже не в памяти (например, после рестарта)."""


@dataclass
class _Payload:
    cookies: list[dict]
    proxy: str
    new_email: str
    sitekey: str | None
    headless: bool | None


def _proxy_key(parsed: dict) -> str:
    # Include the password hash in the key: residential providers often encode
    # a distinct sticky-session id there while keeping host and username equal.
    # The raw credentials are never stored or logged.
    raw = "|".join(
        (
            str(parsed.get("server", "")),
            str(parsed.get("username", "")),
            str(parsed.get("password", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _assert_public_proxy(parsed: dict) -> None:
    host = urlparse(parsed.get("server", "")).hostname
    if not host:
        raise ValueError("proxy must be host:port")
    lowered = host.lower()
    if lowered in ("localhost", "localhost.localdomain", "ip6-localhost") or lowered.endswith(
        ".local"
    ):
        raise ValueError("proxy host must be a public address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname: DNS-проверку делаем на уровне инфраструктуры, не здесь
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError("proxy host must be a public address")


def _sanitize(text: object, proxy: str | None = None) -> str:
    return redact(text, proxy or "")[:1500]


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    idx = round((len(values) - 1) * q)
    return values[idx]


class JobQueue:
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
        self.db_path = Path(db_path or os.getenv("JOBS_DB", str(ROOT / "data" / "jobs.sqlite3")))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
        self._db_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._payloads: dict[str, _Payload] = {}
        # job_id -> webhook_url; живёт только в памяти вместе с cookies.
        self._webhook_urls: dict[str, str] = {}
        self._proxy_busy: set[str] = set()
        self._rate: dict[str, deque] = {}
        self._rate_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.workers)
        self.terminal_callback: Callable[[Job], None] | None = None
        self._finalizing = 0  # job'ы, которые уже завершились, но ещё шлют callback

        self._db = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        with self._db_lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._init_schema()
            self._recover()

        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="riot-job")
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="riot-dispatch", daemon=True
        )

    # ------------------------------------------------------------------ setup

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                external_id TEXT,
                batch_id TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                proxy_key TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                next_run_at REAL,
                duration_ms INTEGER,
                should_poll INTEGER,
                riot_status INTEGER,
                error_code TEXT,
                error TEXT,
                details TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_client_external
                ON jobs(client_id, external_id) WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at);
            CREATE TABLE IF NOT EXISTS proxy_health (
                key TEXT PRIMARY KEY,
                ok INTEGER NOT NULL,
                checked_at REAL NOT NULL,
                error TEXT
            );
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "batch_id" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id)")

    def _recover(self) -> None:
        now = time.time()
        cur = self._db.execute(
            "UPDATE jobs SET status='needs_resubmit', updated_at=? "
            "WHERE status IN ('queued','running','retry_wait')",
            (now,),
        )
        if cur.rowcount:
            log.warning(
                "recovered %d unfinished jobs -> needs_resubmit (cookies were in memory)",
                cur.rowcount,
            )

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
        with self._db_lock:
            self._db.close()

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

    def _payload_and_key(self, spec: JobSpec) -> tuple[_Payload, str]:
        """Validate one spec and derive the proxy deduplication key."""
        parsed = parse_proxy(spec.proxy)
        if parsed is None:
            if not ALLOW_NO_PROXY:
                raise ValueError("proxy must not be empty")
            key = ""
        else:
            _assert_public_proxy(parsed)
            key = _proxy_key(parsed)
        if not spec.new_email or "@" not in spec.new_email:
            raise ValueError("new_email is invalid")
        domain = spec.new_email.rpartition("@")[2].strip().lower()
        blocked = blocked_email_domains()
        if any(domain == item or domain.endswith("." + item) for item in blocked):
            raise ValueError(f"new_email domain is blocked: {domain}")
        if not spec.cookies:
            raise ValueError("cookies must not be empty")
        payload = _Payload(
            cookies=[dict(cookie) for cookie in spec.cookies],
            proxy=spec.proxy,
            new_email=spec.new_email,
            sitekey=spec.sitekey,
            headless=spec.headless,
        )
        if spec.allow_shared_proxy:
            key = ""
        return payload, key

    def _find_existing(self, client_id: str, external_id: str | None) -> Job | None:
        if not external_id:
            return None
        with self._db_lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE client_id=? AND external_id=?",
                (client_id, external_id),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def _proxy_conflict(
        self,
        client_id: str,
        proxy_key_value: str,
        external_id: str | None,
    ) -> str | None:
        """Return the other account that already owns this proxy.

        A proxy is bound to one account for the client's lifetime. Reusing it
        for a different external_id is rejected unless test_mode explicitly
        allows shared proxies.
        """
        if not proxy_key_value:
            return None
        with self._db_lock:
            rows = self._db.execute(
                "SELECT external_id FROM jobs WHERE client_id=? AND proxy_key=?",
                (client_id, proxy_key_value),
            ).fetchall()
        for row in rows:
            owner = row["external_id"]
            if external_id and owner == external_id:
                continue
            return str(owner) if owner else "another account"
        return None

    @staticmethod
    def _is_duplicate(existing: Job | None) -> bool:
        if existing is None:
            return False
        # Old versions recorded shouldPoll=false as succeeded. Treat that row as
        # a reason to run again, not as a completed duplicate.
        legacy_rejection = existing.status == "succeeded" and existing.should_poll is False
        return existing.status in (*ACTIVE, "succeeded") and not legacy_rejection

    def _remember_payload(self, job_id: str, payload: _Payload, webhook_url: str | None) -> None:
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
        payload: _Payload,
        proxy_key_value: str,
        priority: int,
        now: float,
    ) -> Job:
        job_id = uuid.uuid4().hex
        # Publish the payload before INSERT: the dispatcher may claim the row
        # immediately after the statement commits.
        self._remember_payload(job_id, payload, spec.webhook_url)
        try:
            with self._db_lock:
                self._db.execute(
                    "INSERT INTO jobs (id, client_id, external_id, batch_id, status, attempts,"
                    " priority, proxy_key, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
                    (
                        job_id,
                        client_id,
                        spec.external_id,
                        spec.batch_id,
                        priority,
                        proxy_key_value,
                        now,
                        now,
                    ),
                )
        except BaseException:
            with self._state_lock:
                self._payloads.pop(job_id, None)
                self._webhook_urls.pop(job_id, None)
            raise
        job = self.get(job_id, client_id)
        job.webhook_url = spec.webhook_url
        return job

    def _reset_job(
        self,
        existing: Job,
        spec: JobSpec,
        payload: _Payload,
        proxy_key_value: str,
        priority: int,
        now: float,
    ) -> Job:
        self._remember_payload(existing.id, payload, spec.webhook_url)
        with self._db_lock:
            self._db.execute(
                "UPDATE jobs SET status='queued', attempts=0, priority=?, proxy_key=?,"
                " batch_id=?, updated_at=?,"
                " started_at=NULL, finished_at=NULL, next_run_at=NULL, duration_ms=NULL,"
                " should_poll=NULL, riot_status=NULL, error_code=NULL, error=NULL, details=NULL"
                " WHERE id=?",
                (priority, proxy_key_value, spec.batch_id, now, existing.id),
            )
        job = self.get(existing.id, existing.client_id)
        job.webhook_url = spec.webhook_url
        return job

    def submit(
        self,
        client_id: str,
        specs: list[JobSpec],
        *,
        priority: int = 0,
    ) -> tuple[list[Job], int, list[dict]]:
        if self.queue_depth() + len(specs) > self.max_queue:
            raise QueueFull(f"queue is full (max {self.max_queue})")

        now = time.time()
        accepted: list[Job] = []
        duplicates = 0
        errors: list[dict] = []

        for index, spec in enumerate(specs):
            try:
                payload, proxy_key_value = self._payload_and_key(spec)
            except ValueError as exc:
                errors.append(
                    {
                        "index": index,
                        "external_id": spec.external_id,
                        "error": _sanitize(str(exc), spec.proxy),
                    }
                )
                continue

            if not spec.allow_shared_proxy:
                owner = self._proxy_conflict(client_id, proxy_key_value, spec.external_id)
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

            existing = self._find_existing(client_id, spec.external_id)
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

    def get(self, job_id: str, client_id: str) -> Job:
        with self._db_lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE id=? AND client_id=?", (job_id, client_id)
            ).fetchone()
        if row is None:
            raise NotFound(f"job {job_id!r} not found")
        job = self._row_to_job(row)
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
        where = ["client_id = ?"]
        params: list[Any] = [client_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if updated_since is not None:
            where.append("updated_at > ?")
            params.append(updated_since)
        clause = " AND ".join(where)
        with self._db_lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {clause}", params
            ).fetchone()[0]
            rows = self._db.execute(
                f"SELECT * FROM jobs WHERE {clause} ORDER BY updated_at ASC, id ASC LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
        return [self._row_to_job(r) for r in rows], int(total)

    def cancel(self, job_id: str, client_id: str) -> Job:
        job = self.get(job_id, client_id)
        if job.status == "running":
            raise Conflict("running job cannot be canceled safely; wait for it to finish")
        if job.status in TERMINAL:
            raise Conflict(f"job is already {job.status}")
        now = time.time()
        with self._db_lock:
            cur = self._db.execute(
                "UPDATE jobs SET status='canceled', updated_at=?, finished_at=?"
                " WHERE id=? AND status IN ('queued','retry_wait')",
                (now, now, job_id),
            )
        if cur.rowcount != 1:
            current = self.get(job_id, client_id)
            raise Conflict(f"job is already {current.status}")
        with self._state_lock:
            self._payloads.pop(job_id, None)
        updated = self.get(job_id, client_id)
        self._notify_terminal(job_id, client_id)
        return updated

    def stats(self, *, client_id: str | None = None) -> dict:
        scope = " WHERE client_id=?" if client_id else ""
        params: tuple = (client_id,) if client_id else ()
        with self._db_lock:
            counts = {status: 0 for status in ALL_STATUSES}
            for row in self._db.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs{scope} GROUP BY status", params
            ):
                counts[row["status"]] = int(row["n"])
            durations = [
                int(r[0])
                for r in self._db.execute(
                    "SELECT duration_ms FROM jobs WHERE status='succeeded'"
                    " AND duration_ms IS NOT NULL"
                    + (" AND client_id=?" if client_id else "")
                    + " ORDER BY duration_ms DESC LIMIT 5000",
                    params,
                )
            ]
            errors = {
                row["error_code"] or "unknown": int(row["n"])
                for row in self._db.execute(
                    "SELECT error_code, COUNT(*) AS n FROM jobs WHERE status='dead'"
                    + (" AND client_id=?" if client_id else "")
                    + " GROUP BY error_code",
                    params,
                )
            }
            dead_proxies = self._db.execute(
                "SELECT COUNT(*) FROM proxy_health WHERE ok=0"
            ).fetchone()[0]

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
        with self._db_lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','retry_wait')"
            ).fetchone()
        return int(row[0])

    def average_duration_ms(self, *, client_id: str | None = None) -> float | None:
        """Average duration of successfully completed jobs for a client."""
        params: tuple = (client_id,) if client_id else ()
        where = "status='succeeded' AND duration_ms IS NOT NULL"
        if client_id:
            where += " AND client_id=?"
        with self._db_lock:
            row = self._db.execute(
                f"SELECT AVG(duration_ms) FROM jobs WHERE {where}",
                params,
            ).fetchone()
        return round(float(row[0]), 1) if row and row[0] is not None else None

    def batch_stats(self, batch_id: str | None) -> dict[str, int | float | None]:
        """Aggregate processing time and progress for one submitted batch."""
        if not batch_id:
            return {
                "batch_total": None,
                "batch_terminal": None,
                "batch_succeeded": None,
                "batch_failed": None,
                "batch_remaining": None,
                "batch_average_duration_ms": None,
                "batch_average_success_duration_ms": None,
            }
        terminal = "('succeeded','dead','canceled','needs_resubmit')"
        with self._db_lock:
            row = self._db.execute(
                "SELECT COUNT(*), "
                f"SUM(CASE WHEN status IN {terminal} THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), "
                f"AVG(CASE WHEN status IN {terminal} AND duration_ms IS NOT NULL "
                "THEN duration_ms END), "
                "AVG(CASE WHEN status='succeeded' AND duration_ms IS NOT NULL "
                "THEN duration_ms END) "
                "FROM jobs WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        total = int(row[0] or 0)
        done = int(row[1] or 0)
        succeeded = int(row[2] or 0)
        return {
            "batch_total": total,
            "batch_terminal": done,
            "batch_succeeded": succeeded,
            "batch_failed": done - succeeded,
            "batch_remaining": max(0, total - done),
            "batch_average_duration_ms": (round(float(row[3]), 1) if row[3] is not None else None),
            "batch_average_success_duration_ms": (
                round(float(row[4]), 1) if row[4] is not None else None
            ),
        }

    def running_count(self) -> int:
        with self._db_lock:
            row = self._db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()
        return int(row[0])

    def _finalizing_count(self) -> int:
        with self._state_lock:
            return self._finalizing

    # ------------------------------------------------------------------ internals

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        details = None
        if row["details"]:
            try:
                details = json.loads(row["details"])
            except Exception:
                details = {"raw": str(row["details"])}
        return Job(
            id=row["id"],
            client_id=row["client_id"],
            external_id=row["external_id"],
            batch_id=row["batch_id"],
            status=row["status"],
            attempts=int(row["attempts"]),
            priority=int(row["priority"]),
            proxy_key=row["proxy_key"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            next_run_at=row["next_run_at"],
            duration_ms=row["duration_ms"],
            should_poll=None if row["should_poll"] is None else bool(row["should_poll"]),
            riot_status=row["riot_status"],
            error_code=row["error_code"],
            error=row["error"],
            details=details,
        )

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
                self._requeue(job.id)
                self.slots.release()
                continue
            future.add_done_callback(lambda fut, j=job: self._on_done(j, fut))
            if self.ramp_interval:
                self._stop.wait(self.ramp_interval)

    def _claim_next(self) -> Job | None:
        now = time.time()
        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " OR (status='retry_wait' AND next_run_at <= ?)"
                " ORDER BY priority DESC, created_at ASC LIMIT 50",
                (now,),
            ).fetchall()
            running_by_client = {
                r["client_id"]: int(r["n"])
                for r in self._db.execute(
                    "SELECT client_id, COUNT(*) AS n FROM jobs WHERE status='running' GROUP BY client_id"
                )
            }

            for row in rows:
                if running_by_client.get(row["client_id"], 0) >= self.max_per_client:
                    continue
                key = row["proxy_key"]
                with self._state_lock:
                    if key and key in self._proxy_busy:
                        continue
                cur = self._db.execute(
                    "UPDATE jobs SET status='running', started_at=?, updated_at=?, attempts=attempts+1"
                    " WHERE id=? AND status IN ('queued','retry_wait')",
                    (now, now, row["id"]),
                )
                if cur.rowcount != 1:
                    continue
                claimed = self._db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if key:
                    with self._state_lock:
                        self._proxy_busy.add(key)
                return self._row_to_job(claimed)
        return None

    def _requeue(self, job_id: str) -> None:
        now = time.time()
        with self._db_lock:
            self._db.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='running'",
                (now, job_id),
            )

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
            from dataclasses import replace as _replace

            flow_cfg = _replace(self.cfg, sitekey=payload.sitekey)
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

    def _finish_success(self, job: Job, result: Any) -> None:
        now = time.time()
        if not isinstance(result, dict):
            self._finish_error(job, RuntimeError(f"unexpected run() result: {result!r}"))
            return

        outcome = interpret_result(result)
        details = outcome.details
        should_poll = outcome.should_poll
        error_code = outcome.error_code
        error = outcome.error
        # Riot's 200 + shouldPoll=false means the change was not accepted; the
        # client must confirm the pending address and resubmit explicitly.
        status = "needs_resubmit" if error_code == EMAIL_CHANGE_NOT_ACCEPTED else "succeeded"

        with self._state_lock:
            payload = self._payloads.get(job.id)
        proxy_secret = payload.proxy if payload else ""

        details_json = None
        if details is not None:
            try:
                raw_details = json.dumps(details, ensure_ascii=False, default=str)
            except Exception:
                raw_details = json.dumps({"body": str(details)})
            raw_details = redact(raw_details, proxy_secret)
            if len(raw_details) > 20000:
                raw_details = json.dumps({"truncated": True, "text": raw_details[:19000]})
            details_json = raw_details

        duration_ms = int((now - (job.started_at or now)) * 1000)
        with self._db_lock:
            self._db.execute(
                "UPDATE jobs SET status=?, updated_at=?, finished_at=?, duration_ms=?,"
                " should_poll=?, riot_status=?, details=?, error_code=?, error=?, next_run_at=NULL"
                " WHERE id=?",
                (
                    status,
                    now,
                    now,
                    duration_ms,
                    None if should_poll is None else int(bool(should_poll)),
                    result.get("status"),
                    details_json,
                    error_code,
                    error,
                    job.id,
                ),
            )
        self._mark_proxy(job.proxy_key, True, None)
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

    def _finish_error(self, job: Job, exc: BaseException) -> None:
        now = time.time()
        if isinstance(exc, PayloadMissing):
            error = _sanitize(str(exc))
            duration_ms = int((now - (job.started_at or now)) * 1000)
            with self._db_lock:
                self._db.execute(
                    "UPDATE jobs SET status='needs_resubmit', updated_at=?, finished_at=?,"
                    " duration_ms=?, error_code='payload_missing', error=?, next_run_at=NULL"
                    " WHERE id=?",
                    (now, now, duration_ms, error, job.id),
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
        error = _sanitize(f"{type(exc).__name__}: {exc}", payload.proxy if payload else None)

        if code == "proxy_error":
            self._mark_proxy(job.proxy_key, False, error)

        if retryable and job.attempts < self.max_attempts:
            delay = self.retry_base * (2 ** (job.attempts - 1)) * (1 + random.random() * 0.2)
            next_run = now + max(0.0, delay)
            with self._db_lock:
                self._db.execute(
                    "UPDATE jobs SET status='retry_wait', updated_at=?, next_run_at=?, error_code=?, error=?"
                    " WHERE id=?",
                    (now, next_run, code, error, job.id),
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
        with self._db_lock:
            self._db.execute(
                "UPDATE jobs SET status='dead', updated_at=?, finished_at=?, duration_ms=?,"
                " error_code=?, error=?, next_run_at=NULL WHERE id=?",
                (now, now, duration_ms, code, error, job.id),
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

    def _mark_proxy(self, key: str, ok: bool, error: str | None) -> None:
        if not key:
            return
        now = time.time()
        with self._db_lock:
            self._db.execute(
                "INSERT INTO proxy_health (key, ok, checked_at, error) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET ok=excluded.ok, checked_at=excluded.checked_at, error=excluded.error",
                (key, 1 if ok else 0, now, error),
            )
