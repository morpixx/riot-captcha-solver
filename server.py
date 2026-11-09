"""REST API сервер для автоматизации смены почты на аккаунтах Riot Games.

Запуск:
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 8000

Cookies и прокси-креды не пишутся на диск: cookies передаются в
src.riot.flow напрямую из запроса и живут только в памяти.
SQLite (jobs.sqlite3) хранит только метаданные job'ов, без секретов.

Эндпоинты:
    POST /api/v1/run          — синхронный одиночный прогон (для отладки)
    POST /api/v1/jobs         — batch-постановка в очередь (+ webhook_url на job/batch)
    GET  /api/v1/jobs         — список/поллинг (fallback, если webhook не дошёл)
    GET  /api/v1/jobs/{id}    — статус и результат
    POST /api/v1/jobs/{id}/cancel
    GET  /api/v1/stats        — очередь, воркеры, p50/p95, ошибки
    GET  /api/v1/health

Переменные окружения:
    MAX_WORKERS     — воркеров очереди и синхронного пула (по умолчанию 5)
    API_KEY         — один ключ (client_id=default) для X-API-Key
    API_KEYS        — JSON {"key": "client_id", ...} вместо API_KEY
    MAX_QUEUE       — максимум queued+retry_wait (по умолчанию 5000)
    BATCH_MAX       — максимум job'ов в одном POST /jobs (по умолчанию 500)
    JOB_ATTEMPTS    — попыток на job (по умолчанию 3)
    RETRY_BASE      — база экспоненциального backoff, сек (30)
    RAMP_INTERVAL   — пауза между стартами воркеров, сек (1.0)
    MIN_FREE_MB     — пауза очереди при нехватке RAM (2048)
    RATE_LIMIT_PER_MIN — запросов /jobs и /job в минуту на клиента (1200)
    JOBS_DB         — путь к SQLite (по умолчанию jobs.sqlite3)
    SHUTDOWN_TIMEOUT — сколько ждать завершения running-джоб при остановке (10)
    ENABLE_SYNC_RUN — 0 отключает синхронный /run (для production-очереди)
    ALLOW_NO_PROXY — 1 разрешает job/run без proxy (только локальные тесты)
    HEADLESS        — 0 включает видимое окно; по умолчанию headless (job может переопределить)
    SITEKEY/HOST    — параметры hCaptcha (по умолчанию — Riot)
    RUN_TIMEOUT     — таймаут синхронного /run, сек (0 — без таймаута)
    BIND_HOST/PORT  — адрес uvicorn в __main__ (0.0.0.0:8000)
    ENABLE_DOCS     — 0 отключает /docs, /redoc, /openapi.json (для выдачи наружу)
    LOG_LEVEL       — уровень логирования (INFO)
    WEBHOOK_URL     — общий fallback-адрес, если job не передал свой webhook_url
    WEBHOOK_PROXY_URL — опциональный локальный приёмник; пусто = писать
                      в data/output/webhook-results.jsonl
    WEBHOOK_SECRET  — секрет HMAC-SHA256 для X-Webhook-Signature
    WEBHOOK_TIMEOUT — таймаут одного POST на webhook, сек (5)
    WEBHOOK_RETRIES — попыток доставки webhook (3)
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:  # запуск из любого каталога: python D:\project\server.py
    sys.path.insert(0, str(ROOT))

from src.api.auth import api_key_dependency, load_api_keys
from src.api.models import (
    JobsListResponse,
    JobSpecRequest,
    JobsSubmitRequest,
    JobsSubmitResponse,
    JobView,
    RunRequest,
    RunResponse,
)
from src.api.run import build_response
from src.api.webhook_receiver import handle_webhook
from src.api.webhooks import WebhookDispatcher, WebhookSettings
from src.config import Account, Config, env_flag, load_flow_config
from src.jobs.queue import (
    ALL_STATUSES,
    Conflict,
    Job,
    JobQueue,
    JobSpec,
    NotFound,
    QueueFull,
    RateLimited,
)
from src.jobs.runner import run_account
from src.proxy.proxy import describe, parse_proxy
from src.security import email_domain, email_tag, mask_proxy, redact, safe_error

QueueJobSpec = JobSpec

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

log = logging.getLogger("riot.api")
ALLOW_NO_PROXY = env_flag("ALLOW_NO_PROXY")
ALLOW_SHARED_PROXY_TEST = env_flag("ALLOW_SHARED_PROXY_TEST")
WEBHOOK_PROXY_URL = os.getenv("WEBHOOK_PROXY_URL", "").strip()
WEBHOOK_JOURNAL = ROOT / "data" / "output" / "webhook-results.jsonl"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(message)s",
    stream=sys.stdout,
    force=True,
)

MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "5")))
API_KEY = os.getenv("API_KEY", "").strip()
RUN_TIMEOUT = float(os.getenv("RUN_TIMEOUT", "0") or 0) or None

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT", "5") or 5)
WEBHOOK_RETRIES = max(1, int(os.getenv("WEBHOOK_RETRIES", "3")))
if WEBHOOK_URL and not WEBHOOK_URL.startswith(("http://", "https://")):
    raise RuntimeError("WEBHOOK_URL must start with http:// or https://")


API_KEYS_MAP = load_api_keys(API_KEY, os.getenv("API_KEYS", ""))
DOCS_ENABLED = env_flag("ENABLE_DOCS", default=True)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# The server does not use accounts.json; every request carries its own account.
cfg = load_flow_config(accounts=[], allow_email_change=True)

# --------------------------------------------------------------------------- #
# Пул потоков (создаётся на старте приложения)
# --------------------------------------------------------------------------- #

executor: ThreadPoolExecutor | None = None
job_queue: JobQueue | None = None
QUEUE_RUNNER: Callable | None = None  # test seam for a fake browser runner
webhook_dispatcher: WebhookDispatcher | None = None


def _enqueue_webhook(job: Job) -> None:
    """Queue a terminal event using the per-job URL or the global fallback."""
    dispatcher = webhook_dispatcher
    url = (job.webhook_url or WEBHOOK_URL or "").strip()
    if dispatcher is None or not url:
        return
    payload = job.to_dict(include_details=True)
    payload["event"] = "job.terminal"
    queue = job_queue
    payload["average_duration_ms"] = (
        queue.average_duration_ms(client_id=job.client_id) if queue is not None else None
    )
    payload.update(queue.batch_stats(job.batch_id) if queue is not None else {})
    dispatcher.enqueue(url, payload)


def get_executor() -> ThreadPoolExecutor:
    """Пул с жёстким лимитом: больше MAX_WORKERS браузеров одновременно не поднимется."""
    global executor
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="riot-run")
    return executor


def get_queue() -> JobQueue:
    if job_queue is None:
        raise HTTPException(status_code=503, detail="job queue is not running")
    return job_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor, job_queue, webhook_dispatcher
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="riot-run")
    job_queue = JobQueue(cfg, MAX_WORKERS, runner=QUEUE_RUNNER)
    # Start the webhook worker even without a global URL: jobs may carry their own.
    dispatcher = WebhookDispatcher(
        WebhookSettings(
            secret=WEBHOOK_SECRET,
            timeout=WEBHOOK_TIMEOUT,
            retries=WEBHOOK_RETRIES,
        ),
        post=requests.post,
    )
    dispatcher.start()
    webhook_dispatcher = dispatcher
    job_queue.terminal_callback = _enqueue_webhook
    job_queue.start()
    log.info(
        "[%s] START workers=%d api_key=%s timeout=%s queue_db=%s webhook_fallback=%s",
        _ts(),
        MAX_WORKERS,
        "on" if API_KEYS_MAP else "off",
        RUN_TIMEOUT or "none",
        job_queue.db_path,
        "on" if WEBHOOK_URL else "off",
    )
    try:
        yield
    finally:
        log.info("[%s] STOP shutting down workers", _ts())
        if job_queue is not None:
            # Let running jobs finish and queue their terminal callbacks first.
            job_queue.close(timeout=float(os.getenv("SHUTDOWN_TIMEOUT", "10")))
            job_queue.terminal_callback = None
            job_queue = None
        # Do not keep the process alive because a webhook receiver is down.
        webhook_dispatcher = None
        dispatcher.stop(drain_timeout=min(5.0, float(os.getenv("SHUTDOWN_TIMEOUT", "10"))))
        executor.shutdown(wait=False, cancel_futures=True)
        executor = None


app = FastAPI(
    title="Riot Email Change API",
    version="1.0.0",
    description="Смена почты на аккаунтах Riot Games: cookies + прокси -> hCaptcha -> POST.",
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

# --------------------------------------------------------------------------- #
# Авторизация и служебные хелперы
# --------------------------------------------------------------------------- #


require_client = api_key_dependency(lambda: API_KEYS_MAP)


def execute_run(
    flow_cfg: Config,
    account: Account,
    proxy: dict | None,
    headless: bool | None,
) -> dict:
    """Blocking run of one account.

    Camoufox generates and validates the fingerprint before the first page
    loads, so a browser-level retry is not part of the flow.
    """
    return run_account(flow_cfg, account, proxy, headless)


# --------------------------------------------------------------------------- #
# Middleware / обработчики ошибок
# --------------------------------------------------------------------------- #


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception(
            "[%s] %s %s -> unhandled error (%.2fs)",
            _ts(),
            request.method,
            request.url.path,
            time.perf_counter() - started,
        )
        raise
    log.info(
        "[%s] %s %s -> %d (%.2fs)",
        _ts(),
        request.method,
        request.url.path,
        response.status_code,
        time.perf_counter() - started,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("[%s] ERROR %s %s: %s", _ts(), request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# --------------------------------------------------------------------------- #
# Эндпоинты
# --------------------------------------------------------------------------- #

@app.post("/api/v1/webhook")
async def webhook_receiver(request: Request) -> Response:
    """Receive a terminal webhook on the same port as the API.

    With WEBHOOK_PROXY_URL empty (default) the payload is appended to
    data/output/webhook-results.jsonl; otherwise it is forwarded to that
    localhost receiver. One public URL then serves API and webhook traffic.
    """
    body = await request.body()
    return await handle_webhook(
        body,
        request.headers,
        secret=WEBHOOK_SECRET,
        proxy_url=WEBHOOK_PROXY_URL,
        journal=WEBHOOK_JOURNAL,
    )


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "backend": "camoufox"}


@app.post("/api/v1/run", response_model=RunResponse)
def run_endpoint(req: RunRequest, client_id: str = Depends(require_client)) -> RunResponse:
    """Синхронный эндпоинт: FastAPI выполняет его в своём threadpool, а сам прогон
    ограничен нашим ThreadPoolExecutor (MAX_WORKERS), поэтому event loop не блокируется
    и сервер не поднимает больше браузеров, чем разрешено.
    """
    if not env_flag("ENABLE_SYNC_RUN", default=True):
        raise HTTPException(
            status_code=403, detail="sync /api/v1/run is disabled; use /api/v1/jobs"
        )

    email = str(req.new_email)

    try:
        proxy = parse_proxy(req.proxy)
    except ValueError as exc:
        log.warning(
            "[%s] RUN rejected: bad proxy=%s (%s)",
            _ts(),
            mask_proxy(req.proxy),
            redact(exc, req.proxy),
        )
        raise HTTPException(
            status_code=400,
            detail=f"invalid proxy: {redact(exc, req.proxy)}",
        ) from exc

    if proxy is None and not ALLOW_NO_PROXY:
        raise HTTPException(status_code=400, detail="invalid proxy: empty value")

    log.info(
        "[%s] RUN client=%s account=%s domain=%s proxy=%s headless=%s",
        _ts(),
        client_id,
        email_tag(email),
        email_domain(email),
        describe(proxy),
        req.headless,
    )

    if req.sitekey and req.sitekey != cfg.sitekey:
        flow_cfg = replace(cfg, sitekey=req.sitekey)
    else:
        flow_cfg = cfg

    account = Account(
        name="api",
        cookies=[cookie.to_cookie_dict() for cookie in req.cookies],
        new_email=email,
        proxy=req.proxy,
    )
    started = time.perf_counter()
    future = get_executor().submit(execute_run, flow_cfg, account, proxy, req.headless)
    try:
        result = future.result(timeout=RUN_TIMEOUT) if RUN_TIMEOUT else future.result()
    except FutureTimeoutError:
        future.cancel()
        log.error(
            "[%s] RUN timeout after %ss account=%s (%.1fs)",
            _ts(),
            RUN_TIMEOUT,
            email_tag(email),
            time.perf_counter() - started,
        )
        return RunResponse(status=504, error=f"run timed out after {RUN_TIMEOUT}s")
    except Exception as exc:
        error = safe_error(exc, req.proxy)
        log.error("[%s] RUN failed account=%s: %s", _ts(), email_tag(email), error)
        log.debug(
            "[%s] RUN traceback: %s",
            _ts(),
            redact(traceback.format_exc(), req.proxy),
        )
        return RunResponse(status=500, error=error)

    response = build_response(result)
    log.info(
        "[%s] DONE account=%s status=%s shouldPoll=%s in %.1fs%s",
        _ts(),
        email_tag(email),
        response.status,
        response.shouldPoll,
        time.perf_counter() - started,
        f" error={response.error}" if response.error else "",
    )
    return response


# --------------------------------------------------------------------------- #
# Очередь job'ов
# --------------------------------------------------------------------------- #


def _job_view(job: Job, *, include_details: bool = True) -> dict:
    return job.to_dict(include_details=include_details)


def _parse_statuses(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    statuses = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [item for item in statuses if item not in ALL_STATUSES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown status: {unknown}")
    return statuses


def _queue_spec(
    item: JobSpecRequest,
    default_webhook: str | None = None,
    *,
    allow_shared_proxy: bool = False,
    batch_id: str | None = None,
) -> QueueJobSpec:
    return QueueJobSpec(
        cookies=[cookie.to_cookie_dict() for cookie in item.cookies],
        proxy=item.proxy,
        new_email=str(item.new_email),
        sitekey=item.sitekey,
        headless=item.headless,
        external_id=item.external_id or None,
        webhook_url=item.webhook_url or default_webhook,
        batch_id=item.batch_id or batch_id,
        allow_shared_proxy=allow_shared_proxy,
    )


def _check_rate(queue: JobQueue, client_id: str) -> None:
    try:
        queue.check_rate(client_id)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


@app.post("/api/v1/jobs", response_model=JobsSubmitResponse)
def submit_jobs(
    req: JobsSubmitRequest, client_id: str = Depends(require_client)
) -> JobsSubmitResponse:
    """Batch-постановка job'ов. Секреты остаются в памяти процесса."""
    queue = get_queue()
    if len(req.jobs) > queue.batch_max:
        raise HTTPException(
            status_code=413,
            detail=f"at most {queue.batch_max} jobs per request (got {len(req.jobs)})",
        )
    shared_test = req.test_mode or any(item.test_mode for item in req.jobs)
    if shared_test and not ALLOW_SHARED_PROXY_TEST:
        raise HTTPException(
            status_code=400,
            detail="test_mode requires ALLOW_SHARED_PROXY_TEST=1 on the server",
        )
    _check_rate(queue, client_id)
    batch_id = req.batch_id or uuid.uuid4().hex[:12]
    specs = [
        _queue_spec(
            item,
            req.webhook_url,
            allow_shared_proxy=req.test_mode or item.test_mode,
            batch_id=batch_id,
        )
        for item in req.jobs
    ]

    try:
        jobs, duplicates, errors = queue.submit(client_id, specs, priority=req.priority)
    except QueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "30"},
        ) from exc

    log.info(
        "[%s] JOBS client=%s submitted=%d duplicates=%d rejected=%d queue_depth=%d",
        _ts(),
        client_id,
        len(jobs) - duplicates,
        duplicates,
        len(errors),
        queue.queue_depth(),
    )
    return JobsSubmitResponse(
        accepted=len(jobs) - duplicates,
        duplicates=duplicates,
        errors=errors,
        jobs=[_job_view(job, include_details=False) for job in jobs],
    )


