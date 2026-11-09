#!/usr/bin/env python3
"""Interactive cookie extractor for Riot accounts (entry point).

This script is a thin compatibility facade: the implementation lives in
src/extractor/. Names are re-exported because older scripts and tests import
them from here.

Examples:
  python cookie_extractor.py                 # interactive menu
  python cookie_extractor.py add             # add one account
  python cookie_extractor.py relogin acc-01  # sign in again
  python cookie_extractor.py list
  python cookie_extractor.py export --format batch --out batch.json
  python cookie_extractor.py remove acc-01

Ready data/input/accounts.jsonl can be passed to scripts/submit_accounts.py.
"""

from __future__ import annotations

from src.config import DATA_INPUT
from src.extractor.browser import (
    DEFAULT_LOGIN_URL,
    DEFAULT_TIMEOUT,
    VERIFY_JS,
    _find_account_page,
    _has_session_cookie,
    _is_not_logged_in,
    _page_is_blocked,
    _verify_page_session,
    capture_account,
)
from src.extractor.cli import build_parser, main
from src.extractor.commands import (
    _collect_account,
    _resolve_account_proxy,
    cmd_add,
    cmd_add_batch,
    cmd_assign_proxies,
    cmd_export,
    cmd_list,
    cmd_proxy_scan,
    cmd_relogin,
    cmd_remove,
)
from src.extractor.data import (
    DEFAULT_DOMAIN,
    DEFAULT_SESSION_COOKIE,
    DEFAULT_STORE,
    EMAIL_RE,
    EXTERNAL_ID_RE,
    MAX_COOKIES,
    CookieStore,
    ExtractorError,
    build_server_job,
    clean_label,
    clean_proxy,
    describe_proxy,
    domain_matches,
    export_jobs,
    extract_cookies,
    format_expiry,
    load_emails,
    log_to_stderr,
    make_external_id,
    next_label,
    normalize_cookie,
    now_iso,
    session_cookie_info,
    slugify,
    validate_email,
    validate_external_id,
)
from src.extractor.proxies import (
    DEFAULT_PROXY_POOL,
    DEFAULT_PROXY_TEST_URL,
    _grow_proxy_pool,
    _is_dynamic_proxy_file,
    _is_proxy_failure,
    _load_bad_proxy_keys,
    _mark_proxy_bad,
    _pick_proxy,
    _proxy_check_options,
    _proxy_key_or_raw,
    _prune_proxy_file,
    _rank_from_args,
    _rank_pool,
    _remove_proxy_from_file,
    _safe_proxy_label,
    _scan_from_args,
    _select_pool_proxy,
    _used_proxy_keys,
    _write_working_proxies,
    default_auto_proxy_pool,
    default_proxy_pool,
)
from src.extractor.ui import confirm, print_accounts, prompt, truncate

__all__ = [
    "DATA_INPUT",
    "DEFAULT_DOMAIN",
    "DEFAULT_LOGIN_URL",
    "DEFAULT_PROXY_POOL",
    "DEFAULT_PROXY_TEST_URL",
    "DEFAULT_SESSION_COOKIE",
    "DEFAULT_STORE",
    "DEFAULT_TIMEOUT",
    "EMAIL_RE",
    "EXTERNAL_ID_RE",
    "MAX_COOKIES",
    "VERIFY_JS",
    "CookieStore",
    "ExtractorError",
    "_collect_account",
    "_find_account_page",
    "_grow_proxy_pool",
    "_has_session_cookie",
    "_is_dynamic_proxy_file",
    "_is_not_logged_in",
    "_is_proxy_failure",
    "_load_bad_proxy_keys",
    "_mark_proxy_bad",
    "_page_is_blocked",
    "_pick_proxy",
    "_proxy_check_options",
    "_proxy_key_or_raw",
    "_prune_proxy_file",
    "_rank_from_args",
    "_rank_pool",
    "_remove_proxy_from_file",
    "_resolve_account_proxy",
    "_safe_proxy_label",
    "_scan_from_args",
    "_select_pool_proxy",
    "_used_proxy_keys",
    "_verify_page_session",
    "_write_working_proxies",
    "build_parser",
    "build_server_job",
    "capture_account",
    "clean_label",
    "clean_proxy",
    "cmd_add",
    "cmd_add_batch",
    "cmd_assign_proxies",
    "cmd_export",
    "cmd_list",
    "cmd_proxy_scan",
    "cmd_relogin",
    "cmd_remove",
    "confirm",
    "default_auto_proxy_pool",
    "default_proxy_pool",
    "describe_proxy",
    "domain_matches",
    "export_jobs",
    "extract_cookies",
    "format_expiry",
    "load_emails",
    "log_to_stderr",
    "main",
    "make_external_id",
    "next_label",
    "normalize_cookie",
    "now_iso",
    "print_accounts",
    "prompt",
    "session_cookie_info",
    "slugify",
    "truncate",
    "validate_email",
    "validate_external_id",
]

if __name__ == "__main__":
    raise SystemExit(main())
