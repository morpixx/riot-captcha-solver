r"""Camoufox browser backend.

Camoufox is the Firefox-based anti-detect browser used by the project.  It
runs headless, generates a real Windows fingerprint and resolves timezone,
locale and geolocation through the exit proxy before the first page loads.
There is no code path that disables fingerprinting or GeoIP.

Resource usage is kept low with the tuning ported from the Node Camoufox booster (see
camoufox_boost.py): Firefox prefs for helper processes and caches, network
blocking for tracking/media traffic, and humanized cursor movement.

Every launch gets a fresh temporary profile under the system temp directory
and deletes it afterwards.  The Camoufox binary and GeoIP database stay in
Camoufox's per-user cache (%LOCALAPPDATA%\camoufox on Windows), outside the
project.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psutil
import requests

from ..config import env_flag
from ..console import configure_utf8_stdio
from .camoufox_boost import camoufox_options, install_resource_blocking

log = logging.getLogger("riot.browser.camoufox")

# Kept as an alias for callers that imported the old private helper.
_ensure_utf8_stdio = configure_utf8_stdio

IP_SERVICES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)

# Sticky proxies keep the same exit IP for minutes. Re-probing it for every
# job costs up to four HTTPS round-trips through the proxy; cache it briefly.
_GEOIP_CACHE: dict[str, tuple[float, str]] = {}
_GEOIP_LOCK = threading.Lock()


def _proxy_fingerprint(proxy: dict) -> str:
    raw = "|".join(
        str(proxy.get(field) or "") for field in ("server", "username", "password")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _geoip_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("GEOIP_CACHE_TTL", "120")))
    except ValueError:
        return 120.0


def _proxy_url(proxy: dict | None) -> str | None:
    """Build a proxy URL with percent-encoded credentials for requests.

    Playwright/Camoufox receives the structured dict; only the GeoIP probe
    needs a URL, and it must not break on @, : or / inside the password.
    The returned value must never be logged.
    """
    if not proxy:
        return None
    server = str(proxy.get("server") or "").strip()
    if not server:
        raise ValueError("proxy.server is empty")
    username = proxy.get("username")
    password = proxy.get("password")
    if username is None and password is None:
        return server

    scheme, sep, rest = server.partition("://")
    if not sep:
        raise ValueError("proxy.server must include a scheme")
    user = quote(str(username or ""), safe="")
    secret = quote(str(password or ""), safe="")
    return f"{scheme}://{user}:{secret}@{rest}"


def _resolve_exit_ip(proxy: dict | None, timeout: float = 10.0) -> str | None:
    """Resolve the exit IP through the proxy (or the host) for GeoIP.

    Camoufox needs an IP to align timezone, locale and geolocation.  Resolving
    it here means a dead proxy fails with a clear message before a browser is
    started, and credentials with special characters are encoded correctly.
    """
    key = _proxy_fingerprint(proxy) if proxy else None
    ttl = _geoip_ttl()
    if key and ttl > 0:
        with _GEOIP_LOCK:
            cached = _GEOIP_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]

    proxy_url = _proxy_url(proxy) if proxy else None
    with requests.Session() as session:
        # Do not let HTTP_PROXY/HTTPS_PROXY from the host decide the exit IP.
        session.trust_env = False
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
        # Bound the whole probe, not every service: a dead proxy used to cost
        # up to 4 x 10 s before the job failed.
        deadline = time.monotonic() + max(1.0, timeout)
        for service in IP_SERVICES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = session.get(service, timeout=min(4.0, remaining))
                if response.status_code != 200:
                    continue
                candidate = response.text.strip()
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if key and ttl > 0:
                    with _GEOIP_LOCK:
                        _GEOIP_CACHE[key] = (time.monotonic(), candidate)
                return candidate
            except requests.RequestException:
                continue
    return None


def _headless_mode(value: bool | str) -> bool | str:
    """Map the project's headed/headless/virtual switch to Camoufox.

    Camoufox can only run the virtual display on Linux (Xvfb); on Windows and
    macOS a virtual display is replaced by normal headless mode.
    """
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "virtual":
            return "virtual" if sys.platform.startswith("linux") else True
        return normalized in {"1", "true", "yes", "on"}
    return bool(value)


# --------------------------------------------------------------------------- #
# Memory accounting (RSS / private bytes for the whole browser process tree).
# --------------------------------------------------------------------------- #
def _find_browser_roots(profile: Path) -> list[psutil.Process]:
    """Find the Camoufox root processes for one profile directory.

    Content processes do not repeat the profile path in their command line,
    so they are collected later as children of these roots.
    """
    marker = os.path.normcase(str(profile))
    roots: dict[int, psutil.Process] = {}
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = str(process.info.get("name") or "").lower()
            if "camoufox" not in name:
                continue
            cmdline = os.path.normcase(" ".join(process.info.get("cmdline") or []))
            if marker not in cmdline:
                continue
            roots[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return list(roots.values())


def _process_tree_memory(roots: list[psutil.Process]) -> dict[str, float | int] | None:
    processes: dict[int, psutil.Process] = {}
    for root in roots:
        try:
            tree = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            tree = [root]
        for process in tree:
            processes[process.pid] = process
    if not processes:
        return None
    rss = 0
    private = 0
    count = 0
    for process in processes.values():
        try:
            info = process.memory_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rss += int(getattr(info, "rss", 0) or 0)
        private += int(getattr(info, "private", 0) or getattr(info, "vms", 0) or 0)
        count += 1
    if not count:
        return None
    return {
        "rss_mb": round(rss / (1024 * 1024), 1),
        "private_mb": round(private / (1024 * 1024), 1),
        "processes": count,
    }


class _MemoryMonitor:
    """Logs launch and peak memory for one Camoufox session."""

    def __init__(self, profile: Path, label: str, interval: float = 1.0):
        self.profile = profile
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peak: dict[str, float | int] = {}
        self._samples = 0
        self._roots = _find_browser_roots(profile)

    def start(self) -> None:
        if not self._roots:
            time.sleep(0.3)
            self._roots = _find_browser_roots(self.profile)
        self._sample()
        if self._peak:
            log.info(
                "[camoufox] account=%s launch rss=%.1fMB private=%.1fMB procs=%s",
                self.label,
                self._peak["rss_mb"],
                self._peak["private_mb"],
                self._peak["processes"],
            )
        self._thread = threading.Thread(
            target=self._run, name=f"camoufox-mem-{self.label}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self) -> None:
        snapshot = _process_tree_memory(self._roots)
        if not snapshot:
            return
        self._samples += 1
        for key in ("rss_mb", "private_mb", "processes"):
            self._peak[key] = max(self._peak.get(key, 0), snapshot[key])

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._sample()
        if self._peak:
            log.info(
                "[camoufox] account=%s peak rss=%.1fMB private=%.1fMB procs=%s samples=%s",
                self.label,
                self._peak["rss_mb"],
                self._peak["private_mb"],
                self._peak["processes"],
                self._samples,
            )


def _wait_for_browser_exit(profile: Path, timeout: float = 10.0) -> bool:
    """Wait until no Camoufox process still owns the profile directory.

    Windows keeps profile files locked for a moment after close(); deleting
    too early leaves the directory behind (and it can be recreated).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = []
        for process in _find_browser_roots(profile):
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not alive:
            return True
        time.sleep(0.25)
    log.warning("[camoufox] browser processes still alive for %s", profile)
    return False


