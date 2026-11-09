"""Same-port webhook receiver: signature check, journal and forwarding.

One public URL serves both the job API and terminal webhooks. With no
WEBHOOK_PROXY_URL the payload is appended to a local JSONL journal; with one,
it is forwarded to that localhost receiver.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("riot.api")

MAX_BODY_BYTES = 2_000_000
_journal_lock = threading.Lock()
HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def verify_signature(secret: str, provided: str, body: bytes) -> None:
    if not secret:
        return
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


def store_payload(journal: Path, body: bytes) -> None:
    """Append one terminal webhook payload to a JSONL journal."""
    payload = json.loads(body.decode("utf-8"))
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    with _journal_lock, journal.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def is_local_target(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in ("http", "https") and parsed.hostname in (
        "127.0.0.1",
        "localhost",
        "::1",
    )


def is_self_target(url: str) -> bool:
    return urlsplit(url).path.rstrip("/") == "/api/v1/webhook"


async def handle_webhook(
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str,
    proxy_url: str,
    journal: Path,
) -> Response:
    """Validate and deliver one webhook request."""
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="webhook payload too large")
    verify_signature(secret, headers.get("X-Webhook-Signature", ""), body)

    if not proxy_url:
        try:
            await run_in_threadpool(store_payload, journal, body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail="webhook payload must be JSON",
            ) from exc
        except OSError as exc:
            log.error("WEBHOOK journal write failed: %s", exc)
            raise HTTPException(status_code=500, detail="could not store webhook payload") from exc
        try:
            label = str(journal.relative_to(Path.cwd()))
        except ValueError:
            label = str(journal)
        log.info("WEBHOOK stored -> %s", label)
        return JSONResponse({"ok": True, "stored": True, "file": label})

    if not is_local_target(proxy_url):
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_PROXY_URL must point to localhost",
        )
    if is_self_target(proxy_url):
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_PROXY_URL must not point back to /api/v1/webhook",
        )
    forwarded_headers = {
        key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS
    }
    try:
        upstream = await run_in_threadpool(
            requests.post,
            proxy_url,
            data=body,
            headers=forwarded_headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        log.warning("WEBHOOK proxy unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=502,
            content={"detail": "local webhook receiver is unavailable"},
        )
    response_headers = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )