"""SQLite persistence for job metadata and proxy health.

Only metadata is written here. Cookies and proxy credentials live in the
queue's in-memory payload map, so a restart marks unfinished jobs as
"needs_resubmit" instead of resuming them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .errors import NotFound
from .models import Job, JobSpec

_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
CREATE TABLE IF NOT EXISTS proxy_health (
    key TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    checked_at REAL NOT NULL,
    error TEXT
);
"""

_TERMINAL_SQL = "('succeeded','dead','canceled','needs_resubmit')"

log = logging.getLogger("riot.queue")


class JobStore:
    """One SQLite connection whose lock serializes writers.

    The store owns SQL and row mapping; scheduling and retry policy belong to
    the job queue.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.executescript(_SCHEMA)
            self._migrate()
            self._recover()

    # ------------------------------------------------------------------ setup

    def _migrate(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        if "batch_id" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
        self.connection.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id)")

    def _recover(self) -> None:
        """Mark unfinished rows from a previous process as needs_resubmit."""
        cur = self.connection.execute(
            "UPDATE jobs SET status='needs_resubmit', updated_at=? "
            "WHERE status IN ('queued','running','retry_wait')",
            (time.time(),),
        )
        if cur.rowcount:
            log.warning(
                "recovered %d unfinished jobs -> needs_resubmit (cookies were in memory)",
                cur.rowcount,
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    # ------------------------------------------------------------------ writes

    def insert(
        self,
        *,
        job_id: str,
        client_id: str,
        spec: JobSpec,
        proxy_key: str,
        priority: int,
        now: float,
    ) -> Job:
        with self.lock:
            self.connection.execute(
                "INSERT INTO jobs (id, client_id, external_id, batch_id, status, attempts,"
                " priority, proxy_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
                (
                    job_id,
                    client_id,
                    spec.external_id,
                    spec.batch_id,
                    priority,
                    proxy_key,
                    now,
                    now,
                ),
            )
        return self.get(job_id, client_id)

    def reset(
        self,
        *,
        job_id: str,
        spec: JobSpec,
        proxy_key: str,
        priority: int,
        now: float,
    ) -> None:
        """Requeue an existing row so a client can resubmit the same external_id."""
        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET status='queued', attempts=0, priority=?, proxy_key=?,"
                " batch_id=?, updated_at=?,"
                " started_at=NULL, finished_at=NULL, next_run_at=NULL, duration_ms=NULL,"
                " should_poll=NULL, riot_status=NULL, error_code=NULL, error=NULL, details=NULL"
                " WHERE id=?",
                (priority, proxy_key, spec.batch_id, now, job_id),
            )

    def cancel_queued(self, job_id: str, now: float) -> bool:
        with self.lock:
            cur = self.connection.execute(
                "UPDATE jobs SET status='canceled', updated_at=?, finished_at=?"
                " WHERE id=? AND status IN ('queued','retry_wait')",
                (now, now, job_id),
            )
        return cur.rowcount == 1

    def mark_running(self, job_id: str, now: float) -> bool:
        with self.lock:
            cur = self.connection.execute(
                "UPDATE jobs SET status='running', started_at=?, updated_at=?,"
                " attempts=attempts+1"
                " WHERE id=? AND status IN ('queued','retry_wait')",
                (now, now, job_id),
            )
        return cur.rowcount == 1

    def requeue(self, job_id: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='running'",
                (time.time(), job_id),
            )

    def mark_retry(
        self,
        job_id: str,
        *,
        now: float,
        next_run_at: float,
        error_code: str | None,
        error: str | None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET status='retry_wait', updated_at=?, next_run_at=?,"
                " error_code=?, error=? WHERE id=?",
                (now, next_run_at, error_code, error, job_id),
            )

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        now: float,
        duration_ms: int,
        should_poll: bool | None = None,
        riot_status: int | None = None,
        details_json: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE jobs SET status=?, updated_at=?, finished_at=?, duration_ms=?,"
                " should_poll=?, riot_status=?, details=?, error_code=?, error=?,"
                " next_run_at=NULL WHERE id=?",
                (
                    status,
                    now,
                    now,
                    duration_ms,
                    None if should_poll is None else int(bool(should_poll)),
                    riot_status,
                    details_json,
                    error_code,
                    error,
                    job_id,
                ),
            )

    def mark_proxy(self, key: str, ok: bool, error: str | None) -> None:
        if not key:
            return
        with self.lock:
            self.connection.execute(
                "INSERT INTO proxy_health (key, ok, checked_at, error) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET ok=excluded.ok,"
                " checked_at=excluded.checked_at, error=excluded.error",
                (key, 1 if ok else 0, time.time(), error),
            )

    # ------------------------------------------------------------------- reads

    def get(self, job_id: str, client_id: str) -> Job:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE id=? AND client_id=?", (job_id, client_id)
            ).fetchone()
        if row is None:
            raise NotFound(f"job {job_id!r} not found")
        return self._row_to_job(row)

    def find(self, client_id: str, external_id: str) -> Job | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE client_id=? AND external_id=?",
                (client_id, external_id),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def proxy_owner(self, client_id: str, key: str, external_id: str | None) -> str | None:
        """Return the other account that already owns this proxy."""
        if not key:
            return None
        with self.lock:
            rows = self.connection.execute(
                "SELECT external_id FROM jobs WHERE client_id=? AND proxy_key=?",
                (client_id, key),
            ).fetchall()
        for row in rows:
            owner = row["external_id"]
            if external_id and owner == external_id:
                continue
            return str(owner) if owner else "another account"
        return None

    def list(
        self,
        client_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        updated_since: float | None = None,
    ) -> tuple[list[Job], int]:
        where = ["client_id = ?"]
        params: list[object] = [client_id]
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if updated_since is not None:
            where.append("updated_at > ?")
            params.append(updated_since)
        clause = " AND ".join(where)
        with self.lock:
            total = self.connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {clause}", params
            ).fetchone()[0]
            rows = self.connection.execute(
                f"SELECT * FROM jobs WHERE {clause}"
                " ORDER BY updated_at ASC, id ASC LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
        return [self._row_to_job(row) for row in rows], int(total)

    def status_counts(self, client_id: str | None = None) -> dict[str, int]:
        scope = " WHERE client_id=?" if client_id else ""
        params: tuple = (client_id,) if client_id else ()
        with self.lock:
            rows = self.connection.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs{scope} GROUP BY status", params
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def successful_durations(self, client_id: str | None = None) -> list[int]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT duration_ms FROM jobs WHERE status='succeeded'"
                " AND duration_ms IS NOT NULL"
                + (" AND client_id=?" if client_id else "")
                + " ORDER BY duration_ms DESC LIMIT 5000",
                (client_id,) if client_id else (),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def dead_error_counts(self, client_id: str | None = None) -> dict[str, int]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT error_code, COUNT(*) AS n FROM jobs WHERE status='dead'"
                + (" AND client_id=?" if client_id else "")
                + " GROUP BY error_code",
                (client_id,) if client_id else (),
            ).fetchall()
        return {(row["error_code"] or "unknown"): int(row["n"]) for row in rows}

    def dead_proxy_count(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM proxy_health WHERE ok=0"
            ).fetchone()
        return int(row[0])

    def queue_depth(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','retry_wait')"
            ).fetchone()
        return int(row[0])

    def running_count(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='running'"
            ).fetchone()
        return int(row[0])

    def average_duration_ms(self, client_id: str | None = None) -> float | None:
        where = "status='succeeded' AND duration_ms IS NOT NULL"
        if client_id:
            where += " AND client_id=?"
        with self.lock:
            row = self.connection.execute(
                f"SELECT AVG(duration_ms) FROM jobs WHERE {where}",
                (client_id,) if client_id else (),
            ).fetchone()
        return round(float(row[0]), 1) if row and row[0] is not None else None

    def batch_stats(self, batch_id: str | None) -> dict[str, int | float | None]:
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
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*), "
                f"SUM(CASE WHEN status IN {_TERMINAL_SQL} THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), "
                f"AVG(CASE WHEN status IN {_TERMINAL_SQL} AND duration_ms IS NOT NULL "
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
            "batch_average_duration_ms": round(float(row[3]), 1) if row[3] is not None else None,
            "batch_average_success_duration_ms": (
                round(float(row[4]), 1) if row[4] is not None else None
            ),
        }

    # --------------------------------------------------------------- dispatch

    def claim_next(
        self,
        now: float,
        *,
        max_per_client: int,
        is_proxy_busy: Callable[[str], bool],
    ) -> Job | None:
        """Move the next eligible row to running and return it.

        The whole selection runs under the store lock so two dispatchers can
        never claim the same row.
        """
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " OR (status='retry_wait' AND next_run_at <= ?)"
                " ORDER BY priority DESC, created_at ASC LIMIT 50",
                (now,),
            ).fetchall()
            running_by_client = {
                row["client_id"]: int(row["n"])
                for row in self.connection.execute(
                    "SELECT client_id, COUNT(*) AS n FROM jobs"
                    " WHERE status='running' GROUP BY client_id"
                )
            }
            for row in rows:
                if running_by_client.get(row["client_id"], 0) >= max_per_client:
                    continue
                if row["proxy_key"] and is_proxy_busy(row["proxy_key"]):
                    continue
                if not self.mark_running(row["id"], now):
                    continue
                claimed = self.connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
                return self._row_to_job(claimed)
        return None

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
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
