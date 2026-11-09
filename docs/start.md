# Запуск в любой среде

Один скрипт ставит Python-зависимости, системные библиотеки Firefox (Linux),
скачивает Camoufox/GeoLite2, готовит `data/`, поднимает API и при
необходимости — публичный туннель.

## Одна команда

Linux/macOS/Git Bash:

```bash
bash scripts/start.sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Первый запуск долгий: скачивается Firefox (~2.5 ГБ) и зависимости.
Локально API будет на `http://127.0.0.1:8000`, ключ печатается в конце.

Для публичного доступа на Linux/macOS/VPS:

```bash
START_TUNNEL=1 NGROK_AUTHTOKEN=<token> bash scripts/start.sh
```

Скрипт поднимет ngrok и напечатает URL. Подробности: [tunnels.md](tunnels.md).

На Kaggle (`KAGGLE_KERNEL_RUN_TYPE`/`/kaggle/working`) туннель включается
автоматически, но токен всё равно нужен:

```bash
%%bash
cd /kaggle/working
git clone https://github.com/USER/REPO.git riot-email
cd riot-email
NGROK_AUTHTOKEN=<token> bash scripts/start.sh
```

Docker — отдельный вариант: [docker.md](docker.md).

## Проверка одного аккаунта

Сначала сделай relogin: старые cookies дадут `Riot session is not valid`, и
`shouldPoll` ты не увидишь.

```bash
# 1. Headless + hCaptcha, без POST и без смены почты
python scripts/check_one.py --label test-01

# 2. Реальный POST: здесь и появится shouldPoll
python scripts/check_one.py --label test-01 --submit
```

Скрипт берёт запись из `data/input/accounts-test.jsonl`, поднимает Camoufox
с `headless=True` и печатает JSON. В логе ищи строку:

```text
[camoufox] account=test-01 headless=True geoip=proxy launch=...
```

Результат:

- `shouldPoll: true` — Riot принял запрос и просит поллинг; это успех.
- `shouldPoll: false` — HTTP 200, но смена не принята; в ответе будет
  `error_code=email_change_not_accepted` и `message`.
- `shouldPoll: null` — поля нет; смотри `status` и `error` (сессия, прокси,
  hCaptcha, таймаут).

`--headed` покажет окно для отладки, `--email` переопределит новый адрес,
`--proxy` — прокси, `--store` — другой файл. Без `--submit` почта не
меняется.

### Аудит всех аккаунтов

Read-only проверка статуса текущей почты без POST:

```bash
python scripts/check_status.py --store data/input/accounts-test.jsonl
```

Вывод показывает `email_status` и `derived_states`. Если у аккаунта
`email_status=not_validated` и `derived_states=[email_verification]`, Riot не
пропустит смену почты: сначала нужно подтвердить **текущий** email. `shouldPoll`
у такого аккаунта будет `false`, сколько бы раз ни повторять запрос. Job с
`submit` теперь проверяет это до hCaptcha и завершается с
`error_code=email_not_verified`; dry-run по-прежнему можно прогнать.

`--label test-01` — только один аккаунт, `--json` — машинный вывод,
`--headed` — показать окно.

## Что делает скрипт

1. `pip install -e .` (пин Camoufox — в `pyproject.toml`).
2. На Linux ставит зависимости Firefox через `playwright install-deps`.
3. Делает `camoufox fetch` и префетч GeoLite2.
4. Создаёт `data/input` и `data/output`.
5. Генерирует `API_KEY`/`WEBHOOK_SECRET`, если они не заданы.
6. Опционально поднимает ngrok.
7. Запускает `python server.py` (или в фоне при `FOREGROUND=0`).

## GitHub

Секреты исключены `.gitignore`: `.env`, `accounts*.jsonl`, прокси,
`data/output`, `jobs.sqlite3`, `.cache`.

```bash
git init
git add .
git status
git commit -m "Riot email automation"
git branch -M main
git remote add origin https://github.com/USER/REPO.git
git push -u origin main
```

Не коммить реальный токен `deploy/ngrok.yml`.

## Аккаунты

Cookie и прокси **не нужно загружать на сервер**. `scripts/start.sh`
поднимает API, а аккаунты отправляются с локальной машины по HTTPS.

Перед отправкой обнови сессии локально. `relogin` по умолчанию идёт через
прокси из записи:

```powershell
$store = "data/input/accounts-test.jsonl"
Get-Content $store | ForEach-Object {
    $r = $_ | ConvertFrom-Json
    python -X utf8 cookie_extractor.py relogin $r.label --store $store --email $r.new_email
}
```

