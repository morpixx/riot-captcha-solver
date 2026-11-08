"""Pydantic models for the public HTTP API.

Keeping the request/response schemas out of 'server.py' makes the route
handlers easier to read and lets the API contract be tested without starting
the application.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from ..config import env_flag


def clean_webhook_url(value: str | None) -> str | None:
    """Return a normalized absolute http(s) URL, or None for an empty value."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise ValueError("webhook_url must be an absolute http:// or https:// URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("webhook_url has an invalid port") from exc
    return value


def _validate_proxy(value: str) -> str:
    value = value.strip()
    if not value and not env_flag("ALLOW_NO_PROXY"):
        raise ValueError("proxy must not be empty (set ALLOW_NO_PROXY=1 for direct tests)")
    return value


class Cookie(BaseModel):
    """Browser cookie subset understood by Playwright/Camoufox."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(..., min_length=1)
    value: str = ""
    domain: str = ".riotgames.com"
    path: str = "/"
    # Cookie-Editor emits 'expirationDate'; accept both spellings.
    expires: float | None = Field(
        default=None, validation_alias=AliasChoices("expires", "expirationDate")
    )
    httpOnly: bool | None = None
    secure: bool | None = None

    def to_cookie_dict(self) -> dict[str, Any]:
        cookie: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain or ".riotgames.com",
            "path": self.path or "/",
        }
        if self.expires is not None:
            cookie["expires"] = float(self.expires)
        if self.httpOnly is not None:
            cookie["httpOnly"] = self.httpOnly
        if self.secure is not None:
            cookie["secure"] = self.secure
        return cookie


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cookies: list[Cookie] = Field(..., min_length=1)
    proxy: str = Field(default="")
    new_email: EmailStr
    sitekey: str | None = None
    headless: bool | None = None

    _proxy_not_blank = field_validator("proxy")(_validate_proxy)


class RunResponse(BaseModel):
    shouldPoll: bool | None = None
    status: int | None = None
    error_code: str | None = None
    error: str | None = None
    details: dict[str, Any] | None = None


class JobSpecRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cookies: list[Cookie] = Field(..., min_length=1, max_length=200)
    proxy: str = Field(default="")
    new_email: EmailStr
    sitekey: str | None = None
    headless: bool | None = None
    external_id: str | None = Field(default=None, max_length=128)
    batch_id: str | None = Field(default=None, max_length=64)
    webhook_url: str | None = Field(default=None, max_length=2048)
    # Test-only: share this job's proxy with other test-mode jobs.
    test_mode: bool = False

    _proxy_not_blank = field_validator("proxy")(_validate_proxy)
    _webhook_url_valid = field_validator("webhook_url")(clean_webhook_url)


class JobsSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[JobSpecRequest] = Field(..., min_length=1)
    priority: int = 0
    batch_id: str | None = Field(default=None, max_length=64)
    webhook_url: str | None = Field(default=None, max_length=2048)
    # Test-only, gated by ALLOW_SHARED_PROXY_TEST: run several jobs through
    # the same proxy concurrently instead of serializing by proxy key.
    test_mode: bool = False

    _webhook_url_valid = field_validator("webhook_url")(clean_webhook_url)


class SubmitError(BaseModel):
    index: int
    external_id: str | None = None
    error: str


class JobView(BaseModel):
    id: str
    external_id: str | None = None
    batch_id: str | None = None
    status: str
    attempts: int
    shouldPoll: bool | None = None
    riot_status: int | None = None
    error_code: str | None = None
    error: str | None = None
    details: dict[str, Any] | None = None
    duration_ms: int | None = None
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None


class JobsSubmitResponse(BaseModel):
    accepted: int
    duplicates: int
    errors: list[SubmitError]
    jobs: list[JobView]


class JobsListResponse(BaseModel):
    items: list[JobView]
    total: int
    limit: int
    offset: int


__all__ = [
    "Cookie",
    "JobSpecRequest",
    "JobView",
    "JobsListResponse",
    "JobsSubmitRequest",
    "JobsSubmitResponse",
    "RunRequest",
    "RunResponse",
    "SubmitError",
    "clean_webhook_url",
]
