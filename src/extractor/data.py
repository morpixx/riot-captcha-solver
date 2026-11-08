"""Validation, cookie normalization, JSONL storage and export for cookie_extractor.

This module has no browser or CLI dependencies: it can be used by scripts and
tests without starting a browser.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DATA_INPUT, ROOT
from ..proxy.proxy import describe, parse_proxy

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

DEFAULT_STORE = Path(os.environ.get("COOKIES_FILE") or (DATA_INPUT / "accounts.jsonl"))
DEFAULT_DOMAIN = os.environ.get("COOKIE_DOMAIN", "riotgames.com")
DEFAULT_SESSION_COOKIE = os.environ.get("SESSION_COOKIE", "ssid")
MAX_COOKIES = 200  # matches JobSpecRequest.cookies max_length on the server

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ExtractorError(RuntimeError):
    """Expected, user-facing extractor error (printed without a traceback)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_to_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Normalization and validation
# --------------------------------------------------------------------------- #


def clean_label(value: str) -> str:
    label = " ".join(str(value).split())
    if not label:
        raise ExtractorError("название аккаунта не может быть пустым")
    if len(label) > 64:
        raise ExtractorError("название аккаунта длиннее 64 символов")
    if any(ord(ch) < 32 for ch in label):
        raise ExtractorError("в названии аккаунта есть управляющие символы")
    return label


def validate_email(value: str) -> str:
    email = str(value).strip()
    if not email:
        raise ExtractorError("новая почта не указана")
    try:
        from email_validator import validate_email as _validate
    except ImportError:  # pragma: no cover - optional dependency
        _validate = None

    if _validate is not None:
        try:
            return _validate(email, check_deliverability=False).normalized
        except Exception as exc:
            raise ExtractorError(f"некорректный email: {email!r} ({exc})") from exc
    if not EMAIL_RE.match(email):
        raise ExtractorError(f"некорректный email: {email!r}")
    return email


def load_emails(path: str | Path) -> list[str]:
    email_path = Path(path)
    if not email_path.exists():
        raise ExtractorError(f"файл с адресами не найден: {email_path}")
    try:
        text = email_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExtractorError(f"не удалось прочитать {email_path}: {exc}") from exc
    emails: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            email = validate_email(line)
        except ExtractorError as exc:
            raise ExtractorError(f"{email_path.name}:{lineno}: {exc}") from exc
        key = email.casefold()
        if key not in seen:
            seen.add(key)
            emails.append(email)
    if not emails:
        raise ExtractorError(f"в {email_path} нет ни одного email")
    return emails


def clean_proxy(value: str | None) -> str:
    proxy = str(value or "").strip()
    if not proxy:
        return ""
    try:
        parse_proxy(proxy)
    except ValueError as exc:
        raise ExtractorError(f"некорректный proxy: {proxy!r} ({exc})") from exc
    return proxy


def describe_proxy(value: str | None) -> str:
    if not value:
        return "direct"
    try:
        return describe(parse_proxy(value))
    except Exception:
        return "<некорректный proxy>"


def slugify(label: str) -> str:
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_label.lower()).strip("-")
    if not slug:
        slug = "acc-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    return slug[:60].strip("-") or "acc"


def make_external_id(label: str, taken: Iterable[str] = ()) -> str:
    taken_folded = {str(item).casefold() for item in taken if item}
    base = f"riot-{slugify(label)}"
    if base.casefold() not in taken_folded:
        return base
    suffix = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base[:118]}-{suffix}"
    if candidate.casefold() in taken_folded:
        raise ExtractorError(f"не удалось подобрать уникальный external_id для {label!r}")
    return candidate


def validate_external_id(value: str) -> str:
    external_id = str(value).strip()
    if not EXTERNAL_ID_RE.match(external_id):
        raise ExtractorError("external_id: 1-128 символов, только латиница, цифры и . _ : -")
    return external_id


def domain_matches(domain: str, root_domain: str) -> bool:
    d = str(domain or "").strip().lower().lstrip(".")
    root = str(root_domain or "").strip().lower().lstrip(".")
    if not d or not root:
        return False
    return d == root or d.endswith("." + root)


