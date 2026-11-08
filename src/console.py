"""Console helpers shared by command-line entry points."""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("riot.console")


def configure_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 capable on Windows consoles and pipes.

    Windows consoles often default to cp1251/cp1252, and libraries that print
    Unicode (for example Playwright/Camoufox launch output) fail before doing
    any work. Streams that cannot be reconfigured are left untouched.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception as exc:
            log.debug("could not set Windows console codepage: %s", exc)

    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = str(getattr(stream, "encoding", "") or "")
            if encoding.lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue
