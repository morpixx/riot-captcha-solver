"""Interactive prompts and console tables for cookie_extractor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .data import (
    CookieStore,
    ExtractorError,
    describe_proxy,
    format_expiry,
    session_cookie_info,
)

Log = Callable[[str], None]


def truncate(value: Any, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def print_accounts(store: CookieStore, *, log: Log = print) -> None:
    """Print the account table without exposing cookie values or proxy creds."""
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