def _remove_profile(profile: Path) -> None:
    """Remove a temporary profile, retrying for Windows file locks."""
    for _ in range(10):
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return
        time.sleep(0.5)
    log.warning("[camoufox] could not remove profile %s", profile)


def _close_extra_pages(context: Any, keep: Any) -> None:
    for page in list(context.pages):
        if page is keep:
            continue
        try:
            page.close()
        except Exception:
            log.debug("could not close extra Camoufox page", exc_info=True)


@contextmanager
def open_browser(
    cookies: list[dict],
    *,
    headless: bool | str = False,
    proxy: dict | None = None,
    label: str | None = None,
) -> Iterator[tuple[object, object]]:
    """Yield (context, page) for a Camoufox session with cookies loaded."""
    _ensure_utf8_stdio()
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise RuntimeError(
            "camoufox is not installed: python -m pip install -e . "
            "(the browser engine is downloaded on first launch)"
        ) from exc

    mode = _headless_mode(headless)
    profile = Path(tempfile.mkdtemp(prefix="riot-camoufox-"))
    memory = None
    session_started = time.perf_counter()
    try:
        if proxy:
            exit_ip = _resolve_exit_ip(proxy)
            if not exit_ip:
                raise RuntimeError(
                    "Camoufox could not resolve GeoIP through proxy; "
                    "refusing to use the host timezone/locale fallback"
                )
            geoip: bool | str = exit_ip
            geo_label = "proxy"
        else:
            geoip = True
            geo_label = "host"

        options = camoufox_options(
            headless=mode,
            proxy=proxy,
            geoip=geoip,
            humanize=env_flag("CAMOUFOX_HUMANIZE", default=True),
        )
        options["user_data_dir"] = str(profile)

        with Camoufox(**options) as context:
            # Sampling every browser process tree costs a full process scan
            # each second; keep it for diagnostics, not for production runs.
            if env_flag("CAMOUFOX_MEMORY_LOG", default=False):
                memory = _MemoryMonitor(profile, label or "browser", interval=2.0)
                memory.start()
            if env_flag("CAMOUFOX_BLOCK_RESOURCES", default=False):
                install_resource_blocking(context)
            if cookies:
                context.add_cookies(cookies)

            pages = list(context.pages)
            page = pages[0] if pages else context.new_page()
            _close_extra_pages(context, page)
            log.info(
                "[camoufox] account=%s headless=%s geoip=%s launch=%.1fs",
                label or "browser",
                mode,
                geo_label,
                time.perf_counter() - session_started,
            )
            try:
                yield context, page
            finally:
                try:
                    page.close()
                except Exception:
                    log.debug("Camoufox page close failed", exc_info=True)
    finally:
        if memory is not None:
            memory.stop()
            # Only the diagnostics path waits for the process table; normal
            # cleanup relies on Playwright closing the browser and on the
            # retry loop in _remove_profile.
            _wait_for_browser_exit(profile, timeout=3.0)
        _remove_profile(profile)


__all__ = ["open_browser"]
