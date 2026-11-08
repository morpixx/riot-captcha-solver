#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -z "${PYTHON:-}" ]; then
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) PYTHON="python" ;;
    *) if command -v python3 >/dev/null 2>&1; then PYTHON="python3"; else PYTHON="python"; fi ;;
  esac
fi

echo "=== Riot API bootstrap ==="
echo "project: $ROOT"
echo "python:  $PYTHON"

if [ ! -w "$ROOT" ]; then
  echo "ERROR: $ROOT is not writable. Clone/copy the repo somewhere writable." >&2
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  if ! curl -fsS --max-time 15 https://pypi.org >/dev/null; then
    echo "ERROR: no internet. Enable network access for this environment." >&2
    exit 1
  fi
fi

export DEBIAN_FRONTEND=noninteractive

echo "[1/4] Installing Python dependencies..."
"$PYTHON" -m pip install --quiet --no-cache-dir --upgrade pip
"$PYTHON" -m pip install --quiet --no-cache-dir -e .

if [ "${INSTALL_DEPS:-1}" = "1" ]; then
  if [ "$(uname -s)" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
    echo "[2/4] Installing Firefox/Playwright system dependencies..."
    if [ "$(id -u)" = "0" ]; then
      "$PYTHON" -m playwright install-deps firefox || echo "WARN: install-deps failed; continuing"
    elif command -v sudo >/dev/null 2>&1; then
      sudo "$PYTHON" -m playwright install-deps firefox || echo "WARN: install-deps failed; continuing"
    else
      echo "WARN: not root and no sudo; skipping system dependencies"
    fi
  else
    echo "[2/4] System dependencies: skipped (not Linux/apt)"
  fi
else
  echo "[2/4] System dependencies: disabled by INSTALL_DEPS=0"
fi

if [ "$(uname -s)" = "Linux" ]; then
  export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$ROOT/.cache}"
  mkdir -p "$XDG_CACHE_HOME"
fi

echo "[3/4] Fetching Camoufox/Firefox (first run is ~2.5 GB)..."
"$PYTHON" -m camoufox fetch
"$PYTHON" -c "from camoufox.geolocation import download_mmdb; download_mmdb()" || echo "WARN: GeoIP prefetch skipped"

mkdir -p data/input data/output

export PYTHONUNBUFFERED=1
export BIND_HOST="${BIND_HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export HEADLESS=1
export MAX_WORKERS="${MAX_WORKERS:-10}"
export MIN_FREE_MB="${MIN_FREE_MB:-660}"
export JOBS_DB="${JOBS_DB:-$ROOT/data/jobs.sqlite3}"
export ENABLE_DOCS="${ENABLE_DOCS:-0}"
export ENABLE_SYNC_RUN="${ENABLE_SYNC_RUN:-0}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

GENERATED_KEY=""
if [ -z "${API_KEY:-}" ] && [ -z "${API_KEYS:-}" ]; then
  API_KEY="$("$PYTHON" -c "import secrets; print(secrets.token_urlsafe(32))")"
  export API_KEY
  GENERATED_KEY="$API_KEY"
fi

GENERATED_WEBHOOK_SECRET=""
if [ -z "${WEBHOOK_SECRET:-}" ]; then
  WEBHOOK_SECRET="$("$PYTHON" -c "import secrets; print(secrets.token_urlsafe(32))")"
  export WEBHOOK_SECRET
  GENERATED_WEBHOOK_SECRET="$WEBHOOK_SECRET"
fi

if [ -z "${START_TUNNEL:-}" ]; then
  if [ -n "${KAGGLE_KERNEL_RUN_TYPE:-}" ] || [ -d /kaggle/working ]; then
    START_TUNNEL=1
  else
    START_TUNNEL=0
  fi
fi
export START_TUNNEL

PUBLIC_URL=""
if [ "$START_TUNNEL" = "1" ]; then
  if [ -z "${NGROK_AUTHTOKEN:-}" ]; then
    echo "WARN: NGROK_AUTHTOKEN is not set; public tunnel disabled"
  else
    OS_NAME="$(uname -s)"
    ARCH_NAME="$(uname -m)"
    case "$ARCH_NAME" in
      aarch64|arm64) ARCH_NAME="arm64" ;;
      *) ARCH_NAME="amd64" ;;
    esac

    if [ -z "${NGROK:-}" ]; then
      if command -v ngrok >/dev/null 2>&1; then
        NGROK="$(command -v ngrok)"
      else
        NGROK="/tmp/ngrok"
      fi
    fi
    NGROK_OS=""
    case "$OS_NAME" in
      Linux*) NGROK_OS="linux" ;;
      Darwin*) NGROK_OS="darwin" ;;
    esac

    if [ ! -x "$NGROK" ] && [ -n "$NGROK_OS" ]; then
      echo "[4/4] Downloading ngrok..."
      curl -fsSL --retry 3 -o /tmp/ngrok.zip "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-${NGROK_OS}-${ARCH_NAME}.zip" || true
      "$PYTHON" -m zipfile -e /tmp/ngrok.zip /tmp 2>/dev/null || true
      chmod +x "$NGROK" 2>/dev/null || true
    fi

    if [ -x "$NGROK" ]; then
      "$NGROK" config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null 2>&1 || true
      echo "[4/4] Starting ngrok..."
      nohup "$NGROK" http "$PORT" --log=stdout > /tmp/ngrok.log 2>&1 &
      for _ in $(seq 1 30); do
        PUBLIC_URL="$(grep -oE "https://[a-z0-9-]+\.ngrok-free\.(app|dev)" /tmp/ngrok.log | head -1 || true)"
        if [ -n "$PUBLIC_URL" ]; then
          break
        fi
        sleep 1
      done
    fi

    if [ -z "$PUBLIC_URL" ]; then
      echo "WARN: public tunnel not ready; see /tmp/ngrok.log"
    fi
  fi
fi

LOCAL_URL="http://127.0.0.1:${PORT}"
SUBMIT_URL="${PUBLIC_URL:-$LOCAL_URL}"

echo ""
echo "================ READY ================"
echo "local:  $LOCAL_URL"
if [ -n "$PUBLIC_URL" ]; then
  echo "public: $PUBLIC_URL"
else
  echo "public: none (set START_TUNNEL=1 and NGROK_AUTHTOKEN)"
fi
if [ -n "$GENERATED_KEY" ]; then
  echo "api key: $GENERATED_KEY"
else
  echo "api key: use API_KEY/API_KEYS from the environment"
fi
if [ -n "$GENERATED_WEBHOOK_SECRET" ]; then
  echo "webhook secret: $GENERATED_WEBHOOK_SECRET"
fi
echo ""
echo "Submit from another machine (or locally):"
echo "  python scripts/submit_accounts.py --url $SUBMIT_URL --api-key ${GENERATED_KEY:-YOUR_API_KEY} --input data/input/accounts-test.jsonl --out data/output/job-results.jsonl --webhook-url http://127.0.0.1:${PORT}/api/v1/webhook --concurrency 10 --rate 10"
echo ""
echo "Results: data/output/webhook-results.jsonl"
echo "Stop: Ctrl-C"
echo "========================================"
echo ""

if [ "${FOREGROUND:-1}" = "0" ]; then
  echo "Starting API in background..."
  nohup "$PYTHON" server.py > /tmp/riot-api.log 2>&1 &
  echo "server pid: $!"
  echo "logs: tail -f /tmp/riot-api.log"
  exit 0
fi

exec "$PYTHON" server.py
