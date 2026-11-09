# Docker

Образ собирает API-сервер с Camoufox и предзагруженным Firefox/GeoIP.
Контейнер работает от пользователя `app` (UID 10001), наружу отдаёт только
порт 8000, состояние хранит в `./data`.

## Быстрый старт

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Linux/macOS
# отредактируйте API_KEY или API_KEYS в .env
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
```

Ответ: `{"status":"ok","backend":"camoufox"}`.

Первый build скачивает Firefox (~2.5 ГБ) в слой образа и занимает время.
Последующие сборки используют кэш, если не менялся `CAMOUFOX_VERSION`.

## Доступ снаружи

- Compose публикует порт на всех интерфейсах: `http://<host-ip>:8000`.
- Другой порт: `RIOT_PORT=8080 docker compose up -d`.
- API без ключа открыт всем. Задайте `API_KEY` или `API_KEYS` в `.env`;
  клиенты присылают `X-API-Key`.
- Для публичного доступа ставьте reverse proxy с TLS (Caddy/nginx) или
  туннель; не выставляйте контейнер в интернет без ключа.
- Firewall/reverse proxy должны пропускать выбранный порт только с нужных
  адресов, если сервис не публичный.

## Переменные

`docker-compose.yml` передаёт `.env` в контейнер и переопределяет ключевые
значения:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `RIOT_PORT` | `8000` | порт на хосте |
| `BIND_HOST` | `0.0.0.0` | адрес внутри контейнера (принудительно) |
| `PORT` | `8000` | порт uvicorn внутри контейнера |
| `DOCKER_MAX_WORKERS` | `4` | одновременных Camoufox |
| `DOCKER_MIN_FREE_MB` | `1500` | минимум свободной RAM для старта job'а |
| `JOBS_DB` | `/app/data/jobs.sqlite3` | SQLite внутри volume |
| `HEADLESS` | `1` | headless-режим |

Остальные переменные (`API_KEY`, `API_KEYS`, `WEBHOOK_*`, `SITEKEY`,
`ALLOW_NO_PROXY` и т.д.) берутся из `.env`. Если `.env` нет, скопируйте
`.env.example`.

`BIND_HOST` и `PORT` в `.env` могут содержать локальные значения — compose
всё равно переопределяет их на `0.0.0.0:8000` внутри контейнера.

## Данные

`./data` монтируется в `/app/data`:

- `data/input/` — accounts/proxies/emails, которые вы кладёте сами;
- `data/output/` — результаты job'ов и webhook'ов;
- `data/jobs.sqlite3` — состояние очереди.

Входные файлы можно положить на хост до запуска:

```bash
# Windows
copy accounts.jsonl data\input\accounts.jsonl
# Linux/macOS
cp accounts.jsonl data/input/accounts.jsonl
```

Внутри контейнера путь будет `/app/data/input/accounts.jsonl`.

Резервная копия состояния:

```bash
docker compose stop api
copy data\jobs.sqlite3 data\jobs.sqlite3.bak      # Windows
cp data/jobs.sqlite3 data/jobs.sqlite3.bak        # Linux/macOS
docker compose start api
```

## Проверка job'а

```bash
curl -sS -X POST "http://127.0.0.1:8000/api/v1/job" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d @data/input/job.json
```

`data/input/job.json` — один объект с `cookies`, `proxy`, `new_email` и
необязательными `external_id`, `webhook_url`, `headless`. Полный контракт —
[docs/jobs-api.md](jobs-api.md).

Проверить хранилище и статистику:

```bash
docker compose ps
docker compose logs -f api
curl -H "X-API-Key: YOUR_API_KEY" http://127.0.0.1:8000/api/v1/stats
```

## Webhook

Клиент может указать внешний `webhook_url` — контейнер сам сделает POST.
Для режима same-URL (`POST /api/v1/webhook`) **отдельный приёмник не нужен**:
сервер принимает событие на том же порту и пишет его в
`data/output/webhook-results.jsonl` (файл на хосте через volume).

Пример для job'а/скрипта:

```text
http://127.0.0.1:8000/api/v1/webhook
```

`WEBHOOK_PROXY_URL` нужен только если вы хотите пересылать событие в
отдельный локальный приёмник. В контейнере `127.0.0.1` — это сам контейнер,
поэтому для приёмника на хосте используйте `host.docker.internal`.

```ini
WEBHOOK_PROXY_URL=http://host.docker.internal:9000/hook
```

## CLI внутри контейнера

```bash
docker compose exec api python -m src.cli \
  --cookies /app/data/input/cookies.json \
  --email new@example.com --dry-run
```

CLI запускается в headless-режиме; headed-браузер в контейнере не нужен.

## Ресурсы

- Один Camoufox занимает ~0.55 ГБ на пустой странице и ~0.9–1.1 ГБ на
  Riot с hCaptcha. `DOCKER_MAX_WORKERS=4` — безопасный старт для 8 ГБ RAM.
- `shm_size: 2gb` уже задан. Если Firefox падает с memory/shm-ошибкой,
  увеличьте значение в `docker-compose.yml` и перезапустите.
- `MIN_FREE_MB` внутри контейнера видит память хоста, а не cgroup-лимит.
  При жёстком `--memory` уменьшите `DOCKER_MIN_FREE_MB` или задайте
  `MAX_WORKERS` вручную.

## Обновление

```bash
docker compose build --pull
docker compose up -d
```

При смене версии Camoufox обновите `CAMOUFOX_VERSION` в `Dockerfile` и
`docker-compose.yml`, а также пин в `pyproject.toml` — слой с браузером
пересоберётся.

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| `cannot connect to the Docker daemon` | запустите Docker Desktop/daemon |
| `port is already allocated` | `RIOT_PORT=8081 docker compose up -d` |
| `401 invalid or missing X-API-Key` | передайте `X-API-Key` из `.env` |
| `502` на `/api/v1/webhook` | `WEBHOOK_PROXY_URL` должен указывать на работающий приёмник |
| браузер завершился с ошибкой | `docker compose logs api`, проверьте libs/shm; не запускайте headed |
| `database is locked` | не запускайте два контейнера на одном `data/jobs.sqlite3` |
| первый build очень долгий | скачивается Camoufox/Firefox ~2.5 ГБ |

## Файлы Docker

- `Dockerfile` — два stage: browser cache и приложение.
- `docker-compose.yml` — порт, volume, лимиты, healthcheck.
- `docker-entrypoint.sh` — создаёт `data/input` и `data/output`, чинит
  владельца bind-mount и запускает процесс от `app`.
- `.dockerignore` — не тащит в образ `.env`, `data`, `tests`, `docs`.
