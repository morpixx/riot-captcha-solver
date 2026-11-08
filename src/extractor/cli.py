"""Command-line parser and interactive menu for cookie_extractor.

Kept separate from the command implementations so the top-level script stays
focused on account/proxy work. Importing this module is safe: it uses the
fully initialized cookie_extractor module.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cookie_extractor import (
    DEFAULT_DOMAIN,
    DEFAULT_LOGIN_URL,
    DEFAULT_PROXY_TEST_URL,
    DEFAULT_SESSION_COOKIE,
    DEFAULT_STORE,
    DEFAULT_TIMEOUT,
    CookieStore,
    ExtractorError,
    _collect_account,
    _resolve_account_proxy,
    cmd_add,
    cmd_add_batch,
    cmd_assign_proxies,
    cmd_export,
    cmd_list,
    cmd_proxy_scan,
    cmd_relogin,
    cmd_remove,
    default_auto_proxy_pool,
    default_proxy_pool,
    next_label,
    print_accounts,
    prompt,
)
from src.config import DATA_INPUT


def _add_proxy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--proxy-pool",
        type=Path,
        default=default_proxy_pool(),
        help="файл: один proxy на строку; выбирается самый быстрый свободный",
    )
    parser.add_argument(
        "--proxy-test-url",
        default=DEFAULT_PROXY_TEST_URL,
        help="URL для проверки proxy",
    )
    parser.add_argument("--proxy-timeout", type=float, default=8.0)
    parser.add_argument(
        "--max-proxy-latency",
        type=float,
        default=3.0,
        help="удалять proxy, если проверка грузится дольше N секунд (0 = без лимита)",
    )
    parser.add_argument("--proxy-workers", type=int, default=24, help=argparse.SUPPRESS)
    parser.add_argument(
        "--proxy-top",
        type=int,
        default=0,
        help="сколько лучших по TCP proxy проверять по HTTP (0 = все)",
    )
    parser.add_argument(
        "--allow-unverified-proxy",
        action="store_true",
        help="если HTTP-проверка не прошла, брать лучший по TCP",
    )


def _add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="секунд ждать вход (900; 0 = без таймаута)",
    )
    parser.add_argument("--url", default=DEFAULT_LOGIN_URL, help="страница входа")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="корневой домен cookie")
    parser.add_argument(
        "--session-cookie", default=DEFAULT_SESSION_COOKIE, help="имя cookie сессии"
    )
    parser.add_argument(
        "--max-page-load",
        type=float,
        default=30.0,
        help="считать proxy медленным, если страница входа грузится дольше N секунд (0 = без лимита)",
    )
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cookie_extractor.py",
        description="Собирает cookie нескольких аккаунтов Riot в один accounts.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Без аргументов открывается интерактивное меню.\n"
            "Формат accounts.jsonl совместим с scripts/submit_accounts.py."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="добавить аккаунт: открыть браузер и войти")
    add.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    add.add_argument("--label", help="название аккаунта; без него будет acc-NN")
    add.add_argument("--auto-label", action="store_true", help="взять следующий свободный acc-NN")
    add.add_argument("--label-prefix", default="acc", help="префикс для auto-label (acc)")
    add.add_argument("--email", help="новая почта для смены")
    add.add_argument("--proxy", default=None, help="конкретный proxy; отменяет --proxy-pool")
    add.add_argument(
        "--no-proxy", action="store_true", help="работать без proxy (прямое подключение)"
    )
    add.add_argument(
        "--new-proxy", action="store_true", help="перевыбрать proxy даже для существующего label"
    )
    add.add_argument("--external-id", help="стабильный id для идемпотентности")
    _add_proxy_args(add)
    _add_browser_args(add)
    add.set_defaults(func=cmd_add)

    relogin = sub.add_parser("relogin", help="заново войти в уже сохранённый аккаунт")
    relogin.add_argument("target", help="label или external_id")
    relogin.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    relogin.add_argument("--email", help="новая почта (по умолчанию из записи)")
    relogin.add_argument("--proxy", default=None, help="proxy (по умолчанию из записи)")
    relogin.add_argument("--no-proxy", action="store_true", help="работать без proxy")
    relogin.add_argument("--new-proxy", action="store_true", help="выбрать новый proxy из пула")
    _add_proxy_args(relogin)
    _add_browser_args(relogin)
    relogin.set_defaults(func=cmd_relogin)

    assign = sub.add_parser("assign-proxies", help="выдать proxy записям, сохранённым без proxy")
    assign.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    assign.add_argument(
        "--auto-proxy-pool",
        type=Path,
        default=default_auto_proxy_pool(),
        help="большой пул для автодобора",
    )
    _add_proxy_args(assign)
    assign.set_defaults(func=cmd_assign_proxies)

    scan = sub.add_parser("proxy-scan", help="найти N рабочих proxy и сохранить в файл")
    scan.add_argument(
        "--proxy-pool", type=Path, default=default_proxy_pool(), help="откуда брать кандидатов"
    )
    scan.add_argument("--need", type=int, default=16, help="сколько рабочих proxy нужно (16)")
    scan.add_argument(
        "--out", type=Path, default=DATA_INPUT / "proxies-working.txt", help="куда сохранить"
    )
    scan.add_argument("--batch-size", type=int, default=64, help="сколько proxy проверять за батч")
    scan.add_argument("--proxy-test-url", default=DEFAULT_PROXY_TEST_URL)
    scan.add_argument("--proxy-timeout", type=float, default=8.0)
    scan.add_argument("--max-proxy-latency", type=float, default=3.0)
    scan.add_argument("--proxy-workers", type=int, default=24, help=argparse.SUPPRESS)
    scan.set_defaults(func=cmd_proxy_scan)

    batch = sub.add_parser("add-batch", help="несколько аккаунтов подряд: auto label + proxy pool")
    batch.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    batch.add_argument("--emails", type=Path, help="файл: одна почта на строку")
    batch.add_argument(
        "--count", type=int, default=0, help="сколько аккаунтов (по умолчанию = строк в emails)"
    )
    batch.add_argument("--label-prefix", default="acc", help="префикс label (acc)")
    batch.add_argument(
        "--proxy",
        default=None,
        help="один proxy для всех аккаунтов пачки (отменяет --proxy-pool)",
    )
    batch.add_argument(
        "--no-proxy", action="store_true", help="все аккаунты без proxy (только для теста)"
    )
    batch.add_argument(
        "--auto-proxy-pool",
        type=Path,
        default=default_auto_proxy_pool(),
        help="большой пул для автодобора, если рабочих proxy не хватает",
    )
    _add_proxy_args(batch)
    _add_browser_args(batch)
    batch.set_defaults(func=cmd_add_batch)

    listing = sub.add_parser("list", help="показать аккаунты без раскрытия cookie")
    listing.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    listing.set_defaults(func=cmd_list)

    remove = sub.add_parser("remove", help="удалить аккаунт из списка")
    remove.add_argument("target", help="label или external_id")
    remove.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    remove.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    remove.set_defaults(func=cmd_remove)

    export = sub.add_parser("export", help="сделать JSON для отправки на сервер")
    export.add_argument("--store", type=Path, default=DEFAULT_STORE, help="общий JSONL-файл")
    export.add_argument("--format", choices=("jsonl", "json", "batch"), default="jsonl")
    export.add_argument("--out", help="куда писать; '-' = stdout")
    export.add_argument(
        "--skip-incomplete", action="store_true", help="пропустить записи без email/proxy/cookie"
    )
    export.add_argument("--allow-empty-proxy", action="store_true", help="не требовать proxy")
    export.add_argument("--domain", default=DEFAULT_DOMAIN, help="корневой домен cookie")
    export.add_argument(
        "--session-cookie", default=DEFAULT_SESSION_COOKIE, help="имя cookie сессии"
    )
    export.set_defaults(func=cmd_export)
    return parser


def _interactive_args(store: CookieStore, **overrides: Any) -> argparse.Namespace:
    """Browser/proxy defaults shared by the interactive menu actions."""
    values: dict[str, Any] = {
        "store": store.path,
        "url": DEFAULT_LOGIN_URL,
        "domain": DEFAULT_DOMAIN,
        "session_cookie": DEFAULT_SESSION_COOKIE,
        "timeout": DEFAULT_TIMEOUT,
        "headless": False,
        "proxy": None,
        "no_proxy": False,
        "new_proxy": False,
        "proxy_pool": default_proxy_pool(),
        "auto_proxy_pool": default_auto_proxy_pool(),
        "proxy_test_url": DEFAULT_PROXY_TEST_URL,
        "proxy_timeout": 8.0,
        "proxy_workers": 24,
        "proxy_top": 0,
        "max_proxy_latency": 3.0,
        "allow_unverified_proxy": False,
        "max_page_load": 30.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _interactive_add(store: CookieStore) -> None:
    label = prompt("Название аккаунта (Enter — auto)", default=next_label(store))
    existing = store.find(label)
    default_email = str(existing.get("new_email") or "") if existing else ""
    email = prompt("Новая почта для смены", default=default_email, required=True)
    args = _interactive_args(store)
    proxy = _resolve_account_proxy(store, existing, args, use_env_default=True)
    _collect_account(
        store,
        label=label,
        email=email,
        proxy=proxy,
        external_id=str(existing.get("external_id") or "") if existing else None,
        login_url=args.url,
        root_domain=args.domain,
        session_cookie=args.session_cookie,
        timeout=args.timeout,
        headless=args.headless,
        max_page_load=args.max_page_load,
    )


def _interactive_relogin(store: CookieStore) -> None:
    target = prompt("Label или external_id", required=True)
    cmd_relogin(_interactive_args(store, target=target))
    store.load()


def _interactive_export(store: CookieStore) -> None:
    fmt = prompt("Формат: jsonl/json/batch", default="jsonl")
    if fmt not in ("jsonl", "json", "batch"):
        raise ExtractorError("формат должен быть jsonl, json или batch")
    cmd_export(
        _interactive_args(
            store,
            format=fmt,
            out=None,
            skip_incomplete=False,
            allow_empty_proxy=False,
        )
    )


def _interactive_remove(store: CookieStore) -> None:
    target = prompt("Label или external_id", required=True)
    cmd_remove(_interactive_args(store, target=target, yes=False))
    store.load()


def _interactive_batch(store: CookieStore) -> None:
    pool = default_proxy_pool()
    pool_text = prompt(
        "Файл с proxy",
        default=str(pool) if pool else None,
        required=not pool,
    )
    emails_text = prompt("Файл с email (Enter — вводить вручную)", default="")
    count_text = prompt("Сколько аккаунтов (Enter = по числу email)", default="")
    args = _interactive_args(
        store,
        proxy_pool=Path(pool_text),
        emails=Path(emails_text) if emails_text else None,
        count=int(count_text) if count_text.isdigit() else 0,
        label_prefix=prompt("Префикс label", default="acc"),
    )
    cmd_add_batch(args)
    store.load()


def _interactive(store_path: Path) -> int:
    store = CookieStore(store_path)
    actions: dict[str, Callable[[CookieStore], None]] = {
        "1": _interactive_add,
        "2": _interactive_relogin,
        "3": lambda current: print_accounts(current),
        "4": _interactive_export,
        "5": _interactive_remove,
        "6": _interactive_batch,
    }
    while True:
        print()
        print("=" * 62)
        print("  Riot Cookie Extractor")
        print(f"  Список: {store.path}  (аккаунтов: {len(store.entries)})")
        print("=" * 62)
        print("  1) Добавить аккаунт (открыть браузер)")
        print("  2) Перелогинить аккаунт")
        print("  3) Показать список")
        print("  4) Экспорт для сервера")
        print("  5) Удалить аккаунт")
        print("  6) Добавить несколько подряд (auto label + proxy pool)")
        print("  0) Выход")
        try:
            choice = input("Выбор: ").strip()
        except EOFError:
            return 0
        print()
        if choice == "0" or choice.lower() in ("q", "exit", "quit"):
            return 0
        action = actions.get(choice)
        if action is None:
            print("Нет такого пункта.")
            continue
        try:
            action(store)
        except ExtractorError as exc:
            print(f"[!] {exc}")
        except KeyboardInterrupt:
            print("\nПрервано.")
            return 130


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        try:
            return _interactive(DEFAULT_STORE)
        except ExtractorError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"[!] ошибка файла: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nПрервано.")
            return 130
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except ExtractorError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[!] ошибка файла: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
