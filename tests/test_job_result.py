"""Shared interpretation of Riot's email-change responses."""

from __future__ import annotations

from src.jobs.result import (
    EMAIL_CHANGE_NOT_ACCEPTED,
    interpret_result,
    result_details,
    result_should_poll,
)


def test_interpret_accepts_should_poll_true():
    outcome = interpret_result(
        {
            "status": 200,
            "shouldPoll": True,
            "body": {"message": "email_verification_sent"},
        }
    )

    assert outcome.details == {"message": "email_verification_sent"}
    assert outcome.should_poll is True
    assert outcome.error_code is None
    assert outcome.error is None


def test_interpret_rejects_should_poll_false_with_riot_message():
    outcome = interpret_result(
        {
            "status": 200,
            "body": {"should_poll": False, "message": "email_verification_sent"},
        }
    )

    assert outcome.should_poll is False
    assert outcome.error_code == EMAIL_CHANGE_NOT_ACCEPTED
    assert "shouldPoll=false" in outcome.error
    assert "email_verification_sent" in outcome.error


def test_interpret_keeps_runner_error_and_wraps_scalar_body():
    outcome = interpret_result(
        {
            "status": 502,
            "shouldPoll": False,
            "body": "bad gateway",
            "error": "upstream said no",
        }
    )

    assert outcome.details == {"body": "bad gateway"}
    assert outcome.should_poll is False
    assert outcome.error_code == EMAIL_CHANGE_NOT_ACCEPTED
    assert outcome.error == "upstream said no"


def test_result_helpers_do_not_invent_values():
    result = {"body": None}
    assert result_details(result) is None
    assert result_should_poll(result, None) is None
