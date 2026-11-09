"""API-key authentication for the job API.

Keys are read once at startup, but the dependency asks for the current map on
every request so tests and hot reloads can patch it without restarting.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable

from fastapi import Header, HTTPException


def load_api_keys(api_key: str, api_keys_json: str) -> dict[str, str]:
    """Return {key: client_id} from API_KEYS JSON or a single API_KEY."""
    raw = api_keys_json.strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"API_KEYS must be valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not data:
            raise RuntimeError("API_KEYS must be a non-empty JSON object {key: client_id}")
        return {str(key): str(client) for key, client in data.items()}
    key = api_key.strip()
    if key:
        return {key: "default"}
    return {}


def api_key_dependency(
    keys: Callable[[], dict[str, str]],
) -> Callable[..., str]:
    """Build the FastAPI dependency that resolves X-API-Key to a client id.

    An empty key map means authentication is intentionally disabled.
    """

    def require_client(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str:
        current = keys()
        if not current:
            return "anonymous"
        if x_api_key:
            for key, client_id in current.items():
                if hmac.compare_digest(x_api_key.encode(), key.encode()):
                    return client_id
        raise HTTPException(
            status_code=401,
            detail="invalid or missing X-API-Key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    return require_client
