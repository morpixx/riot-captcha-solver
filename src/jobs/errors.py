"""Domain errors and error classification for retry decisions."""

from __future__ import annotations


class QueueFull(Exception):
    """Raised when the queue cannot accept more jobs."""


class RateLimited(Exception):
    """Raised when one client exceeds the submission rate limit."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(f"rate limited, retry after {self.retry_after}s")


class NotFound(Exception):
    """Raised when a job does not exist for the requesting client."""


class Conflict(Exception):
    """Raised when a job cannot move to the requested status."""


class PayloadMissing(Exception):
    """The job row exists, but its cookies are no longer in memory."""


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Return ``(error_code, retryable)`` for a failed job attempt."""
    msg = f"{type(exc).__name__}: {exc}".lower()

    if "429" in msg or "too many requests" in msg or "rate limit" in msg:
        return "rate_limited", True
    if "rejected the email change from the ui: 401" in msg or (
        "rejected the email change from the ui: 403" in msg
    ):
        return "session_invalid", False
    if "no webgl data" in msg:
        return "browser_fingerprint", True
    if "session is not valid" in msg or "left the riot account origin" in msg:
        return "session_invalid", False
    if "email is not verified" in msg:
        return "email_not_verified", False
    if "pending email change" in msg or "email_verification_pending" in msg:
        return "email_change_pending", False
    if "hcaptcha issued no token" in msg or "unusable token" in msg or "no token" in msg:
        return "captcha_failed", True
    proxy_markers = (
        "proxy",
        "socks",
        "ns_error_proxy",
        "err_proxy",
        "connection_refused",
        "econnrefused",
        "network is unreachable",
        "err_tunnel_connection_failed",
    )
    if any(marker in msg for marker in proxy_markers):
        return "proxy_error", False
    if "timed out" in msg or "timeout" in msg or "err_timed_out" in msg:
        return "timeout", True
    if "target closed" in msg or "browser has been closed" in msg or "crashed" in msg:
        return "browser_crashed", True
    return "unknown", True