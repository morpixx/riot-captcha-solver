"""Interpretation of Riot's email-change response.

Riot answers HTTP 200 with shouldPoll=false when it did not accept the change
(for example, a pending request already exists or the current address is not
verified). That is not success: jobs become needs_resubmit and the same
explanation is returned by both the synchronous API and the queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EMAIL_CHANGE_NOT_ACCEPTED = "email_change_not_accepted"


@dataclass(frozen=True)
class RiotOutcome:
    """The part of a browser result that both API paths care about."""

    details: dict[str, Any] | None
    should_poll: bool | None
    error_code: str | None
    error: str | None


def result_details(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON body as details, wrapping non-object bodies."""
    body = result.get("body")
    if isinstance(body, dict):
        return body
    if body is None:
        return None
    return {"body": body}


def result_should_poll(
    result: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> bool | None:
    """Copy shouldPoll from Riot without inventing a value."""
    value = result.get("shouldPoll")
    if value is None and isinstance(details, dict):
        value = details.get("shouldPoll", details.get("should_poll"))
    return value


def email_change_not_accepted_error(message: str | None = None) -> str:
    suffix = f", message={message!r}" if message else ""
    return (
        "Riot did not accept the email change (shouldPoll=false"
        f"{suffix}); confirm the current email, wait for the pending "
        "request to expire, or resubmit the same external_id with a "
        "different address/domain"
    )


def interpret_result(result: dict[str, Any]) -> RiotOutcome:
    """Turn one browser run result into a consistent API/queue outcome.

    The rule that shouldPoll=false is a rejection lives here only. Callers
    still decide how to persist or expose the outcome.
    """
    details = result_details(result)
    should_poll = result_should_poll(result, details)

    error = result.get("error")
    error = str(error) if error else None
    error_code = None
    if should_poll is False:
        error_code = EMAIL_CHANGE_NOT_ACCEPTED
        if not error:
            message = details.get("message") if isinstance(details, dict) else None
            error = email_change_not_accepted_error(message)

    return RiotOutcome(
        details=details,
        should_poll=should_poll,
        error_code=error_code,
        error=error,
    )


__all__ = [
    "EMAIL_CHANGE_NOT_ACCEPTED",
    "RiotOutcome",
    "email_change_not_accepted_error",
    "interpret_result",
    "result_details",
    "result_should_poll",
]
