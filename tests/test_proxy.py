import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.proxy.proxy import describe, parse_proxy  # noqa: E402


def test_socks5_with_auth():
    p = parse_proxy("socks5://user:pa%40ss@1.2.3.4:1080")
    assert p == {"server": "socks5://1.2.3.4:1080", "username": "user", "password": "pa@ss"}


def test_http_without_auth():
    assert parse_proxy("http://5.6.7.8:8080") == {"server": "http://5.6.7.8:8080"}


def test_default_scheme_is_socks5():
    assert parse_proxy("9.9.9.9:1080")["server"] == "socks5://9.9.9.9:1080"


def test_invalid():
    for bad in ("ftp://1.2.3.4:21", "1.2.3.4", "socks5://1.2.3.4"):
        try:
            parse_proxy(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_describe_masks_credentials():
    assert "***" in describe(parse_proxy("socks5://u:p@1.2.3.4:1080"))
    assert describe(None) == "off"
