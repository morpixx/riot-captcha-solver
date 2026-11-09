"""Proxy pool selection and proxy-file bookkeeping for the extractor.

This module knows how to rank candidates, pick a free proxy, and keep
data/input/proxies-*.txt consistent. It knows nothing about accounts, the
browser, or the CLI parser.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import DATA_INPUT
from ..proxy.pool import (
    ProxyPoolError,
    ProxyProbe,
    choose_proxy,
    load_proxy_pool,
    proxy_key,
    rank_proxies,
)
from ..proxy.scan import scan_until
from .data import ExtractorError, describe_proxy

log = logging.getLogger("riot.extractor")

DEFAULT_PROXY_TEST_URL = os.environ.get("PROXY_TEST_URL", "https://account.riotgames.com/")
DEFAULT_PROXY_POOL = Path(os.environ["PROXY_POOL"]) if os.environ.get("PROXY_POOL") else None

Log = Callable[[str], None]


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


def _pick_proxy(probes: Sequence[ProxyProbe], used_keys: set[str]) -> ProxyProbe:
    try:
        return choose_proxy(probes, used_keys)
    except ProxyPoolError as exc:
        raise ExtractorError(str(exc)) from exc


def _proxy_check_options(args: Any) -> dict[str, Any]:
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
    args: Any,
    *,
    exclude: set[str] | None = None,
) -> list[ProxyProbe]:
    return _rank_pool(pool, exclude=exclude, **_proxy_check_options(args))


def _scan_from_args(
    candidates: Sequence[str],
    args: Any,
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
    args: Any,
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


def _used_proxy_keys(store: Any) -> set[str]:
    return {
        _proxy_key_or_raw(str(entry.get("proxy") or ""))
        for entry in store.entries
        if entry.get("proxy")
    }


def _select_pool_proxy(store: Any, args: Any) -> str:
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


def _write_working_proxies(probes: Sequence[ProxyProbe]) -> None:
    path = DATA_INPUT / "proxies-working.txt"
    seen: set[str] = set()
    lines: list[str] = []
    for probe in probes:
        host = (urlsplit(probe.proxy).hostname or probe.proxy).lower()
        if host in seen:
            continue
        seen.add(host)
        lines.append(probe.proxy + "\n")
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def default_proxy_pool() -> Path | None:
    if DEFAULT_PROXY_POOL:
        return DEFAULT_PROXY_POOL
    for name in (
        "proxies-working.txt",
        "proxies-top.txt",
        "proxies.txt",
        "proxies-ports-fast.txt",
        "proxies-ports-test.txt",
        "proxies-test.txt",
    ):
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


def proxy_top_pool() -> Path | None:
    """Optional higher-priority pool checked before the main candidates."""
    candidate = DATA_INPUT / "proxies-top.txt"
    return candidate if candidate.exists() else None
