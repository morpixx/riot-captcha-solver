"""Runtime configuration and the list of local test accounts.

Environment values are read once when a CLI/page object is created. Keeping
parsing here means the API server, CLI and batch runner agree on how a boolean
such as HEADLESS=0 is interpreted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency in some environments

    def load_dotenv(*_args, **_kwargs):
        return False


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_INPUT = DATA_DIR / "input"
DATA_OUTPUT = DATA_DIR / "output"
DEFAULT_SITEKEY = "b3c619fc-4b72-4838-9bf2-398888588d62"
DEFAULT_HOST = "account.riotgames.com"
_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))


def env_flag(name: str, default: bool = False) -> bool:
    """Read a common boolean environment flag.

    Both 1/true/yes/on and their case variants are accepted. An unset or
    blank variable returns default.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def blocked_email_domains() -> set[str]:
    """Domains rejected before a browser is started.

    The value is a comma-separated list, for example
    outlook.com,hotmail.com. Empty means no domain filtering.
    """
    raw = os.environ.get("BLOCKED_EMAIL_DOMAINS", "")
    return {item.strip().lower().lstrip("@") for item in raw.split(",") if item.strip()}


@dataclass(frozen=True)
class Account:
    """One email-change target.

    Cookies may come from a JSON file (CLI) or directly from an API request
    (kept in memory only).
    """

    name: str
    cookies: Path | list[dict]
    new_email: str
    proxy: str | None = None


@dataclass(frozen=True)
class Config:
    sitekey: str
    host: str
    headless: bool
    allow_email_change: bool
    captcha_timeout: int
    accounts: list[Account]

    def account(self, name: str) -> Account:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        raise ValueError(f"unknown account {name!r}; known: {[a.name for a in self.accounts]}")


def load_flow_config(
    *,
    accounts: list[Account] | None = None,
    allow_email_change: bool | None = None,
) -> Config:
    """Build the browser-flow config shared by the CLI and the API server.

    The server has no accounts.json: it passes an empty list and receives
    the account from each request instead.
    """
    return Config(
        sitekey=os.environ.get("SITEKEY", DEFAULT_SITEKEY),
        host=os.environ.get("HOST", DEFAULT_HOST),
        headless=env_flag("HEADLESS", default=True),
        allow_email_change=(
            env_flag("ALLOW_EMAIL_CHANGE") if allow_email_change is None else allow_email_change
        ),
        captcha_timeout=int(os.environ.get("CAPTCHA_TIMEOUT", "150")),
        accounts=list(accounts or []),
    )


def load_config(env_file: str | None = None) -> Config:
    """Load accounts.json plus flow settings for the local CLI tools."""
    load_dotenv(env_file or ROOT / ".env")

    accounts_path = ROOT / os.environ.get("ACCOUNTS_FILE", "data/input/accounts.json")
    if not accounts_path.is_file():
        raise ValueError(f"accounts file not found: {accounts_path} (put it in data/input/)")

    raw = json.loads(accounts_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("accounts.json must be a non-empty JSON array")

    accounts: list[Account] = []
    for item in raw:
        name = str(item.get("name", "")).strip()
        cookies = ROOT / str(item.get("cookies", f"cookies-{name}.json"))
        email = str(item.get("new_email", "")).strip()
        if not name or not email or "@" not in email:
            raise ValueError(f"bad account entry: {item!r}")
        proxy = str(item.get("proxy", "")).strip() or None
        accounts.append(Account(name=name, cookies=cookies, new_email=email, proxy=proxy))

    return load_flow_config(accounts=accounts)
