#!/usr/bin/env python3
"""Интерактивный экстрактор cookie для нескольких аккаунтов Riot Games.

Что делает:
  1. Открывает Camoufox (Firefox) на account.riotgames.com.
  2. Вы входите в аккаунт руками, включая 2FA/hCaptcha, если они есть.
  3. Скрипт ждёт валидную сессию, забирает cookie домена riotgames.com.
  4. Дописывает/обновляет одну строку в data/input/accounts.jsonl.

Формат строки совпадает с элементом jobs для POST /api/v1/jobs:
  {"external_id": "...", "label": "...", "cookies": [...],
   "proxy": "...", "new_email": "...", "headless": true, ...}

Примеры:
  python cookie_extractor.py                 # меню
  python cookie_extractor.py add             # добавить аккаунт
  python cookie_extractor.py relogin acc-01  # перелогинить аккаунт
  python cookie_extractor.py list
  python cookie_extractor.py export --format batch --out batch.json
  python cookie_extractor.py remove acc-01

Готовый data/input/accounts.jsonl можно сразу отдать scripts/submit_accounts.py.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.config import DATA_INPUT
from src.console import configure_utf8_stdio
from src.extractor.browser import (
    DEFAULT_LOGIN_URL,
    DEFAULT_TIMEOUT,
    VERIFY_JS,
    _find_account_page,
    _has_session_cookie,
    _is_not_logged_in,
    _page_is_blocked,
    _verify_page_session,
    capture_account,
)
from src.extractor.data import (
    DEFAULT_DOMAIN,
    DEFAULT_SESSION_COOKIE,
    DEFAULT_STORE,
    EMAIL_RE,
    EXTERNAL_ID_RE,
    MAX_COOKIES,
    CookieStore,
    ExtractorError,
    build_server_job,
    clean_label,
    clean_proxy,
    describe_proxy,
    domain_matches,
    export_jobs,
    extract_cookies,
    format_expiry,
    load_emails,
    log_to_stderr,
    make_external_id,
    next_label,
    normalize_cookie,
    now_iso,
    session_cookie_info,
    slugify,
    validate_email,
    validate_external_id,
)
from src.proxy.pool import (
    ProxyPoolError,
    ProxyProbe,
    choose_proxy,
    load_proxy_pool,
    proxy_key,
    rank_proxies,
)
from src.proxy.scan import scan_until

configure_utf8_stdio()

DEFAULT_PROXY_TEST_URL = os.environ.get("PROXY_TEST_URL", "https://account.riotgames.com/")
DEFAULT_PROXY_POOL = Path(os.environ["PROXY_POOL"]) if os.environ.get("PROXY_POOL") else None

Log = Callable[[str], None]
log = logging.getLogger("riot.extractor")

# The names below are part of cookie_extractor's historical import surface.
# Tests and small scripts import them from here, so keep the re-exports explicit.
__all__ = [
    "DEFAULT_DOMAIN",
    "DEFAULT_LOGIN_URL",
    "DEFAULT_PROXY_POOL",
    "DEFAULT_PROXY_TEST_URL",
    "DEFAULT_SESSION_COOKIE",
    "DEFAULT_STORE",
    "DEFAULT_TIMEOUT",
    "EMAIL_RE",
    "EXTERNAL_ID_RE",
    "MAX_COOKIES",
    "VERIFY_JS",
    "CookieStore",
    "ExtractorError",
    "_find_account_page",
    "_has_session_cookie",
    "_is_not_logged_in",
    "_page_is_blocked",
    "_verify_page_session",
    "build_parser",
    "build_server_job",
    "capture_account",
    "clean_label",
    "clean_proxy",
    "cmd_add",
    "cmd_add_batch",
    "cmd_assign_proxies",
    "cmd_export",
    "cmd_list",
    "cmd_proxy_scan",
    "cmd_relogin",
    "cmd_remove",
    "confirm",
    "describe_proxy",
    "domain_matches",
    "export_jobs",
    "extract_cookies",
    "format_expiry",
    "load_emails",
    "log_to_stderr",
    "main",
    "make_external_id",
    "next_label",
    "normalize_cookie",
    "now_iso",
    "print_accounts",
    "prompt",
    "session_cookie_info",
    "slugify",
    "truncate",
    "validate_email",
    "validate_external_id",
]


# --------------------------------------------------------------------------- #
# Proxy pool helpers
# --------------------------------------------------------------------------- #


def _proxy_key_or_raw(value: str | None) -> str:
    if not value:
        return ""
    try:
        return proxy_key(value)
    except ProxyPoolError:
        return str(value).strip().casefold()


def _rank_pool(
    path: str | Path,
    *,
    test_url: str = DEFAULT_PROXY_TEST_URL,
    timeout: float = 8.0,
    workers: int = 24,
    top_n: int = 0,
    allow_unverified: bool = False,
    max_latency_ms: float = 3000.0,
    exclude: set[str] | None = None,
    log: Log = print,
) -> list[ProxyProbe]:
    try:
        proxies = load_proxy_pool(path)
        if exclude:
            proxies = [proxy for proxy in proxies if proxy_key(proxy) not in exclude]
        log(f"[*] проверяю {len(proxies)} proxy из {path} ...")
        probes = rank_proxies(
            proxies,
            test_url=test_url,
            timeout=timeout,
            workers=workers,
            top_n=top_n,
            allow_unverified=allow_unverified,
            max_latency_ms=max_latency_ms,
            log=log,
        )
    except ProxyPoolError as exc:
        message = str(exc)
        if "HTTP-проверку" in message and not allow_unverified:
            message += " (можно попробовать --allow-unverified-proxy)"
        raise ExtractorError(message) from exc
    if not probes:
        raise ExtractorError("не осталось рабочих proxy")
    best = probes[0]
    best_ms = best.http_ms if best.http_ms is not None else best.tcp_ms
    log(
        f"[*] рабочих proxy: {len(probes)}; лучший: {describe_proxy(best.proxy)} ({best_ms:.0f} ms)"
    )
    return probes


def _pick_proxy(
    probes: Sequence[ProxyProbe],
    used_keys: set[str],
) -> ProxyProbe:
    try:
        return choose_proxy(probes, used_keys)
    except ProxyPoolError as exc:
        raise ExtractorError(str(exc)) from exc


def _proxy_check_options(args: argparse.Namespace) -> dict[str, Any]:
    """Map CLI proxy flags to the proxy-pool API in one place."""
    return {
        "test_url": getattr(args, "proxy_test_url", DEFAULT_PROXY_TEST_URL),
        "timeout": float(getattr(args, "proxy_timeout", 8.0)),
        "workers": int(getattr(args, "proxy_workers", 24)),
        "top_n": int(getattr(args, "proxy_top", 0)),
        "allow_unverified": bool(getattr(args, "allow_unverified_proxy", False)),
        "max_latency_ms": float(getattr(args, "max_proxy_latency", 3.0)) * 1000.0,
    }


def _rank_from_args(
    pool: str | Path,
    args: argparse.Namespace,
    *,
    exclude: set[str] | None = None,
) -> list[ProxyProbe]:
    return _rank_pool(pool, exclude=exclude, **_proxy_check_options(args))


def _scan_from_args(
    candidates: Sequence[str],
    args: argparse.Namespace,
    *,
    need: int,
    known: Sequence[ProxyProbe] = (),
    exclude: set[str] = frozenset(),
) -> list[ProxyProbe]:
    options = _proxy_check_options(args)
    return scan_until(
        candidates,
        need=need,
        known=known,
        exclude=exclude,
        batch_size=int(getattr(args, "batch_size", 64)),
        test_url=options["test_url"],
        timeout=options["timeout"],
        workers=options["workers"],
        max_latency_ms=options["max_latency_ms"],
        log=print,
    )


def _grow_proxy_pool(
    probes: list[ProxyProbe],
    args: argparse.Namespace,
    *,
    pool: str | Path,
    auto_pool: str | Path | None,
    need: int,
    used: set[str],
    bad_keys: set[str],
) -> tuple[list[ProxyProbe], bool]:
    """Top up the working pool from the large pool when it is too small.

    Returns the unchanged pool when no second source exists. Capacity errors
    stay with the caller so each command can explain what is missing.
    """
    available = [probe for probe in probes if probe.key not in used]
    if len(available) >= need or not auto_pool:
        return probes, False
    if Path(auto_pool).resolve() == Path(pool).resolve():
        return probes, False

    shortfall = need - len(available)
    print(
        f"[!] свободных proxy {len(available)}/{need}; "
        f"продолжаю искать ещё {shortfall} в {auto_pool} ...",
        flush=True,
    )
    try:
        candidates = load_proxy_pool(auto_pool)
        probes = _scan_from_args(
            candidates,
            args,
            known=probes,
            exclude=used | bad_keys,
            need=len(probes) + shortfall,
        )
    except ProxyPoolError as exc:
        raise ExtractorError(f"не удалось добрать proxy: {exc}") from exc
    available = [probe for probe in probes if probe.key not in used]
    print(f"[+] теперь рабочих proxy: {len(probes)}, свободных: {len(available)}", flush=True)
    return probes, True


def _load_bad_proxy_keys() -> set[str]:
    path = DATA_INPUT / "proxies-bad.txt"
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        label = line.split("\t", 1)[0].strip()
        if not label:
            continue
        try:
            keys.add(proxy_key(label))
        except Exception:
            continue
    return keys


def _is_dynamic_proxy_file(path: Path) -> bool:
    return path.name in ("proxies-working.txt", "proxies-good.txt")


def _safe_proxy_label(proxy: str) -> str:
    try:
        parsed = urlsplit(proxy)
        if parsed.hostname and parsed.port:
            auth = f"{parsed.username}@" if parsed.username else ""
            return f"{parsed.scheme}://{auth}{parsed.hostname}:{parsed.port}"
    except Exception as exc:
        log.debug("could not format proxy label: %s", exc)
    return "<proxy>"


def _prune_proxy_file(path: Path, live_keys: set[str]) -> None:
    if not path.exists() or not _is_dynamic_proxy_file(path):
        return
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            kept.append(line)
            continue
        try:
            key = proxy_key(text)
        except Exception:
            continue
        if key in live_keys:
            kept.append(text)
    path.write_text("".join(item + "\n" for item in kept), encoding="utf-8", newline="\n")


def _remove_proxy_from_file(path: Path, proxy: str) -> None:
    if not path.exists() or not _is_dynamic_proxy_file(path):
        return
    try:
        target = proxy_key(proxy)
    except Exception:
        return
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            kept.append(line)
            continue
        try:
            key = proxy_key(text)
        except Exception:
            continue
        if key != target:
            kept.append(text)
    path.write_text("".join(item + "\n" for item in kept), encoding="utf-8", newline="\n")


def _mark_proxy_bad(pool: str | Path | None, proxy: str, error: str) -> None:
    targets = [Path(pool)] if pool else []
    targets += [DATA_INPUT / "proxies-working.txt", DATA_INPUT / "proxies-good.txt"]
    seen: set[Path] = set()
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        _remove_proxy_from_file(target, proxy)
    error_lower = error.lower()
    if "cloudflare" in error_lower:
        reason = "cloudflare"
    elif "медлен" in error_lower or "slow" in error_lower:
        reason = "slow"
    else:
        reason = "proxy_error"
    bad_file = DATA_INPUT / "proxies-bad.txt"
    with bad_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{_safe_proxy_label(proxy)}\t{reason}\n")


def _is_proxy_failure(error: str) -> bool:
    text = error.lower()
    return any(
        marker in text
        for marker in (
            "cloudflare",
            "proxy",
            "socks",
            "connect",
            "tunnel",
            "econnrefused",
            "timed out",
        )
    )


def _used_proxy_keys(store: CookieStore) -> set[str]:
    return {
        _proxy_key_or_raw(str(entry.get("proxy") or ""))
        for entry in store.entries
        if entry.get("proxy")
    }


def _select_pool_proxy(store: CookieStore, args: argparse.Namespace) -> str:
    pool = getattr(args, "proxy_pool", None)
    if not pool:
        raise ExtractorError("не указан --proxy-pool")
    probes = _rank_from_args(pool, args, exclude=_load_bad_proxy_keys())
    _prune_proxy_file(Path(pool), {probe.key for probe in probes})
    selected = _pick_proxy(probes, _used_proxy_keys(store))
    print(
        f"[*] выбран proxy: {describe_proxy(selected.proxy)} "
        f"({(selected.http_ms or selected.tcp_ms):.0f} ms)",
        flush=True,
    )
    return selected.proxy


def _write_working_proxies(path: Path, probes: Sequence[ProxyProbe]) -> None:
    seen: set[str] = set()
    lines: list[str] = []
    for probe in probes:
        host = (urlsplit(probe.proxy).hostname or probe.proxy).lower()
        if host in seen:
            continue
        seen.add(host)
        lines.append(probe.proxy + "\n")
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #


def truncate(value: Any, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def print_accounts(store: CookieStore, *, log: Log = print) -> None:
    if not store.entries:
        if not store.path.exists():
            log(f"Файл ещё не создан: {store.path}")
            log("Сначала добавь аккаунт через add, запись появится после успешного входа.")
        else:
            log(f"Список пуст: {store.path}")
        return
    log(f"Файл: {store.path}  (аккаунтов: {len(store.entries)})")
    headers = ("LABEL", "EMAIL", "COOKIES", "SSID", "PROXY", "UPDATED")
    rows = []
    for entry in store.entries:
        cookies = entry.get("cookies") if isinstance(entry.get("cookies"), list) else []
        ssid = session_cookie_info(cookies)
        rows.append(
            (
                truncate(entry.get("label", ""), 22),
                truncate(entry.get("new_email", ""), 28),
                str(len(cookies)),
                format_expiry(ssid),
                truncate(describe_proxy(entry.get("proxy", "")), 32),
                truncate(str(entry.get("updated_at", ""))[:16].replace("T", " "), 16),
            )
        )
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    log("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    log("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        log("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    log("")
    log("Cookie и proxy в выводе не раскрываются. Полные данные — в файле.")


# --------------------------------------------------------------------------- #
# Interactive commands
# --------------------------------------------------------------------------- #


def prompt(text: str, *, default: str | None = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{text}{suffix}: ").strip()
        except EOFError:
            if required:
                raise ExtractorError("ввод закрыт, а обязательное значение не указано") from None
            return default if default is not None else ""
        if answer:
            return answer
        if default:
            return default
        if not required:
            return default if default is not None else ""
        print("  значение обязательно.")


def confirm(text: str, *, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        answer = input(f"{text}{suffix}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def _collect_account(
    store: CookieStore,
    *,
    label: str,
    email: str,
    proxy: str,
    external_id: str | None,
    login_url: str,
    root_domain: str,
    session_cookie: str,
    timeout: int,
    headless: bool,
    max_page_load: float = 30.0,
    log: Log = print,
) -> dict[str, Any]:
    label = clean_label(label)
    existing = store.find(label)
    if existing is not None:
        label = str(existing.get("label") or label)
        if not external_id:
            external_id = str(existing.get("external_id") or "") or None
    email = validate_email(email)
    for other in store.entries:
        if other is existing:
            continue
        if str(other.get("new_email") or "").strip().casefold() == email.casefold():
            raise ExtractorError(
                f"email {email!r} уже используется аккаунтом {other.get('label')!r}"
            )
    proxy = clean_proxy(proxy)
    if external_id:
        external_id = validate_external_id(external_id)
        for other in store.entries:
            if other is existing:
                continue
            if str(other.get("external_id", "")).casefold() == external_id.casefold():
                raise ExtractorError(
                    f"external_id {external_id!r} уже занят аккаунтом {other.get('label')!r}"
                )

    if not proxy:
        log("[!] proxy не указан — запись сохранится, но export для сервера потребует proxy.")

    log("")
    log("=" * 62)
    log(f"  label: {label}")
    log(f"  email: {email}")
    log(f"  proxy: {describe_proxy(proxy)}")
    log("=" * 62)

    cookies, _info = capture_account(
        login_url=login_url,
        root_domain=root_domain,
        session_cookie=session_cookie,
        proxy=proxy,
        headless=headless,
        timeout=timeout,
        max_page_load=max_page_load,
        log=log,
    )
    record, created = store.upsert(
        {
            "external_id": external_id or "",
            "label": label,
            "new_email": email,
            "proxy": proxy,
            "cookies": cookies,
            "headless": True,
        }
    )
    ssid = session_cookie_info(cookies, session_cookie)
    log("")
    log(f"[+] {'добавлен' if created else 'обновлён'} аккаунт {record['label']!r}")
    log(f"    external_id: {record['external_id']}")
    log(f"    cookie: {len(cookies)}, ssid: {format_expiry(ssid)}")
    log(f"    файл: {store.path.resolve()}")
    log("    cookie — это доступ к аккаунту; не публикуйте файл.")
    return record


def _resolve_account_proxy(
    store: CookieStore,
    existing: dict[str, Any] | None,
    args: argparse.Namespace,
    *,
    use_env_default: bool = False,
) -> str:
    """Apply the shared CLI proxy precedence rules for one account.

    Explicit flag wins over the existing value, then the pool, then a prompt.
    """
    if getattr(args, "no_proxy", False):
        return ""
    if getattr(args, "proxy", None) is not None:
        return str(args.proxy)
    current = str((existing or {}).get("proxy") or "")
    if current and not getattr(args, "new_proxy", False):
        return current
    if getattr(args, "proxy_pool", None):
        return _select_pool_proxy(store, args)
    fallback = current or (os.environ.get("PROXY", "") if use_env_default else "")
    return prompt("Proxy (Enter — без proxy)", default=fallback)


def _print_batch_slot(index: int, count: int, label: str, email: str, proxy_note: str) -> None:
    print()
    print("=" * 62)
    print(f"  [{index + 1}/{count}] {label}")
    print(f"  email: {email}")
    print(f"  proxy: {proxy_note}")
    print("=" * 62)


def _run_batch(
    store: CookieStore,
    args: argparse.Namespace,
    *,
    count: int,
    emails: Sequence[str],
    proxy_for: Callable[[], tuple[str, str]],
    max_page_load: float,
    on_error: Callable[[str, ExtractorError], None] | None = None,
    on_success: Callable[[str], None] | None = None,
) -> None:
    """Collect one account at a time while keeping proxy bookkeeping outside."""
    for index in range(count):
        label = next_label(store, str(getattr(args, "label_prefix", "acc") or "acc"))
        email = (
            emails[index] if index < len(emails) else prompt(f"Почта для {label}", required=True)
        )
        proxy, proxy_note = proxy_for()
        _print_batch_slot(index, count, label, email, proxy_note)
        try:
            _collect_account(
                store,
                label=label,
                email=email,
                proxy=proxy,
                external_id=None,
                login_url=args.url,
                root_domain=args.domain,
                session_cookie=args.session_cookie,
                timeout=args.timeout,
                headless=args.headless,
                max_page_load=max_page_load,
            )
        except ExtractorError as exc:
            if on_error is not None:
                on_error(proxy, exc)
            raise
        if on_success is not None:
            on_success(proxy)
    print(f"[+] пакет завершён: {count} аккаунтов")


def cmd_add(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    auto_label = bool(getattr(args, "auto_label", False)) or (
        bool(getattr(args, "proxy_pool", None)) and not args.label
    )
    prefix = str(getattr(args, "label_prefix", "acc") or "acc")
    label = (
        next_label(store, prefix)
        if auto_label
        else (
            args.label
            or prompt("Название аккаунта (Enter — auto)", default=next_label(store, prefix))
        )
    )
    existing = store.find(label)
    if existing is not None:
        print(f"[*] {label!r} уже есть — cookie будут обновлены.", flush=True)
    default_email = str(existing.get("new_email") or "") if existing else ""
    email = args.email or prompt("Новая почта для смены", default=default_email, required=True)

    proxy = _resolve_account_proxy(store, existing, args, use_env_default=True)

    _collect_account(
        store,
        label=label,
        email=email,
        proxy=proxy,
        external_id=args.external_id,
        login_url=args.url,
        root_domain=args.domain,
        session_cookie=args.session_cookie,
        timeout=args.timeout,
        headless=args.headless,
        max_page_load=float(getattr(args, "max_page_load", 30.0)),
    )
    return 0


def cmd_relogin(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    existing = store.find(args.target)
    if existing is None:
        raise ExtractorError(f"аккаунт {args.target!r} не найден в {store.path}")
    email = args.email or prompt(
        "Новая почта для смены", default=str(existing.get("new_email") or ""), required=True
    )
    proxy = _resolve_account_proxy(store, existing, args)
    _collect_account(
        store,
        label=str(existing.get("label") or args.target),
        email=email,
        proxy=proxy,
        external_id=str(existing.get("external_id") or "") or None,
        login_url=args.url,
        root_domain=args.domain,
        session_cookie=args.session_cookie,
        timeout=args.timeout,
        headless=args.headless,
        max_page_load=float(getattr(args, "max_page_load", 30.0)),
    )
    return 0


def cmd_proxy_scan(args: argparse.Namespace) -> int:
    pool = args.proxy_pool or default_proxy_pool()
    if not pool:
        raise ExtractorError("не указан --proxy-pool и нет proxies-top.txt/proxies.txt")
    candidates = load_proxy_pool(pool)
    top_path = DATA_INPUT / "proxies-top.txt"
    if top_path.exists() and top_path.resolve() != Path(pool).resolve():
        top = load_proxy_pool(top_path)
        top_keys = {proxy_key(item) for item in top}
        candidates = top + [item for item in candidates if proxy_key(item) not in top_keys]

    need = max(1, int(args.need))
    print(
        f"[*] ищу {need} рабочих proxy; батч {args.batch_size}, кандидатов {len(candidates)}",
        flush=True,
    )
    try:
        probes = _scan_from_args(
            candidates,
            args,
            need=need,
            exclude=_load_bad_proxy_keys(),
        )
    except ProxyPoolError as exc:
        raise ExtractorError(str(exc)) from exc

    out = Path(args.out)
    if out.resolve() == Path(pool).resolve():
        raise ExtractorError("--out не должен совпадать с --proxy-pool")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(f"{probe.proxy}\n" for probe in probes),
        encoding="utf-8",
        newline="\n",
    )
    print(f"[+] найдено {len(probes)} рабочих proxy -> {out.resolve()}")
    for probe in probes:
        ms = probe.http_ms if probe.http_ms is not None else probe.tcp_ms
        print(f"    {describe_proxy(probe.proxy)}  {ms:.0f} ms")
    return 0


def cmd_assign_proxies(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    targets = [entry for entry in store.entries if not str(entry.get("proxy") or "").strip()]
    if not targets:
        print("[*] у всех записей уже есть proxy")
        return 0
    pool = args.proxy_pool or default_proxy_pool()
    if not pool:
        raise ExtractorError("не указан --proxy-pool")
    bad_keys = _load_bad_proxy_keys()
    probes = _rank_from_args(pool, args, exclude=bad_keys)
    _prune_proxy_file(Path(pool), {probe.key for probe in probes})
    used = _used_proxy_keys(store)
    probes, expanded = _grow_proxy_pool(
        probes,
        args,
        pool=pool,
        auto_pool=getattr(args, "auto_proxy_pool", None),
        need=len(targets),
        used=used,
        bad_keys=bad_keys,
    )
    available = [probe for probe in probes if probe.key not in used]
    if len(available) < len(targets):
        raise ExtractorError(
            f"свободных proxy {len(available)}, а записей без proxy {len(targets)}"
        )
    for entry in targets:
        selected = _pick_proxy(probes, used)
        entry["proxy"] = selected.proxy
        used.add(selected.key)
        print(
            f"[+] {entry.get('label')}: {describe_proxy(selected.proxy)} "
            f"({(selected.http_ms or selected.tcp_ms):.0f} ms)",
            flush=True,
        )
    store.save()
    if expanded:
        _write_working_proxies(DATA_INPUT / "proxies-working.txt", probes)
    print(f"[+] proxy назначены {len(targets)} аккаунтам")
    return 0


def cmd_add_batch(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    no_proxy = bool(getattr(args, "no_proxy", False))
    pool = getattr(args, "proxy_pool", None)
    fixed_proxy = clean_proxy(getattr(args, "proxy", None))
    if fixed_proxy and no_proxy:
        raise ExtractorError("нельзя одновременно --proxy и --no-proxy")
    if not pool and not no_proxy and not fixed_proxy:
        raise ExtractorError("для add-batch нужен --proxy-pool FILE или --proxy")

    emails = load_emails(args.emails) if getattr(args, "emails", None) else []
    used_emails = {
        str(entry.get("new_email") or "").strip().casefold()
        for entry in store.entries
        if entry.get("new_email")
    }
    if used_emails:
        emails = [email for email in emails if email.casefold() not in used_emails]
    count = int(getattr(args, "count", 0) or 0) or len(emails)
    if count <= 0:
        raise ExtractorError("укажите --count N или --emails FILE")
    if emails and count > len(emails):
        raise ExtractorError(f"в --emails только {len(emails)} адресов, а --count={count}")

    page_load = float(getattr(args, "max_page_load", 30.0))
    if fixed_proxy:
        note = describe_proxy(fixed_proxy)
        print(f"[*] пакет: {count} аккаунтов; proxy: {note} (один на всех)", flush=True)
        _run_batch(
            store,
            args,
            count=count,
            emails=emails,
            proxy_for=lambda: (fixed_proxy, note),
            max_page_load=page_load,
        )
        return 0

    if no_proxy:
        print(f"[*] пакет без proxy: {count} аккаунтов", flush=True)
        _run_batch(
            store,
            args,
            count=count,
            emails=emails,
            proxy_for=lambda: ("", "direct"),
            max_page_load=0.0,
        )
        return 0

    bad_keys = _load_bad_proxy_keys()
    probes = _rank_from_args(pool, args, exclude=bad_keys)
    _prune_proxy_file(Path(pool), {probe.key for probe in probes})
    used = _used_proxy_keys(store)
    probes, expanded = _grow_proxy_pool(
        probes,
        args,
        pool=pool,
        auto_pool=getattr(args, "auto_proxy_pool", None),
        need=count,
        used=used,
        bad_keys=bad_keys,
    )
    available = [probe for probe in probes if probe.key not in used]
    if len(available) < count:
        raise ExtractorError(
            f"в пуле только {len(available)} свободных proxy, а нужно {count}; "
            "добавьте proxy или удалите лишние аккаунты"
        )
    if expanded:
        _write_working_proxies(DATA_INPUT / "proxies-working.txt", probes)

    print(
        f"[*] пакет: {count} аккаунтов; proxy в пуле: {len(probes)}; уже занято: {len(used)}",
        flush=True,
    )

    def proxy_for() -> tuple[str, str]:
        selected = _pick_proxy(probes, used)
        elapsed = selected.http_ms or selected.tcp_ms
        note = f"{describe_proxy(selected.proxy)} ({elapsed:.0f} ms)"
        return selected.proxy, note

    def on_error(proxy: str, exc: ExtractorError) -> None:
        if _is_proxy_failure(str(exc)):
            _mark_proxy_bad(pool, proxy, str(exc))
            print("[!] прокси удалён из рабочего списка", flush=True)

    def on_success(proxy: str) -> None:
        selected_key = next((probe.key for probe in probes if probe.proxy == proxy), "")
        used.add(selected_key or _proxy_key_or_raw(proxy))

    _run_batch(
        store,
        args,
        count=count,
        emails=emails,
        proxy_for=proxy_for,
        max_page_load=page_load,
        on_error=on_error,
        on_success=on_success,
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    print_accounts(CookieStore(args.store))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    entry = store.find(args.target)
    if entry is None:
        raise ExtractorError(f"аккаунт {args.target!r} не найден в {store.path}")
    if not args.yes and not confirm(f"Удалить {entry.get('label')!r}?", default=False):
        print("Отменено.")
        return 0
    store.remove(args.target)
    print(f"[+] удалён {entry.get('label')!r}; осталось {len(store.entries)}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = CookieStore(args.store)
    count, path = export_jobs(
        store,
        fmt=args.format,
        out=args.out,
        require_proxy=not args.allow_empty_proxy,
        skip_incomplete=args.skip_incomplete,
        root_domain=args.domain,
        session_cookie=args.session_cookie,
    )
    if path is not None:
        print(f"[+] экспортировано {count} job -> {path.resolve()}")
        print(
            "    отправка: python scripts/submit_accounts.py --url ... --api-key ... "
            f"--input {path}"
        )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def default_proxy_pool() -> Path | None:
    if DEFAULT_PROXY_POOL:
        return DEFAULT_PROXY_POOL
    for name in ("proxies-working.txt", "proxies-top.txt", "proxies.txt"):
        candidate = DATA_INPUT / name
        if candidate.exists():
            return candidate
    return None


def default_auto_proxy_pool() -> Path | None:
    for name in ("proxies-socks5-all.txt", "proxies.txt"):
        candidate = DATA_INPUT / name
        if candidate.exists():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (implementation lives in src.extractor.cli)."""
    from src.extractor.cli import build_parser as _build_parser

    return _build_parser()


def main(argv: list[str] | None = None) -> int:
    """Run the CLI (implementation lives in src.extractor.cli)."""
    if __name__ == "__main__":
        # The CLI module imports this file by its package name. Alias the
        # script module so it is not executed a second time.
        import sys

        sys.modules.setdefault("cookie_extractor", sys.modules[__name__])
    from src.extractor.cli import main as _main

    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
