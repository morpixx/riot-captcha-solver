# Riot email automation

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![Camoufox](https://img.shields.io/badge/browser-Camoufox%20Firefox-ff7139)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688)

Смена почты Riot через headless **Camoufox (Firefox)** и API-очередь job'ов.
Проект рассчитан на собственные аккаунты и собственные прокси; это не
официальный клиент Riot Games.

**Начало:** [Возможности](#возможности) · [Требования](#требования) ·
[Быстрый старт](#быстрый-старт)

**Работа:** [Как это работает](#как-это-работает) · [API](#api) ·
[Конфигурация](#конфигурация)

**Проект:** [Данные](#данные) · [Структура](#структура) ·
[Документация](#документация) · [Тесты](#тесты) ·
[Безопасность](#безопасность) · [Ограничения](#ограничения) ·
[Лицензия](#лицензия)

## Возможности

- Headless Camoufox с fingerprint Windows, GeoIP по exit-IP прокси и
  отключённым WebRTC.
- Submit через родной виджет Riot: заполнение email и клик по
  `SAVE AND VERIFY` на странице; hCaptcha n-token создаёт сама страница.
  Отдельный invisible-минтер остаётся для dry-run.
- SQLite-очередь: параллелизм, ретраи, идемпотентность по `external_id`,
  отмена job'а, статистика.
- Webhook на том же порту: терминальные события пишутся в
  `data/output/webhook-results.jsonl`, отдельный приёмник не нужен.
- Оптимизация ресурсов Camoufox: Firefox-prefs, humanized-курсор, memory
  guard; блокировка тяжёлых запросов включается через
  `CAMOUFOX_BLOCK_RESOURCES=1`.
- Запуск нативно (Linux/macOS/Windows), в Docker и на удалённом сервере
  через ngrok.

## Требования

| Компонент | Требование |
|:--|:--|
| Python | **3.10+**, 64-bit |
| ОС | Linux, macOS или Windows |
| Диск | ~3 ГБ на первый запуск (Camoufox/Firefox ~2.5 ГБ) |
| RAM | ~1 ГБ на один браузер; для 10 браузеров — ~10 ГБ и запас |
| Публичный доступ | бесплатный `NGROK_AUTHTOKEN` |
| Docker | опционально |

## Быстрый старт

### Linux / macOS / Git Bash

~~~bash
bash scripts/start.sh
~~~

### Windows PowerShell

~~~powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
~~~

Первый запуск ставит зависимости, скачивает Camoufox и GeoLite2, создаёт
`data/input` и `data/output`, поднимает API на `http://127.0.0.1:8000` и
печатает `API_KEY`.

Проверка:

~~~bash
curl http://127.0.0.1:8000/api/v1/health
~~~

`scripts/start.ps1` поднимает локальный API без туннеля. Для публичного
доступа на Windows используй Docker, ngrok вручную или WSL.

### Проверка одного аккаунта

Сначала обнови cookies через `relogin`, иначе будет `Riot session is not valid`.

~~~bash
# headless + hCaptcha, без POST
python scripts/check_one.py --label test-01

# реальный POST и shouldPoll
python scripts/check_one.py --label test-01 --submit
~~~

В логе должно быть `headless=True`; в JSON — `shouldPoll: true/false/null`.
`--headed` показывает окно, `--email`/`--proxy`/`--store` переопределяют
параметры. Без `--submit` почта не меняется.

Аудит всех аккаунтов без POST:

~~~bash
python scripts/check_status.py --store data/input/accounts-test.jsonl
~~~

Если `email_status=not_validated`, сначала подтверди **текущий** email Riot —
иначе `shouldPoll` всегда будет `false`. Job с `submit` проверяет это до
hCaptcha и завершается с `error_code=email_not_verified`, не тратя токен.

### Публичный доступ через ngrok

~~~bash
START_TUNNEL=1 NGROK_AUTHTOKEN=<token> bash scripts/start.sh
~~~

Скрипт сам скачает ngrok (Linux/macOS), поднимет туннель и напечатает
публичный URL. На Kaggle туннель включается автоматически, но токен всё
равно нужен:

~~~bash
NGROK_AUTHTOKEN=<token> bash scripts/start.sh
~~~

Подробно: [docs/start.md](docs/start.md), [docs/tunnels.md](docs/tunnels.md).

### Docker

~~~bash
cp .env.example .env        # Linux/macOS
copy .env.example .env      # Windows
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
~~~

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
   заполняет email и нажимает `SAVE AND VERIFY`; hCaptcha-токен создаёт родной
   виджет страницы, затем уходит POST смены почты.
4. Терминальный статус (`succeeded`, `dead`, `canceled`, `needs_resubmit`)
   уходит в webhook или в same-port журнал.
5. Если Riot просит подтвердить email, подтверждение выполняется отдельно.

Проект не обходит hCaptcha: он получает токен в браузере через родной
виджет Riot и передаёт его Riot. Качество прокси и актуальность cookies
влияют на результат сильнее всего остального.

## API

Авторизация: `X-API-Key: <ключ>` (значение печатает `scripts/start.sh`).
Для нескольких клиентов используй `API_KEYS`.

| Метод | Endpoint | Назначение |
|:--|:--|:--|
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

~~~bash
python scripts/submit_accounts.py \
  --url http://127.0.0.1:8000 \
  --api-key "$API_KEY" \
  --input data/input/accounts-test.jsonl \
  --out data/output/job-results.jsonl \
  --webhook-url http://127.0.0.1:8000/api/v1/webhook \
  --concurrency 10 --rate 10
~~~

`--webhook-url` — адрес **с точки зрения сервера**. Для удалённого сервера
он всё равно остаётся `http://127.0.0.1:8000/api/v1/webhook`; публичный URL
нужен только для `--url`.

Контракт, статусы, идемпотентность и формат webhook:
[docs/jobs-api.md](docs/jobs-api.md).

## Конфигурация

Все переменные — в [.env.example](.env.example). Основные:

| Переменная | По умолчанию | Назначение |
|:--|:--|:--|
| `BIND_HOST` | `127.0.0.1` | интерфейс API; `0.0.0.0` — только осознанно |
| `PORT` | `8000` | порт API |
| `API_KEY` / `API_KEYS` | — | ключ клиента / несколько клиентов |
| `MAX_WORKERS` | `5` (в `start.sh` — 6, в Docker — 4) | одновременные браузеры |
| `MIN_FREE_MB` | `2048` (в `start.sh`/Docker — 1500) | memory guard перед стартом job'а |
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

`scripts/start.sh` по умолчанию берёт 6 воркеров и `MIN_FREE_MB=1500` —
это расчёт на ~1.3–1.5 ГБ пика на один Camoufox. Для 8 ГБ поставь
`MAX_WORKERS=4`. Docker по умолчанию использует 4/1500.

## Данные

- `data/input/` — аккаунты, cookies, прокси, email-файлы. В git не попадают.
- `data/output/` — результаты, webhook-журнал, логи job'ов. В git не попадают.
- `data/jobs.sqlite3` — очередь и статусы; cookies/прокси туда не пишутся.
- В репозитории нет аккаунтов и cookie: их нужно подготовить локально.

Формат аккаунтов и `relogin`/`add`:
[docs/cookie-extractor.md](docs/cookie-extractor.md).

## Структура

~~~text
.
├─ server.py                 # сборка FastAPI и endpoints
├─ cookie_extractor.py       # тонкая точка входа extractor
├─ pyproject.toml            # зависимости, ruff, pytest
├─ LICENSE                   # MIT
├─ src/
│  ├─ api/                   # схемы, auth, webhook, ответы
│  ├─ jobs/                  # очередь, SQLite store, payload, результаты
│  ├─ riot/                  # flow, навигация, состояние, email-форма, minter
│  ├─ browser/               # Camoufox backend и resource tuning
│  ├─ proxy/                 # разбор/проверка прокси
│  └─ extractor/             # команды, пул прокси, данные и CLI
├─ scripts/
│  ├─ start.sh               # универсальный launcher (Linux/macOS)
│  ├─ start.ps1              # Windows launcher
│  ├─ check_one.py           # smoke-тест одного аккаунта
│  ├─ check_status.py        # read-only аудит email_status
│  ├─ submit_accounts.py     # построчная отправка аккаунтов
│  ├─ webhook_receiver.py    # локальный приёмник webhook
│  └─ client_example.py      # пример клиента
├─ docs/                     # архитектура, API, Docker, ngrok, cookies
├─ deploy/ngrok.yml          # конфиг ngrok
├─ data/                     # input/output/БД (не коммитится)
└─ tests/                    # pytest
~~~

## Документация

| Документ | О чём |
|:--|:--|
| [Запуск в любой среде](docs/start.md) | локальный старт, сервер, аккаунты |
| [Jobs API](docs/jobs-api.md) | контракт, статусы, webhook |
| [Docker](docs/docker.md) | образ, compose, volume |
| [ngrok](docs/tunnels.md) | публичный доступ |
| [Архитектура](docs/architecture.md) | слои и модули |
| [Cookie extractor](docs/cookie-extractor.md) | add/relogin/export |
| [Передача клиенту](docs/client-handoff.md) | API для заказчика |
| [Разработка](CONTRIBUTING.md) | тесты, стиль, PR |
| [Безопасность](SECURITY.md) | секреты и ограничения |

## Тесты

~~~bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
~~~

В текущей версии: **126 passed**, `ruff` без замечаний.

## Безопасность

- Публичный API без `API_KEY` открыт всем: всегда задавай ключ.
- `BIND_HOST=127.0.0.1` — безопасный default; `0.0.0.0` только для
  контролируемой сети.
- ngrok терминирует TLS и видит cookies/прокси; не используй его для
  чужих аккаунтов.
- `WEBHOOK_SECRET` подписывает терминальные события.
- `.env`, `accounts*.jsonl`, прокси, `data/output`, `jobs.sqlite3` и `.cache`
  исключены `.gitignore`.

> **Перед публичным запуском:** задайте `API_KEY`, оставьте
> `BIND_HOST=127.0.0.1` и не передавайте `.env` вместе с проектом.

Подробно: [SECURITY.md](SECURITY.md).

## Ограничения

- Riot может менять страницу, hCaptcha и требования к сессии.
- Качество прокси и свежесть cookies не гарантируют результат.
- Подтверждение email после смены может требовать ручного шага.
- Проект не является официальным продуктом Riot Games; используй только
  свои аккаунты и соблюдай ToS.
- Windows-лаунчер не поднимает ngrok; для публичного доступа — Docker,
  ngrok вручную или WSL.

## Лицензия

MIT — см. [LICENSE](LICENSE).
