# Riot email automation

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![Camoufox](https://img.shields.io/badge/browser-Camoufox%20Firefox-ff7139)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688)

Смена почты Riot через headless **Camoufox (Firefox)** и API-очередь job'ов.
Проект рассчитан на собственные аккаунты и собственные прокси; это не
официальный клиент Riot Games.

## Возможности

- Headless Camoufox с fingerprint Windows, GeoIP по exit-IP прокси и
  отключённым WebRTC.
- Минт hCaptcha-токена в изолированном world страницы, без обхода CSP
  через внешние скрипты.
- SQLite-очередь: параллелизм, ретраи, идемпотентность по `external_id`,
  отмена job'а, статистика.
- Webhook на том же порту: терминальные события пишутся в
  `data/output/webhook-results.jsonl`, отдельный приёмник не нужен.
- Оптимизация ресурсов Camoufox: Firefox-prefs, блокировка тяжёлых
  запросов, humanized-курсор, memory guard.
- Запуск нативно (Linux/macOS/Windows), в Docker и на удалённом сервере
  через ngrok.

## Требования

- Python **3.10+**, 64-bit.
- Linux, macOS или Windows.
- ~3 ГБ диска на первый запуск (Camoufox/Firefox ~2.5 ГБ).
- 1 ГБ RAM на один браузер; для 10 браузеров нужно ~10 ГБ и запас.
- Для публичного доступа — бесплатный `NGROK_AUTHTOKEN`.
- Docker — опционально.

## Быстрый старт

### Linux / macOS / Git Bash

```bash
bash scripts/start.sh
```

### Windows PowerShell

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Первый запуск ставит зависимости, скачивает Camoufox и GeoLite2, создаёт
`data/input` и `data/output`, поднимает API на `http://127.0.0.1:8000` и
печатает `API_KEY`.

Проверка:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

`scripts/start.ps1` поднимает локальный API без туннеля. Для публичного
доступа на Windows используй Docker, ngrok вручную или WSL.

### Публичный доступ через ngrok

```bash
START_TUNNEL=1 NGROK_AUTHTOKEN=<token> bash scripts/start.sh
```

Скрипт сам скачает ngrok (Linux/macOS), поднимет туннель и напечатает
публичный URL. На Kaggle туннель включается автоматически, но токен всё
равно нужен:

```bash
NGROK_AUTHTOKEN=<token> bash scripts/start.sh
```

Подробно: [docs/start.md](docs/start.md), [docs/tunnels.md](docs/tunnels.md).

### Docker

```bash
cp .env.example .env        # Linux/macOS
copy .env.example .env      # Windows
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
```

Порт на хосте задаётся `RIOT_PORT`, данные лежат в `./data`. Webhook по
умолчанию работает на том же порту:
`http://HOST:8000/api/v1/webhook` → `data/output/webhook-results.jsonl`.
Подробности: [docs/docker.md](docs/docker.md).

## Как это работает

1. Клиент отправляет cookies, прокси и новый email в `POST /api/v1/job`
   или batch в `POST /api/v1/jobs`.
2. Job попадает в SQLite-очередь; cookies и прокси остаются только в
   памяти и не пишутся в БД.
3. Camoufox открывает Riot, проверяет сессию, подставляет exit-IP прокси,
   минтит hCaptcha-токен и отправляет запрос смены почты.
4. Терминальный статус (`succeeded`, `dead`, `canceled`, `needs_resubmit`)
   уходит в webhook или в same-port журнал.
5. Если Riot просит подтвердить email, подтверждение выполняется отдельно.

Проект не обходит hCaptcha: он получает токен в браузере и передаёт его
Riot. Качество прокси и актуальность cookies влияют на результат сильнее
всего остального.

## API

Авторизация: `X-API-Key: <ключ>` (значение печатает `scripts/start.sh`).
Для нескольких клиентов используй `API_KEYS`.

| Метод | Endpoint | Назначение |
|---|---|---|
| `POST` | `/api/v1/job` | один аккаунт — один POST |
| `POST` | `/api/v1/jobs` | batch до `BATCH_MAX` (500) |
| `GET` | `/api/v1/jobs` | список/фильтр по статусу |
| `GET` | `/api/v1/jobs/{id}` | статус job'а |
| `POST` | `/api/v1/jobs/{id}/cancel` | отмена |
| `GET` | `/api/v1/stats` | очередь и воркеры |
| `GET` | `/api/v1/health` | healthcheck |
| `POST` | `/api/v1/webhook` | same-port журнал/webhook |

`POST /api/v1/run` доступен только при `ENABLE_SYNC_RUN=1`; по умолчанию
выключен. Swagger (`/docs`) включается `ENABLE_DOCS=1`.

Пример отправки одного аккаунта:

```bash
python scripts/submit_accounts.py \
  --url http://127.0.0.1:8000 \
  --api-key "$API_KEY" \
  --input data/input/accounts-test.jsonl \
  --out data/output/job-results.jsonl \
  --webhook-url http://127.0.0.1:8000/api/v1/webhook \
  --concurrency 10 --rate 10
```

`--webhook-url` — адрес **с точки зрения сервера**. Для удалённого сервера
он всё равно остаётся `http://127.0.0.1:8000/api/v1/webhook`; публичный URL
нужен только для `--url`.

Контракт, статусы, идемпотентность и формат webhook: [docs/jobs-api.md](docs/jobs-api.md).

## Конфигурация

Все переменные — в [.env.example](.env.example). Основные:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | интерфейс API; `0.0.0.0` — только осознанно |
| `PORT` | `8000` | порт API |
| `API_KEY` / `API_KEYS` | — | ключ клиента / несколько клиентов |
| `MAX_WORKERS` | `5` в `.env.example`; `10` в `start.sh`; `4` в Docker | одновременные браузеры |
| `MIN_FREE_MB` | `2048` / `660` / `1024` | memory guard перед стартом job'а |
| `MAX_QUEUE` | `5000` | максимум job'ов в очереди |
| `BATCH_MAX` | `500` | максимум job'ов в одном POST |
| `JOB_ATTEMPTS` | `3` | попытки на job |
| `HEADLESS` | `1` | headless-режим Camoufox |
| `ALLOW_NO_PROXY` | `0` | разрешить job без прокси (только тесты) |
| `WEBHOOK_SECRET` | — | HMAC-подпись webhook'ов |
| `ENABLE_SYNC_RUN` | `0` | синхронный `/api/v1/run` |
| `ENABLE_DOCS` | `0` | Swagger `/docs` |
| `JOBS_DB` | `data/jobs.sqlite3` | SQLite-очередь |
| `COOKIES_FILE` | `data/input/accounts.jsonl` | store для `cookie_extractor.py` |
| `NGROK_AUTHTOKEN` | — | токен ngrok для `start.sh` |
| `START_TUNNEL` | `0` локально, `1` на Kaggle | публичный ngrok-туннель |

`scripts/start.sh` по умолчанию берёт 10 воркеров и `MIN_FREE_MB=660` —
это агрессивный режим для 16 ГБ. Для 8 ГБ поставь `MAX_WORKERS=4` и
`MIN_FREE_MB=1536`. Docker по умолчанию использует 4/1024.

## Данные

- `data/input/` — аккаунты, cookies, прокси, email-файлы. В git не попадают.
- `data/output/` — результаты, webhook-журнал, логи job'ов. В git не попадают.
- `data/jobs.sqlite3` — очередь и статусы; cookies/прокси туда не пишутся.
- В репозитории нет аккаунтов и cookie: их нужно подготовить локально.

Формат аккаунтов и `relogin`/`add`: [docs/cookie-extractor.md](docs/cookie-extractor.md).

## Структура

```text
.
├─ server.py                 # FastAPI: auth, jobs, webhook, stats
├─ cookie_extractor.py       # ручной вход, cookies, relogin, add
├─ src/
│  ├─ api/                   # Pydantic-схемы и HTTP-слой
│  ├─ jobs/                  # очередь, dispatcher, batch, результаты
│  ├─ riot/                  # flow, minter hCaptcha, контракт Riot
│  ├─ browser/               # Camoufox backend и resource tuning
│  ├─ proxy/                 # разбор/проверка прокси
│  └─ extractor/             # cookie extractor и CLI
├─ scripts/
│  ├─ start.sh               # универсальный launcher (Linux/macOS)
│  ├─ start.ps1              # Windows launcher
│  ├─ submit_accounts.py     # построчная отправка аккаунтов
│  ├─ webhook_receiver.py    # локальный приёмник webhook
│  └─ client_example.py      # пример клиента
├─ docs/                     # архитектура, API, Docker, ngrok, cookies
├─ deploy/ngrok.yml          # конфиг ngrok
├─ data/                     # input/output/БД (не коммитится)
└─ tests/                    # pytest
```

## Документация

- [Запуск в любой среде](docs/start.md)
- [Docker](docs/docker.md)
- [Jobs API](docs/jobs-api.md)
- [ngrok](docs/tunnels.md)
- [Архитектура](docs/architecture.md)
- [Cookie extractor](docs/cookie-extractor.md)
- [Передача клиенту](docs/client-handoff.md)
- [Безопасность](SECURITY.md)
- [Разработка](CONTRIBUTING.md)

## Тесты

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

В текущей версии: **113 passed**, `ruff` без замечаний.

## Безопасность

- Публичный API без `API_KEY` открыт всем: всегда задавай ключ.
- `BIND_HOST=127.0.0.1` — безопасный default; `0.0.0.0` только для
  контролируемой сети.
- ngrok терминирует TLS и видит cookies/прокси; не используй его для
  чужих аккаунтов.
- `WEBHOOK_SECRET` подписывает терминальные события.
- `.env`, `accounts*.jsonl`, прокси, `data/output`, `jobs.sqlite3` и `.cache`
  исключены `.gitignore`.

Подробно: [SECURITY.md](SECURITY.md).

## Ограничения

- Riot может менять страницу, hCaptcha и требования к сессии.
- Качество прокси и свежесть cookies не гарантируют результат.
- Подтверждение email после смены может требовать ручного шага.
- Проект не является официальным продуктом Riot Games; используй только
  свои аккаунты и соблюдай ToS.
- Windows-лаунчер не поднимает ngrok; для публичного доступа — Docker,
  ngrok вручную или WSL.

## Публикация на GitHub

```bash
git add .
git status
git commit -m "Riot Camoufox automation"
git branch -M main
git remote add origin https://github.com/USER/REPO.git
git push -u origin main
```

Перед push проверь `git status`: `.env`, cookies, прокси и `data/output`
не должны попасть в индекс.

## Лицензия

Лицензия не указана. Если репозиторий публичный, добавь `LICENSE`
(например, MIT или Apache-2.0). Без файла лицензии код по умолчанию
распространяется как all rights reserved.
