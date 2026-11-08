"""Job domain types and status constants shared by the queue and the API."""

from __future__ import annotations

from dataclasses import dataclass

TERMINAL = ("succeeded", "dead", "canceled", "needs_resubmit")
ACTIVE = ("queued", "retry_wait", "running")
ALL_STATUSES = (
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "dead",
    "canceled",
    "needs_resubmit",
)


@dataclass(frozen=True)
class JobSpec:
    """Everything needed to enqueue one email-change job."""

    cookies: list[dict]
    proxy: str
    new_email: str
    sitekey: str | None = None
    headless: bool | None = None
    external_id: str | None = None
    webhook_url: str | None = None
    batch_id: str | None = None
    # Test-only: allow several jobs to share one proxy concurrently.
    allow_shared_proxy: bool = False


@dataclass
class Job:
    """Persisted job state; secrets stay in the queue's in-memory payloads."""

    id: str
    client_id: str
    external_id: str | None
    status: str
    attempts: int
    priority: int
    proxy_key: str
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    next_run_at: float | None = None
    duration_ms: int | None = None
    should_poll: bool | None = None
    riot_status: int | None = None
    error_code: str | None = None
    error: str | None = None
    details: dict | None = None
    # In memory only: the client's webhook address never reaches SQLite or API views.
    webhook_url: str | None = None
    batch_id: str | None = None

    def to_dict(self, *, include_details: bool = True) -> dict:
        out = {
            "id": self.id,
            "external_id": self.external_id,
            "batch_id": self.batch_id,
            "status": self.status,
            "attempts": self.attempts,
            "shouldPoll": self.should_poll,
            "riot_status": self.riot_status,
            "error_code": self.error_code,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_details:
            out["details"] = self.details
        return out