def normalize_cookie(raw: dict[str, Any], root_domain: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None

    domain = str(raw.get("domain") or ("." + root_domain.strip().lstrip("."))).strip()
    if not domain_matches(domain, root_domain):
        return None

    cookie: dict[str, Any] = {
        "name": name,
        "value": str(raw.get("value", "")),
        "domain": domain,
        "path": str(raw.get("path") or "/"),
    }

    expires = raw.get("expires", raw.get("expirationDate"))
    if expires is not None:
        try:
            expires_f = float(expires)
        except (TypeError, ValueError):
            expires_f = 0.0
        # -1/0 mean a session cookie; the server field is optional.
        if expires_f > 0:
            cookie["expires"] = expires_f

    for key in ("httpOnly", "secure"):
        if raw.get(key) is not None:
            cookie[key] = bool(raw.get(key))
    return cookie


def extract_cookies(
    raw_cookies: Iterable[dict[str, Any]],
    root_domain: str = DEFAULT_DOMAIN,
    session_cookie: str = DEFAULT_SESSION_COOKIE,
) -> list[dict[str, Any]]:
    """Keep cookies for the target domain, deduplicate them and check the session."""
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in raw_cookies or []:
        cookie = normalize_cookie(raw, root_domain)
        if cookie is None:
            continue
        key = (cookie["name"], cookie["domain"].lower(), cookie["path"])
        by_key[key] = cookie

    cookies = sorted(by_key.values(), key=lambda c: (c["domain"], c["path"], c["name"]))
    if session_cookie:
        session = next(
            (c for c in cookies if c["name"] == session_cookie and c["value"]),
            None,
        )
        if session is None:
            raise ExtractorError(
                f"cookie {session_cookie!r} не найдена — вход не завершён или сессия не создана"
            )
    if len(cookies) > MAX_COOKIES:
        raise ExtractorError(
            f"получилось {len(cookies)} cookie, сервер принимает не больше {MAX_COOKIES}"
        )
    return cookies


def session_cookie_info(
    cookies: Iterable[dict[str, Any]], name: str = DEFAULT_SESSION_COOKIE
) -> dict[str, Any] | None:
    for cookie in cookies or []:
        if cookie.get("name") == name:
            return cookie
    return None


def format_expiry(cookie: dict[str, Any] | None) -> str:
    if not cookie:
        return "нет"
    expires = cookie.get("expires")
    if not expires:
        return "session"
    try:
        exp = datetime.fromtimestamp(float(expires), timezone.utc)
    except (TypeError, ValueError, OSError):
        return "?"
    left = exp - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "EXPIRED"
    days = left.days
    if days >= 2:
        return f"{days}d"
    hours = int(left.total_seconds() // 3600)
    return f"{hours}h"


# --------------------------------------------------------------------------- #
# JSONL storage and export
# --------------------------------------------------------------------------- #


def _atomic_write(path: Path, text: str) -> None:
    """Write a file via a temporary sibling so a crash never leaves half a store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        with suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


class CookieStore:
    """One JSONL file: one line = one account."""

    def __init__(self, path: str | Path = DEFAULT_STORE, *, log=None):
        self.path = Path(path)
        self.log = log or log_to_stderr
        self.entries: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        self.entries = []
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExtractorError(f"не удалось прочитать {self.path}: {exc}") from exc
        if not text.strip():
            return

        items: list[Any]
        if text.lstrip().startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ExtractorError(f"{self.path}: битый JSON-массив: {exc}") from exc
            if not isinstance(loaded, list):
                raise ExtractorError(f"{self.path}: ожидался JSON-массив")
            items = loaded
        else:
            items = []
            for lineno, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    self.log(f"[!] {self.path.name}:{lineno}: строка пропущена ({exc})")

        seen: dict[str, int] = {}
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                self.log(f"[!] {self.path.name}: запись #{index} не объект, пропущена")
                continue
            label = str(
                item.get("label")
                or item.get("name")
                or item.get("external_id")
                or f"account-{index}"
            )
            try:
                label = clean_label(label)
            except ExtractorError:
                self.log(f"[!] {self.path.name}: запись #{index} с пустым label, пропущена")
                continue
            entry = dict(item)
            entry["label"] = label
            if not entry.get("external_id"):
                entry["external_id"] = make_external_id(
                    label, [e.get("external_id", "") for e in self.entries]
                )
            key = label.casefold()
            if key in seen:
                self.log(f"[!] {self.path.name}: дубль label {label!r}, оставлена последняя запись")
                self.entries[seen[key]] = entry
            else:
                seen[key] = len(self.entries)
                self.entries.append(entry)

    def find(self, needle: str) -> dict[str, Any] | None:
        target = str(needle or "").strip().casefold()
        if not target:
            return None
        for entry in self.entries:
            if str(entry.get("label", "")).casefold() == target:
                return entry
            if str(entry.get("external_id", "")).casefold() == target:
                return entry
        return None

    def _check_id_collision(
        self, external_id: str, *, ignore: dict[str, Any] | None = None
    ) -> None:
        for entry in self.entries:
            if entry is ignore:
                continue
            if str(entry.get("external_id", "")).casefold() == external_id.casefold():
                raise ExtractorError(
                    f"external_id {external_id!r} уже занят аккаунтом {entry.get('label')!r}"
                )

    def upsert(self, entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        label = clean_label(str(entry.get("label") or ""))
        existing = self.find(label)
        created = existing is None

        if existing is None:
            external_id = str(entry.get("external_id") or "").strip()
            if external_id:
                external_id = validate_external_id(external_id)
                self._check_id_collision(external_id)
            else:
                external_id = make_external_id(
                    label, [e.get("external_id", "") for e in self.entries]
                )
            record = {
                "external_id": external_id,
                "label": label,
                "new_email": entry.get("new_email", ""),
                "proxy": entry.get("proxy", ""),
                "cookies": entry.get("cookies", []),
                "headless": bool(entry.get("headless", True)),
                "updated_at": entry.get("updated_at") or now_iso(),
            }
            # Preserve optional user fields that were present in the entry.
            for key, value in entry.items():
                record.setdefault(key, value)
            self.entries.append(record)
        else:
            external_id = str(entry.get("external_id") or existing.get("external_id") or "").strip()
            if not external_id:
                external_id = make_external_id(
                    label, [e.get("external_id", "") for e in self.entries if e is not existing]
                )
            external_id = validate_external_id(external_id)
            self._check_id_collision(external_id, ignore=existing)
            merged = dict(existing)
            merged.update(entry)
            merged["label"] = existing.get("label") or label
            merged["external_id"] = external_id
            merged["updated_at"] = entry.get("updated_at") or now_iso()
            merged["headless"] = bool(entry.get("headless", existing.get("headless", True)))
            index = self.entries.index(existing)
            self.entries[index] = merged
            record = merged

        self.save()
        return record, created

    def remove(self, needle: str) -> dict[str, Any]:
        entry = self.find(needle)
        if entry is None:
            raise ExtractorError(f"аккаунт {needle!r} не найден в {self.path}")
        self.entries.remove(entry)
        self.save()
        return entry

    def save(self) -> None:
        text = "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in self.entries
        )
        _atomic_write(self.path, text)


def next_label(store: CookieStore, prefix: str = "acc") -> str:
    """First free acc-01, acc-02, ..."""
    prefix = str(prefix or "acc").strip().strip("-") or "acc"
    used = {str(entry.get("label", "")).casefold() for entry in store.entries}
    for number in range(1, 100000):
        candidate = f"{prefix}-{number:02d}"
        if candidate.casefold() not in used:
            return candidate
    raise ExtractorError("не удалось подобрать свободный label")


def build_server_job(
    entry: dict[str, Any],
    *,
    root_domain: str = DEFAULT_DOMAIN,
    session_cookie: str = DEFAULT_SESSION_COOKIE,
    require_proxy: bool = True,
) -> dict[str, Any]:
    cookies = entry.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise ExtractorError("нет cookie")
    cookies = extract_cookies(cookies, root_domain, session_cookie)
    email = validate_email(str(entry.get("new_email") or ""))
    proxy = clean_proxy(str(entry.get("proxy") or ""))
    if require_proxy and not proxy:
        raise ExtractorError("не указан proxy (сервер требует proxy для каждого job)")
    external_id = str(entry.get("external_id") or "").strip()
    if not external_id:
        external_id = make_external_id(str(entry.get("label") or "account"))
    external_id = validate_external_id(external_id)
    return {
        "external_id": external_id,
        "cookies": cookies,
        "proxy": proxy,
        "new_email": email,
        "headless": bool(entry.get("headless", True)),
    }


def export_jobs(
    store: CookieStore,
    *,
    fmt: str = "jsonl",
    out: str | Path | None = None,
    require_proxy: bool = True,
    skip_incomplete: bool = False,
    root_domain: str = DEFAULT_DOMAIN,
    session_cookie: str = DEFAULT_SESSION_COOKIE,
) -> tuple[int, Path | None]:
    if fmt not in ("jsonl", "json", "batch"):
        raise ExtractorError("format должен быть jsonl, json или batch")
    if not store.entries:
        raise ExtractorError("список аккаунтов пуст")

    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for entry in store.entries:
        label = entry.get("label") or "?"
        try:
            job = build_server_job(
                entry,
                root_domain=root_domain,
                session_cookie=session_cookie,
                require_proxy=require_proxy,
            )
            if job["external_id"].casefold() in seen_ids:
                raise ExtractorError(f"повторяется external_id {job['external_id']!r}")
            seen_ids.add(job["external_id"].casefold())
            jobs.append(job)
        except ExtractorError as exc:
            errors.append(f"{label}: {exc}")

    if errors:
        if skip_incomplete:
            for error in errors:
                log_to_stderr(f"[!] пропущен {error}")
        else:
            detail = "\n".join(f"  - {error}" for error in errors)
            raise ExtractorError(
                "не все записи готовы к отправке:\n"
                + detail
                + "\nИсправьте их или запустите с --skip-incomplete."
            )
    if not jobs:
        raise ExtractorError("после проверки не осталось ни одного готового job")

    if fmt == "jsonl":
        text = "".join(
            json.dumps(job, ensure_ascii=False, separators=(",", ":")) + "\n" for job in jobs
        )
        default_name = "accounts.export.jsonl"
    elif fmt == "json":
        text = json.dumps(jobs, ensure_ascii=False, indent=2) + "\n"
        default_name = "accounts.export.json"
    else:
        text = json.dumps({"priority": 0, "jobs": jobs}, ensure_ascii=False, indent=2) + "\n"
        default_name = "batch.json"

    if out is None:
        out_path: Path | None = ROOT / default_name
    elif str(out) == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return len(jobs), None
    else:
        out_path = Path(out)
        try:
            same_path = out_path.resolve() == store.path.resolve()
        except OSError:
            same_path = False
        if same_path:
            raise ExtractorError("--out не должен совпадать с файлом --store")

    assert out_path is not None
    _atomic_write(out_path, text)
    return len(jobs), out_path
