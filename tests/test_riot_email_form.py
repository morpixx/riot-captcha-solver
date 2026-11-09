"""Tests for the Riot email card: fill and click SAVE AND VERIFY (no browser)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.riot import email_form


class FakeResponse:
    def __init__(self, status: int = 200, body=None, *, json_error: Exception | None = None):
        self.status = status
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body

    def text(self) -> str:
        return "denied by Riot"


class FakeResponseContext:
    def __init__(self, page: FakePage):
        self.page = page
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.page.response is None:
            raise TimeoutError("no email-change POST")
        self.value = self.page.response


class FakeLocator:
    def __init__(self, page: FakePage, selector: str, *, enabled=True, disabled=False):
        self.page = page
        self.selector = selector
        self.enabled = enabled
        self.disabled = disabled
        self.waits: list[dict] = []
        self.clicks = 0
        self.fills: list[str] = []

    def wait_for(self, **kwargs):
        self.waits.append(kwargs)

    def is_enabled(self) -> bool:
        return self.enabled

    def is_disabled(self) -> bool:
        return self.disabled

    def fill(self, value: str):
        self.fills.append(value)
        self.page.on_fill()

    def click(self):
        self.clicks += 1
        self.page.clicks.append(self.selector)


class FakePage:
    def __init__(
        self,
        *,
        input_enabled: bool = True,
        keep_save_disabled: bool = False,
        response: FakeResponse | None = None,
    ):
        self.response = response
        self.keep_save_disabled = keep_save_disabled
        self.clicks: list[str] = []
        self.selector_waits: list[tuple[str, dict]] = []
        self.timeouts: list[int] = []
        self.attempts = 0
        self.predicate = None
        self.input_box = FakeLocator(self, email_form.EMAIL_INPUT, enabled=input_enabled)
        self.save = FakeLocator(self, email_form.SAVE_BUTTON, disabled=keep_save_disabled)

    def on_fill(self) -> None:
        if not self.keep_save_disabled:
            self.save.disabled = False

    def locator(self, selector: str) -> FakeLocator:
        if selector == email_form.EMAIL_INPUT:
            return self.input_box
        assert selector == email_form.SAVE_BUTTON
        return self.save

    def wait_for_selector(self, selector: str, **kwargs):
        self.selector_waits.append((selector, kwargs))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)

    def expect_response(self, predicate, **_kwargs):
        self.attempts += 1
        self.predicate = predicate
        return FakeResponseContext(self)


class AdvanceOneSecond:
    """Fake clock: every call advances one second, so the 10s guard is fast."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now


def test_submit_via_ui_fills_the_card_and_clicks_save():
    page = FakePage(response=FakeResponse(200, {"shouldPoll": True}))

    result = email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    assert result == {"status": 200, "body": {"shouldPoll": True}}
    assert page.input_box.fills == ["new@example.com"]
    assert page.input_box.waits[0]["state"] == "visible"
    assert page.save.waits[0]["state"] == "visible"
    assert page.clicks == [email_form.EMAIL_INPUT, email_form.SAVE_BUTTON]
    assert page.save.clicks == 1
    assert page.attempts == 1
    assert page.selector_waits[0][0] == 'iframe[src*="hcaptcha"]'


def test_submit_via_ui_matches_only_the_email_change_post():
    page = FakePage(response=FakeResponse(200, {}))
    email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    post = SimpleNamespace(
        url="https://account.riotgames.com/api/account/v1/user/email",
        request=SimpleNamespace(method="POST"),
    )
    get = SimpleNamespace(url=post.url, request=SimpleNamespace(method="GET"))
    other = SimpleNamespace(
        url="https://account.riotgames.com/api/account/v1/user",
        request=SimpleNamespace(method="POST"),
    )

    assert page.predicate(post) is True
    assert page.predicate(get) is False
    assert page.predicate(other) is False


def test_submit_via_ui_rejects_a_disabled_email_field():
    page = FakePage(input_enabled=False)

    with pytest.raises(RuntimeError, match="email field is disabled"):
        email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    assert page.clicks == []


def test_submit_via_ui_reports_a_stuck_save_button(monkeypatch):
    page = FakePage(keep_save_disabled=True)
    monkeypatch.setattr(email_form, "time", SimpleNamespace(monotonic=AdvanceOneSecond().monotonic))

    with pytest.raises(RuntimeError, match="stayed disabled"):
        email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    assert page.clicks == [email_form.EMAIL_INPUT]
    assert page.save.clicks == 0


def test_submit_via_ui_reports_a_missing_post_response():
    page = FakePage(response=None)

    with pytest.raises(RuntimeError, match="did not submit the email change"):
        email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    assert page.attempts == email_form.SUBMIT_ATTEMPTS
    assert page.save.clicks == email_form.SUBMIT_ATTEMPTS


def test_submit_via_ui_raises_on_http_error_and_keeps_text_fallback():
    page = FakePage(response=FakeResponse(400, json_error=ValueError("not json")))

    with pytest.raises(RuntimeError) as caught:
        email_form.submit_via_ui(page, "new@example.com", timeout_ms=5000, log=lambda _m: None)

    message = str(caught.value)
    assert "400" in message
    assert "denied by Riot" in message