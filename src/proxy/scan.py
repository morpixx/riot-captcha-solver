"""Дополнительно: постепенный поиск N рабочих proxy.

Не проверяет весь пул сразу: берёт батчи, делает TCP-пробу, затем HTTP-пробу
через proxy и останавливается, когда найдено достаточно рабочих.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from .pool import (
    DEFAULT_MAX_LATENCY_MS,
    DEFAULT_TEST_URL,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    ProxyPoolError,
    ProxyProbe,
    _http_probe,
    _tcp_probe,
    normalize_proxy,
    proxy_key,
)


def _host(proxy: str) -> str:
    try:
        return (urlsplit(proxy).hostname or proxy).lower()
    except Exception:
        return proxy.lower()


def scan_until(
    proxies: Iterable[str],
    *,
    need: int,
    known: Iterable[ProxyProbe] = (),
    exclude: Iterable[str] = (),
    unique_hosts: bool = True,
    batch_size: int = 64,
    test_url: str = DEFAULT_TEST_URL,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    max_latency_ms: float = DEFAULT_MAX_LATENCY_MS,
    log=None,
) -> list[ProxyProbe]:
    """Ищет рабочие proxy батчами, пока не наберёт need уникальных хостов."""
    need = max(1, int(need))
    batch_size = max(1, int(batch_size))
    timeout = max(1.0, float(timeout))
    workers = max(1, int(workers))

    collected: dict[str, ProxyProbe] = {}
    hosts: set[str] = set()
    for probe in known:
        if unique_hosts and _host(probe.proxy) in hosts:
            continue
        collected[probe.key] = probe
        hosts.add(_host(probe.proxy))

    excluded: set[str] = set()
    for item in exclude:
        try:
            excluded.add(proxy_key(item))
        except Exception:
            continue

    normalized: list[str] = []
    seen: set[str] = set(collected) | excluded
    for item in proxies:
        proxy = normalize_proxy(item)
        key = proxy_key(proxy)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(proxy)

    tested = 0
    total = len(normalized)
    for start in range(0, total, batch_size):
        if len(collected) >= need:
            break
        batch = normalized[start : start + batch_size]
        tested += len(batch)
        if log:
            log(
                f"[*] proxy {start + 1}-{start + len(batch)}/{total}; "
                f"рабочих {len(collected)}/{need}"
            )

        tcp_results: list[tuple[float, str, str]] = []
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as pool:
            futures = {pool.submit(_tcp_probe, proxy, min(3.0, timeout)): proxy for proxy in batch}
            for future in as_completed(futures):
                proxy = futures[future]
                try:
                    latency = future.result()
                except Exception:
                    latency = None
                if latency is not None:
                    tcp_results.append((latency, proxy, proxy_key(proxy)))

        if not tcp_results:
            continue
        tcp_results.sort(key=lambda item: item[0])

        with ThreadPoolExecutor(max_workers=min(workers, len(tcp_results))) as pool:
            futures = {
                pool.submit(_http_probe, proxy, test_url, timeout): (tcp_ms, proxy, key)
                for tcp_ms, proxy, key in tcp_results
            }
            for future in as_completed(futures):
                tcp_ms, proxy, key = futures[future]
                try:
                    http_ms, status, _error = future.result()
                except Exception:
                    http_ms = None
                if http_ms is None:
                    continue
                if max_latency_ms > 0 and http_ms > max_latency_ms:
                    continue
                host = _host(proxy)
                if unique_hosts and host in hosts:
                    continue
                hosts.add(host)
                collected[key] = ProxyProbe(
                    proxy=proxy,
                    key=key,
                    tcp_ms=tcp_ms,
                    http_ms=http_ms,
                    status=status,
                )

        if len(collected) >= need:
            break

    if not collected:
        raise ProxyPoolError("не нашёл ни одного рабочего proxy")
    result = sorted(collected.values(), key=lambda item: item.http_ms or 10**9)
    if len(result) < need:
        raise ProxyPoolError(
            f"нашёл только {len(result)} рабочих proxy из {tested} проверенных "
            f"(нужно {need}); добавьте ещё proxy или уменьшите --need"
        )
    return result[:need]
