#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR/input" "$DATA_DIR/output"

# Bind-mount data/ приходит с правами хоста. Исправляем владельца и
# сбрасываем privileges до app, чтобы сервер не работал от root.
if [ "$(id -u)" = "0" ]; then
    chown -R app:app "$DATA_DIR" 2>/dev/null || true
    exec gosu app "$@"
fi

exec "$@"
