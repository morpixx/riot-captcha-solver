"""Riot account state read through the page's own API.

These checks reject a stale session, an unverified current address, or an
already pending change before spending an hCaptcha token. The rules about
what Riot's JSON means live only here.
"""

from __future__ import annotations

VERIFY_JS = r"""
async () => {
  try {
    const r = await fetch('/api/account/v1/user', {
      method: 'GET',
      credentials: 'include',
      headers: {'Accept': 'application/json'},
      cache: 'no-store',
    });
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    return {status: r.status, body: body};
  } catch (e) {
    return {status: 0, error: String(e)};
  }
}
"""

PENDING_JS = r"""
async () => {
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  try {
    const r = await fetch('/api/account/v1/user/email', {
      method: 'GET',
      credentials: 'include',
      headers: {'Accept': 'application/json', 'csrf-token': csrf},
      cache: 'no-store',
    });
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    return {status: r.status, body: body};
  } catch (e) {
    return {status: 0, error: String(e)};
  }
}
"""


def session_is_rejected(info: dict | None) -> bool:
    """True when Riot's own API says the cookie session is not valid."""
    if not info:
        return False
    status = info.get("status")
    if status == 401:
        return True
    if status != 403:
        return False
    body = info.get("body")
    if not isinstance(body, dict):
        return True
    code = str(body.get("errorCode") or body.get("error_code") or "").lower()
    return code in ("not_logged_in", "invalid_session_id")


def verify_session(page, log) -> dict | None:
    """Reject a stale cookie session before spending time on hCaptcha.

    The account page can render first and redirect client-side afterwards, so
    an origin check alone is not enough: ask the API that the page uses.
    Return the same answer so callers can also inspect the email state.
    """
    try:
        info = page.evaluate(VERIFY_JS)
    except Exception as exc:
        log(f"[*] session check skipped: {exc}")
        return None
    if session_is_rejected(info):
        detail = f"{info.get('status')} {info.get('body')!r}"
        raise RuntimeError(
            ("Riot session is not valid: /api/account/v1/user returned " + detail)[:200]
        )
    return info if isinstance(info, dict) else None


def email_verification_problem(info: dict | None) -> str | None:
    """Explain why Riot will refuse a change, or None when it may accept one.

    Riot answers 200 with shouldPoll=false for an unverified current address.
    That looks like a captcha rejection and burns a minted token, so the
    account state from /api/account/v1/user is checked up front.
    """
    if not isinstance(info, dict) or info.get("status") != 200:
        return None
    body = info.get("body")
    if not isinstance(body, dict):
        return None
    email_status = str(body.get("email_status") or "").strip().lower()
    states = body.get("derived_states")
    if not isinstance(states, list):
        states = []
    derived = {str(state).strip().lower() for state in states}
    if email_status != "not_validated" and "email_verification" not in derived:
        return None
    email = str(body.get("email") or "current email")
    return (
        f"Riot email is not verified ({email}, email_status={email_status or 'unknown'}); "
        "verify the current address before changing it"
    )


def pending_email_problem(page, log) -> str | None:
    """Refuse a duplicate submit while Riot already has a pending change."""
    try:
        info = page.evaluate(PENDING_JS)
    except Exception as exc:
        log(f"[*] pending email check skipped: {exc}")
        return None
    if not isinstance(info, dict) or info.get("status") != 200:
        return None
    body = info.get("body")
    if not isinstance(body, dict):
        return None
    email = str(body.get("email") or "").strip()
    message = str(body.get("message") or "").strip().lower()
    if email and (message == "email_verification_pending" or body.get("mode")):
        return (
            f"Riot already has a pending email change to {email}; "
            "confirm it or wait for it to expire before submitting another"
        )
    return None
