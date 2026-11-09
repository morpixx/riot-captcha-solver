"""Mapping from a browser run result to the public RunResponse."""

from __future__ import annotations

from typing import Any

from ..jobs.result import interpret_result
from ..security import redact
from .models import RunResponse


def build_response(result: Any) -> RunResponse:
    """Turn a run() dict into the documented API shape.

    Credentials are redacted here, so both success and error paths are safe.
    """
    if not isinstance(result, dict):
        return RunResponse(status=500, error=redact(f"unexpected run() result: {result!r}"))

    outcome = interpret_result(result)
    return RunResponse(
        shouldPoll=outcome.should_poll,
        status=result.get("status"),
        error_code=outcome.error_code,
        error=redact(outcome.error) if outcome.error else None,
        details=outcome.details,
    )