Один аккаунт с явным прокси:

```powershell
python -X utf8 cookie_extractor.py relogin test-01 `
  --store data/input/accounts-test.jsonl `
  --email "<email>" `
  --proxy "http://user:pass@host:port"
```

Перевыбрать прокси из пула:

```powershell
python -X utf8 cookie_extractor.py relogin test-01 `
  --store data/input/accounts-test.jsonl `
  --email "<email>" `
  --new-proxy `
  --proxy-pool data/input/proxies-ports-test.txt
```

Добавить 10-й аккаунт:

```powershell
python -X utf8 cookie_extractor.py add `
  --store data/input/accounts-test.jsonl `
  --label test-10 `
  --email "<email>" `
  --proxy-pool data/input/proxies-ports-test.txt
```

## Отправка job'ов

Скрипт печатает `api key` и URL. С локальной машины:

```powershell
python scripts/submit_accounts.py `
  --url http://127.0.0.1:8000 `
  --api-key "<ключ>" `
  --input data/input/accounts-test.jsonl `
  --out data/output/job-results.jsonl `
  --webhook-url http://127.0.0.1:8000/api/v1/webhook `
  --concurrency 10 `
  --rate 10
```

Для удалённого сервера замени `--url` на публичный URL. `--webhook-url`
оставайся `http://127.0.0.1:8000/api/v1/webhook`: это адрес **с точки
зрения сервера**, он пишет результаты в свой журнал. Туннель для webhook
не нужен.

Если сервер на другом порту, замени `8000` в `--webhook-url`.

## Результаты

На сервере:

```bash
tail -f data/output/webhook-results.jsonl
```

Или с локальной машины:

```powershell
curl -H "X-API-Key: <ключ>" "http://127.0.0.1:8000/api/v1/jobs?limit=20"
```

Для удалённого сервера — публичный URL.

## Опции

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | `0.0.0.0` — доступ из LAN напрямую |
| `PORT` | `8000` | порт API |
| `START_TUNNEL` | `0` локально, `1` на Kaggle | публичный ngrok-туннель |
| `NGROK_AUTHTOKEN` | — | обязательный токен ngrok |
| `NGROK` | из `PATH` или `/tmp/ngrok` | путь к бинарнику ngrok |
| `INSTALL_DEPS` | `1` | `0` — не ставить системные библиотеки |
| `MAX_WORKERS` | `10` | одновременных браузеров |
| `MIN_FREE_MB` | `1500` | минимум свободной RAM для старта job'а |
| `API_KEY` | генерируется | фиксированный ключ клиента |
| `WEBHOOK_SECRET` | генерируется | подпись webhook'ов |
| `FOREGROUND` | `1` | `0` — API в фоне, ячейка/терминал свободны |
| `PYTHON` | `python3`/`python` | какой интерпретатор использовать |

`START_TUNNEL`, `NGROK_AUTHTOKEN`, `FOREGROUND` реализованы в
`scripts/start.sh` для Linux/macOS (на Git Bash можно задать путь через `NGROK`).
На Windows `start.ps1` поднимает локальный API; для публичного доступа
используй Docker, ngrok вручную или WSL.

Пример:

```bash
API_KEY=my-secret MAX_WORKERS=6 BIND_HOST=0.0.0.0 bash scripts/start.sh
START_TUNNEL=1 NGROK_AUTHTOKEN=<token> bash scripts/start.sh
```

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| `no internet` | включи сеть/Internet |
| браузер падает при старте | `INSTALL_DEPS=1`; смотри `playwright install-deps firefox` |
| `public tunnel not ready` | `/tmp/ngrok.log`; проверь `NGROK_AUTHTOKEN` |
| `401` | неверный `--api-key` |
| `502` на `/api/v1/webhook` | не задавай `WEBHOOK_PROXY_URL` без приёмника |
| `database is locked` | не запускай два сервера на одном `data/jobs.sqlite3` |
| медленно / OOM | уменьши `MAX_WORKERS`, подними `MIN_FREE_MB` |
| первый запуск долгий | ~2.5 ГБ Camoufox/Firefox |

## Безопасность

- Публичный туннель без `API_KEY` открывает API всем.
- Ноутбук/сервер с ключами держи приватным.
- Cookie — доступ к аккаунтам; не коммить их.
- `.env`, `accounts*.jsonl`, прокси и `data/output` уже в `.gitignore`.
