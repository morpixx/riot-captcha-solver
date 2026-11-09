"""Implementations of the cookie-extractor commands.

Each command validates its arguments, performs one account/proxy operation and
returns a process exit code. The argparse layer lives in cli.py; proxy-file
bookkeeping lives in proxies.py.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..proxy.pool import ProxyPoolError, load_proxy_pool, proxy_key
from . import proxies
from .browser import capture_account
from .data import (
    CookieStore,
    ExtractorError,
    clean_label,
    clean_proxy,
    describe_proxy,
    export_jobs,
    format_expiry,
    load_emails,
    next_label,
    session_cookie_info,
    validate_email,
    validate_external_id,
)
from .ui import confirm, print_accounts, prompt

Log = Callable[[str], None]


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
    args: Any,
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
        return proxies._select_pool_proxy(store, args)
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
    args: Any,
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


def cmd_add(args: Any) -> int:
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


def cmd_relogin(args: Any) -> int:
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


def cmd_proxy_scan(args: Any) -> int:
    pool = args.proxy_pool or proxies.default_proxy_pool()
    if not pool:
        raise ExtractorError("не указан --proxy-pool и нет proxies-top.txt/proxies.txt")
    candidates = load_proxy_pool(pool)
    top_path = proxies.proxy_top_pool()
    if top_path and top_path.resolve() != Path(pool).resolve():
        top = load_proxy_pool(top_path)
        top_keys = {proxy_key(item) for item in top}
        candidates = top + [item for item in candidates if proxy_key(item) not in top_keys]

    need = max(1, int(args.need))
    print(
        f"[*] ищу {need} рабочих proxy; батч {args.batch_size}, кандидатов {len(candidates)}",
        flush=True,
    )
    try:
        probes = proxies._scan_from_args(
            candidates,
            args,
            need=need,
            exclude=proxies._load_bad_proxy_keys(),
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


def cmd_assign_proxies(args: Any) -> int:
    store = CookieStore(args.store)
    targets = [entry for entry in store.entries if not str(entry.get("proxy") or "").strip()]
    if not targets:
        print("[*] у всех записей уже есть proxy")
        return 0
    pool = args.proxy_pool or proxies.default_proxy_pool()
    if not pool:
        raise ExtractorError("не указан --proxy-pool")
    bad_keys = proxies._load_bad_proxy_keys()
    probes = proxies._rank_from_args(pool, args, exclude=bad_keys)
    proxies._prune_proxy_file(Path(pool), {probe.key for probe in probes})
    used = proxies._used_proxy_keys(store)
    probes, expanded = proxies._grow_proxy_pool(
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
        selected = proxies._pick_proxy(probes, used)
        entry["proxy"] = selected.proxy
        used.add(selected.key)
        print(
            f"[+] {entry.get('label')}: {describe_proxy(selected.proxy)} "
            f"({(selected.http_ms or selected.tcp_ms):.0f} ms)",
            flush=True,
        )
    store.save()
    if expanded:
        proxies._write_working_proxies(probes)
    print(f"[+] proxy назначены {len(targets)} аккаунтам")
    return 0


def cmd_add_batch(args: Any) -> int:
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

    bad_keys = proxies._load_bad_proxy_keys()
    probes = proxies._rank_from_args(pool, args, exclude=bad_keys)
    proxies._prune_proxy_file(Path(pool), {probe.key for probe in probes})
    used = proxies._used_proxy_keys(store)
    probes, expanded = proxies._grow_proxy_pool(
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
        proxies._write_working_proxies(probes)

    print(
        f"[*] пакет: {count} аккаунтов; proxy в пуле: {len(probes)}; уже занято: {len(used)}",
        flush=True,
    )

    def proxy_for() -> tuple[str, str]:
        selected = proxies._pick_proxy(probes, used)
        elapsed = selected.http_ms or selected.tcp_ms
        note = f"{describe_proxy(selected.proxy)} ({elapsed:.0f} ms)"
        return selected.proxy, note

    def on_error(proxy: str, exc: ExtractorError) -> None:
        if proxies._is_proxy_failure(str(exc)):
            proxies._mark_proxy_bad(pool, proxy, str(exc))
            print("[!] прокси удалён из рабочего списка", flush=True)

    def on_success(proxy: str) -> None:
        selected_key = next((probe.key for probe in probes if probe.proxy == proxy), "")
        used.add(selected_key or proxies._proxy_key_or_raw(proxy))

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


def cmd_list(args: Any) -> int:
    print_accounts(CookieStore(args.store))
    return 0


def cmd_remove(args: Any) -> int:
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


def cmd_export(args: Any) -> int:
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
