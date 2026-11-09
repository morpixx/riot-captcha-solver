"""Пул прокси: загрузка, проверка задержки и выбор свободного прокси.

Используется cookie_extractor.py. Сначала меряем TCP-connect до самого прокси,
затем top-N проверяем реальным HTTPS-запросом через прокси. Это отсекает
прокси, которые принимают TCP, но не работают/требуют неверный пароль.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .proxy import describe, parse_proxy

DEFAULT_TEST_URL = "https://account.riotgames.com/"
DEFAULT_TIMEOUT = 8.0
DEFAULT_TOP = 0
DEFAULT_WORKERS = 24
DEFAULT_MAX_LATENCY_MS = 8000.0

Log = Callable[[str], None]


class ProxyPoolError(RuntimeError):
    """Ошибка загрузки/проверки пула прокси."""


@dataclass(frozen=True)
class ProxyProbe:
    proxy: str
    key: str
    tcp_ms: float
    http_ms: float | None
    status: int | None = None
    error: str | None = None


def normalize_proxy(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise ProxyPoolError("пустая строка прокси")
    if "://" not in raw:
        raw = "socks5://" + raw
    try:
        parse_proxy(raw)
    except ValueError as exc:
        raise ProxyPoolError(f"некорректный proxy: {exc}") from exc
    return raw


def proxy_key(value: str) -> str:
    """Ключ, чтобы один и тот же прокси не достался двум аккаунтам."""
    proxy = normalize_proxy(value)
    parsed = parse_proxy(proxy)
    server = str(parsed["server"]).lower()
    if server.startswith("socks5h://"):
        server = "socks5://" + server[len("socks5h://") :]
    user = str(parsed.get("username") or "").lower()
    return f"{server}|{user}"


def load_proxy_pool(path: str | Path) -> list[str]:
    pool_path = Path(path)
    if not pool_path.exists():
        raise ProxyPoolError(f"файл с прокси не найден: {pool_path}")
    try:
        text = pool_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProxyPoolError(f"не удалось прочитать {pool_path}: {exc}") from exc

    proxies: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = normalize_proxy(line)
            key = proxy_key(proxy)
        except ProxyPoolError as exc:
            raise ProxyPoolError(f"{pool_path.name}:{lineno}: {exc}") from exc
        if key in seen:
            continue
        seen.add(key)
        proxies.append(proxy)
    if not proxies:
        raise ProxyPoolError(f"в {pool_path} нет ни одного прокси")
    return proxies


def _mask_proxy(proxy: str) -> str:
    try:
        return describe(parse_proxy(proxy))
    except Exception:
        return "<proxy>"


def _requests_proxy_url(proxy: str) -> str:
    raw = normalize_proxy(proxy)
    if raw.lower().startswith("socks5://"):
        # socks5h = DNS через прокси, иначе requests может резолвить локально.
        raw = "socks5h://" + raw[len("socks5://") :]
    return raw


def _tcp_probe(proxy: str, timeout: float) -> float | None:
    parsed = parse_proxy(proxy)
    server = urlparse(parsed["server"])
    host, port = server.hostname, server.port
    if not host or not port:
        return None
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - started) * 1000.0
    except OSError:
        return None


def _http_probe(
    proxy: str, test_url: str, timeout: float
) -> tuple[float | None, int | None, str | None]:
    proxy_url = _requests_proxy_url(proxy)
    session = requests.Session()
    session.trust_env = False
    started = time.perf_counter()
    try:
        response = session.get(
            test_url,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=(min(3.0, timeout), timeout),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            allow_redirects=False,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:220]
        message = message.replace(proxy_url, _mask_proxy(proxy))
        return None, None, message
    elapsed = (time.perf_counter() - started) * 1000.0
    if response.status_code == 407:
        return None, response.status_code, "proxy authentication required (407)"
    return elapsed, response.status_code, None


def rank_proxies(
    proxies: Iterable[str],
    *,
    test_url: str = DEFAULT_TEST_URL,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    top_n: int = DEFAULT_TOP,
    allow_unverified: bool = False,
    max_latency_ms: float = DEFAULT_MAX_LATENCY_MS,
    log: Log | None = None,
) -> list[ProxyProbe]:
    """Возвращает рабочие прокси, отсортированные по HTTP-задержке."""
    timeout = max(1.0, float(timeout))
    workers = max(1, int(workers))
    top_n = int(top_n)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in proxies:
        proxy = normalize_proxy(item)
        key = proxy_key(proxy)
        if key not in seen:
            seen.add(key)
            normalized.append(proxy)
    if not normalized:
        raise ProxyPoolError("список прокси пуст")

    tcp_results: list[tuple[float, str, str]] = []
    with ThreadPoolExecutor(max_workers=min(max(1, workers), len(normalized))) as pool:
        futures = {pool.submit(_tcp_probe, proxy, min(3.0, timeout)): proxy for proxy in normalized}
        for future in as_completed(futures):
            proxy = futures[future]
            try:
                latency = future.result()
            except Exception:
                latency = None
            if latency is not None:
                tcp_results.append((latency, proxy, proxy_key(proxy)))

    if not tcp_results:
        raise ProxyPoolError("ни один прокси не принял TCP-соединение")
    tcp_results.sort(key=lambda item: item[0])

    if log:
        log(
            f"[*] TCP ответили {len(tcp_results)}/{len(normalized)} proxy; "
            f"лучший {tcp_results[0][0]:.0f} ms"
        )

    candidates = tcp_results[: max(1, top_n)] if top_n > 0 else tcp_results
    http_results: list[ProxyProbe] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(max(1, workers), len(candidates))) as pool:
        futures = {
            pool.submit(_http_probe, proxy, test_url, timeout): (tcp_ms, proxy, key)
            for tcp_ms, proxy, key in candidates
        }
        for future in as_completed(futures):
            tcp_ms, proxy, key = futures[future]
            try:
                http_ms, status, error = future.result()
            except Exception as exc:
                http_ms, status, error = None, None, f"{type(exc).__name__}: {exc}"[:220]
            if http_ms is None:
                if error:
                    errors.append(f"{_mask_proxy(proxy)}: {error}")
                continue
            if max_latency_ms > 0 and http_ms > max_latency_ms:
                errors.append(f"{_mask_proxy(proxy)}: slow {http_ms:.0f} ms")
                continue
            http_results.append(
                ProxyProbe(proxy=proxy, key=key, tcp_ms=tcp_ms, http_ms=http_ms, status=status)
            )

    if not http_results:
        if allow_unverified:
            if log:
                log("[!] HTTP-проверка не прошла ни у одного proxy; беру лучший по TCP.")
            return [
                ProxyProbe(proxy=proxy, key=key, tcp_ms=tcp_ms, http_ms=None, error="unverified")
                for tcp_ms, proxy, key in candidates
            ]
        detail = "; ".join(errors[:3]) if errors else "нет ответа"
        raise ProxyPoolError(f"ни один прокси не прошёл HTTP-проверку ({detail})")

    http_results.sort(key=lambda item: item.http_ms or 10**9)
    return http_results


def choose_proxy(probes: Iterable[ProxyProbe], used_keys: set[str]) -> ProxyProbe:
    for probe in probes:
        if probe.key not in used_keys:
            return probe
    raise ProxyPoolError("все прокси из пула уже заняты другими аккаунтами")
