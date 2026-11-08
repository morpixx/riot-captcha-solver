"""Error classification for retry decisions."""

from __future__ import annotations


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Return ``(error_code, retryable)`` for a failed job attempt."""
    msg = f"{type(exc).__name__}: {exc}".lower()

    if "no webgl data" in msg:
        return "browser_fingerprint", True
    if "session is not valid" in msg or "left the riot account origin" in msg:
        return "session_invalid", False
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