@app.post("/api/v1/job", response_model=JobView, status_code=202)
def submit_one_job(item: JobSpecRequest, client_id: str = Depends(require_client)) -> JobView:
    """Один аккаунт — один POST (удобно для туннеля и построчной подачи)."""
    queue = get_queue()
    _check_rate(queue, client_id)
    if item.test_mode and not ALLOW_SHARED_PROXY_TEST:
        raise HTTPException(
            status_code=400,
            detail="test_mode requires ALLOW_SHARED_PROXY_TEST=1 on the server",
        )
    try:
        jobs, duplicates, errors = queue.submit(
            client_id,
            [
                _queue_spec(
                    item,
                    allow_shared_proxy=item.test_mode,
                    batch_id=item.batch_id or uuid.uuid4().hex[:12],
                )
            ],
            priority=0,
        )
    except QueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "30"},
        ) from exc
    if errors:
        raise HTTPException(status_code=400, detail=errors[0]["error"])

    job = jobs[0]
    log.info(
        "[%s] JOB client=%s id=%s external_id=%s status=%s duplicate=%s",
        _ts(),
        client_id,
        job.id[:8],
        job.external_id,
        job.status,
        duplicates,
    )
    return JobView(**_job_view(job))


@app.get("/api/v1/jobs", response_model=JobsListResponse)
def list_jobs(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    updated_since: float | None = None,
    client_id: str = Depends(require_client),
) -> JobsListResponse:
    """Список job'ов клиента; updated_since — для дельта-поллинга после рестарта."""
    queue = get_queue()
    try:
        items, total = queue.list_jobs(
            client_id,
            statuses=_parse_statuses(status),
            limit=limit,
            offset=offset,
            updated_since=updated_since,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JobsListResponse(
        items=[_job_view(job, include_details=True) for job in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, client_id: str = Depends(require_client)) -> JobView:
    try:
        job = get_queue().get(job_id, client_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JobView(**_job_view(job))


@app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str, client_id: str = Depends(require_client)) -> JobView:
    try:
        job = get_queue().cancel(job_id, client_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobView(**_job_view(job))


@app.get("/api/v1/stats")
def stats(client_id: str = Depends(require_client)) -> dict:
    data = get_queue().stats(client_id=client_id)
    data["client_id"] = client_id
    return data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("BIND_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
