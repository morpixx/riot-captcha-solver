"""Mint an hCaptcha token on the Riot page.

Camoufox runs page.evaluate in an isolated world where eval is not subject to
the document CSP (verified against Riot and a strict-CSP page).  That is what
replaces the old Chromium bypass_csp flag: hCaptcha api.js is fetched and
evaluated in that world, while the widget renders a real iframe in the page
DOM and delivers its callback there.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

# hCaptcha tokens are JWE-style strings.  Standard passes start with P1_;
# enterprise sites can issue E0_/E1_.  Anything else means the widget handed
# back a placeholder (for example the sitekey) and Riot would reject it.
HCAPTCHA_PREFIXES = ("P1_", "E0_", "E1_")

LOAD_AND_MINT = r"""
async ({sitekey, timeoutMs, expectedHost}) => {
  const diag = {
    apiLoaded: false,
    widgetId: null,
    error: null,
    renderError: null,
    executeError: null,
    challengeOpened: false,
    callbackAt: null,
    events: [],
  };
  const push = (name, value) => {
    diag.events.push([name, value === undefined ? null : value]);
    if (diag.events.length > 40) diag.events.shift();
  };
  const out = {host: location.hostname, href: location.href, diag};
  if (expectedHost && location.hostname !== expectedHost) {
    out.wrongOrigin = true;
    out.error = 'origin=' + location.hostname;
    return out;
  }
  const tokenValue = (value) =>
    value && typeof value === 'object' && typeof value.response === 'string'
      ? value.response
      : value;

  try {
    // The isolated world is exempt from the page CSP, so this also works on
    // origins that do not allow 'unsafe-eval'.
    try {
      const api = await (await fetch('https://js.hcaptcha.com/1/api.js?render=explicit')).text();
      (0, eval)(api);
      diag.apiLoaded = typeof window.hcaptcha === 'object' && window.hcaptcha !== null
        && typeof window.hcaptcha.render === 'function';
      push('api', diag.apiLoaded);
    } catch (e) {
      diag.error = 'hcaptcha API did not load: ' + String(e);
      push('api-error', String(e));
    }
    if (!diag.apiLoaded) {
      diag.error = diag.error || 'hcaptcha API did not load';
      return out;
    }

    const box = document.createElement('div');
    box.id = 'dsh-hc-' + Date.now();
    box.style.display = 'none';
    document.body.appendChild(box);

    window.__hcTok = null;
    window.__hcErr = null;
    try {
      diag.widgetId = hcaptcha.render(box, {
        sitekey: sitekey,
        size: 'invisible',
        callback: (t) => {
          window.__hcTok = tokenValue(t);
          diag.callbackAt = Date.now();
          push('callback', 'token');
        },
        'error-callback': (e) => {
          const text = String(e);
          window.__hcErr = text;
          diag.error = text;
          push('error-callback', text);
        },
        'chalexpired-callback': () => {
          diag.error = 'challenge-expired';
          push('chalexpired', null);
        },
        'close-callback': () => push('close', null),
        'open-callback': () => {
          diag.challengeOpened = true;
          push('open', null);
        },
      });
    } catch (e) {
      diag.renderError = String(e);
      diag.error = 'render: ' + String(e);
      push('render-error', String(e));
      return out;
    }

    try {
      const maybe = hcaptcha.execute(diag.widgetId, {async: true});
      if (maybe && typeof maybe.then === 'function') {
        maybe.then((t) => {
          const value = tokenValue(t);
          if (value) {
            window.__hcTok = value;
            diag.callbackAt = Date.now();
            push('promise', 'token');
          }
        }).catch((e) => {
          diag.executeError = String(e);
          diag.error = String(e);
          push('promise-error', String(e));
        });
      } else {
        push('execute', 'callback-mode');
      }
    } catch (e) {
      diag.executeError = String(e);
      diag.error = String(e);
      push('execute-error', String(e));
      try {
        hcaptcha.execute(diag.widgetId);
      } catch (e2) {
        diag.executeError += ' | fallback: ' + String(e2);
        push('execute-fallback-error', String(e2));
      }
    }

    const deadline = Date.now() + Math.max(10000, Number(timeoutMs) || 90000);
    while (Date.now() < deadline && !window.__hcTok && !window.__hcErr) {
      await new Promise((r) => setTimeout(r, 500));
    }
    out.token = window.__hcTok;
    out.error = window.__hcErr || diag.error;
    out.ua = navigator.userAgent;
    out.csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    diag.tokenPresent = Boolean(out.token);
    return out;
  } catch (e) {
    diag.error = String(e);
    push('fatal', String(e));
    return out;
  }
}
"""


def _token_problem(token: str, sitekey: str) -> str | None:
    """Explain why a value cannot be a real hCaptcha response token."""
    if not token:
        return "empty token"
    if token == sitekey:
        return "hCaptcha returned the sitekey instead of a token"
    if not token.startswith(HCAPTCHA_PREFIXES):
        return f"unexpected token prefix {token[:12]!r}"
    if len(token) < 100:
        return f"token is too short ({len(token)} chars)"
    return None


def mint_in_page(
    page,
    *,
    sitekey: str,
    host: str = "account.riotgames.com",
    timeout: int = 90,
    log=print,
) -> dict:
    """Mint an hCaptcha token and keep enough diagnostics to explain failure."""
    responses: list[dict] = []
    console: list[str] = []

    def on_response(response) -> None:
        url = response.url
        if "hcaptcha.com" not in url:
            return
        parsed = urlsplit(url)
        responses.append(
            {
                "host": parsed.hostname,
                "path": parsed.path,
                "status": response.status,
            }
        )
        if len(responses) > 30:
            del responses[:-30]

    def on_console(message) -> None:
        if message.type not in ("error", "warning"):
            return
        text = str(message.text)[:300]
        if "hcaptcha" in text.lower() or "captcha" in text.lower():
            console.append(text)
            if len(console) > 10:
                del console[:-10]

    page.on("response", on_response)
    page.on("console", on_console)
    log(f"[hcaptcha] minting on {host}, waiting up to {max(10, int(timeout))} s")
    try:
        result = page.evaluate(
            LOAD_AND_MINT,
            {
                "sitekey": sitekey,
                "timeoutMs": max(10, int(timeout)) * 1000,
                "expectedHost": host,
            },
        )
    finally:
        try:
            page.remove_listener("response", on_response)
            page.remove_listener("console", on_console)
        except Exception:
            pass

    if not isinstance(result, dict):
        raise RuntimeError(f"hCaptcha issued no token: unexpected result {result!r}")
    diag = result.get("diag") or {}
    actual_host = str(result.get("host") or "").strip().lower()
    expected_host = str(host or "").strip().lower()
    if result.get("wrongOrigin") or (
        expected_host and actual_host and actual_host != expected_host
    ):
        raise RuntimeError(
            f"hCaptcha was executed on {actual_host!r}, expected {expected_host!r}; "
            "the page left the Riot account origin"
        )
    if diag.get("challengeOpened"):
        log("[hcaptcha] challenge opened by hCaptcha")
    if not result.get("token"):
        reason = "challenge opened" if diag.get("challengeOpened") else "no callback"
        detail = {
            "reason": reason,
            "error": result.get("error") or diag.get("error"),
            "diag": diag,
            "network": responses,
            "console": console,
        }
        raise RuntimeError(
            f"hCaptcha issued no token ({reason}): " + json.dumps(detail, ensure_ascii=False)[:1500]
        )

    raw_token = result["token"]
    if isinstance(raw_token, dict):
        raw_token = raw_token.get("response") or raw_token.get("token") or ""
    token = str(raw_token).strip()

    problem = _token_problem(token, sitekey)
    if problem:
        detail = {
            "problem": problem,
            "token_prefix": token[:12],
            "diag": diag,
            "network": responses,
            "console": console,
        }
        raise RuntimeError(
            "hCaptcha returned an unusable token: " + json.dumps(detail, ensure_ascii=False)[:1500]
        )
    log(f"[hcaptcha] token {token[:10]}... len={len(token)} host={actual_host}")
    return result
