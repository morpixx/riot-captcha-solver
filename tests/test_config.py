import json

import pytest

from src.config import blocked_email_domains, env_flag, load_config


def test_accounts_and_overrides(monkeypatch, tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(
        json.dumps(
            [
                {"name": "bodyatymkiv", "cookies": "c1.json", "new_email": "a@gmail.com"},
                {"name": "uglymorpix", "cookies": "c2.json", "new_email": "b@gmail.com"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("ALLOW_EMAIL_CHANGE", "0")
    cfg = load_config()
    assert [a.name for a in cfg.accounts] == ["bodyatymkiv", "uglymorpix"]
    assert cfg.account("uglymorpix").new_email == "b@gmail.com"
    assert cfg.allow_email_change is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_flag_true_values(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_flag("FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_flag_false_values(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_flag("FLAG", default=True) is (value == "")


def test_blocked_email_domains_are_normalized(monkeypatch):
    monkeypatch.setenv("BLOCKED_EMAIL_DOMAINS", " Outlook.com, @hotmail.com ,, ")
    assert blocked_email_domains() == {"outlook.com", "hotmail.com"}
