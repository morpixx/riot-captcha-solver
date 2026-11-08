# Архитектура проекта

Документ для разработчика, который впервые открыл репозиторий. Он объясняет,
где проходит граница между HTTP-слоем, бизнес-логикой браузера и хранением
данных, и почему модули разделены именно так.

## Принципы

1. **Тонкие точки входа.** server.py и cookie_extractor.py можно запускать
   напрямую, но основная логика лежит в src/.
2. **Одна причина для изменения.** Pydantic-схемы не знают о SQLite, очередь не
   знает о FastAPI, браузерный слой не пишет файлы аккаунтов.
3. **Секреты не покидают память.** Cookie, proxy-креды и email не попадают в
   SQLite и в ответы API. Всё, что пишется в лог или ошибку, проходит через
   src.security.
4. **Простые функции вместо фреймворка.** Нет DI-контейнера, фабрик и
   менеджеров: там, где достаточно функции или dataclass, используется она.

## Запуск

- Локально: `python server.py` (порт из `BIND_HOST`/`PORT`).
- Docker: `docker compose up -d --build`, порт 8000, состояние в `data/`.
  Образ собирает Camoufox/Firefox и работает от non-root пользователя.
  Подробности: [docker.md](docker.md).

## Потоки данных

### Синхронный прогон

~~~text
CLI / POST /api/v1/run
        |
        v
src.config.Config + Account
        |
        v
src.riot.flow.run()
  -> src.browser.browser.open_browser()  (Camoufox)
  -> src.riot.minter.mint_in_page()
  -> POST с открытой страницы
        |
        v
dict {status, shouldPoll, body}
        |
        v
src.jobs.result.interpret_result()
        |
        v
RunResponse / результат CLI
~~~

### Очередь job'ов

~~~text
POST /api/v1/job(s)
        |
        v
src.api.models (валидация)
        |
        v
src.jobs.queue.JobQueue.submit()
  -> SQLite: только метаданные job'а
  -> память: cookies, proxy, webhook URL
        |
        v
dispatcher -> ThreadPoolExecutor -> src.jobs.runner.run_account()
        |
        v
src.jobs.result.interpret_result()
        |
        +--> SQLite: terminal status
        +--> src.api.webhooks: POST клиенту
~~~

## Слои и модули

### HTTP и контракты

- **server.py** — создание FastAPI, lifespan, auth, endpoints, фоновые пулы.
- **src/api/models.py** — все Pydantic-модели и валидация URL. Здесь нет
  доступа к БД или браузеру.
- **src/api/webhooks.py** — сериализация, HMAC-подпись и повторная доставка
  terminal-событий. URL и payload живут только в памяти.

### Очередь

- **src/jobs/queue.py** — состояние job'ов, ретраи, fairness, SQLite-схема и
  диспетчер. Это единственное место, где меняется статус job'а.
- **src/jobs/models.py** — dataclass'ы Job/JobSpec и допустимые статусы.
- **src/jobs/errors.py** — преобразование исключения в error_code и решение
  retryable/not retryable.
- **src/jobs/result.py** — единственное место с правилом
  shouldPoll=false => needs_resubmit. Его используют и API, и очередь.
- **src/jobs/runner.py** — тонкий адаптер, чтобы очередь не зависела от деталей
  src/riot/flow.py.

### Браузер и Riot

- **src/browser/browser.py** — загрузка cookie и Camoufox entrypoint.
- **src/browser/camoufox_browser.py** — Firefox-backend Camoufox: fingerprint,
  proxy, GeoIP, headless и временный профиль; тот же интерфейс context/page.
- **src/browser/camoufox_boost.py** — оптимизация ресурсов (Python-порт Node-бустера Camoufox):
  Firefox-prefs, блокировка тяжёлых запросов, human-поведение.
- **src/riot/flow.py** — сценарий: открыть страницу, проверить сессию,
  сминтить токен, выполнить POST.
- **src/riot/minter.py** — JavaScript для hCaptcha на реальном origin.

### Cookie extractor

- **cookie_extractor.py** — команды add/relogin/list/export и работа с пулом
  прокси. Содержит бизнес-логику пакетного добавления.
- **src/extractor/cli.py** — только argparse и интерактивное меню.
  Импортируется лениво, чтобы запуск скрипта не дублировал модуль.
- **src/extractor/browser.py** — ожидание ручного входа, проверка сессии,
  обнаружение Cloudflare-блокировки.
- **src/extractor/data.py** — нормализация cookie, CookieStore (JSONL),
  валидация email/external_id, экспорт jobs.
- **src/proxy/pool.py** и **src/proxy/scan.py** — проверка и ранжирование
  прокси; не знают об аккаунтах.

### Инфраструктура

- **src/config.py** — env-флаги, Config/Account, пути data/input и data/output.
- **src/security.py** — redaction, маскирование proxy, email_tag (общий модуль).
- **data/** — входные файлы в data/input, результаты в data/output,
  SQLite-база заданий в data/jobs.sqlite3; это состояние сервера, а не код.

## Потокобезопасность

- JobQueue использует отдельные блокировки для SQLite (_db_lock) и
  in-memory состояния (_payloads, _proxy_busy, webhook URL).
- Payload публикуется до INSERT: диспетчер может взять job сразу после
  коммита, но cookies уже будут в памяти.
- Webhook-очередь имеет одного воркера. Медленный получатель не блокирует
  job'ы, но и не создаёт неограниченное число потоков.
- SQLite рассчитан на один процесс. Не запускайте несколько uvicorn workers
  на одной JOBS_DB.

## Куда добавлять изменения

- Новый HTTP-эндпоинт: модель в src/api/models.py, обработчик в server.py.
- Новый статус/правило job'а: src/jobs/models.py + src/jobs/queue.py + тест.
- Новое правило ответа Riot: src/jobs/result.py (не дублировать в server/queue).
- Новый способ доставки результата: рядом с src/api/webhooks.py, не внутри очереди.
- Новый шаг браузерного сценария: src/riot/flow.py и/или отдельный модуль в src/.
- Новый формат экспорта cookie: src/extractor/data.py, CLI не должен знать
  детали формата.

## Проверки

~~~bash
python -m pytest -q
python -m ruff check .
~~~

Тесты не запускают браузер и не требуют реальных прокси. Для новых правил
добавляйте unit-тест рядом с существующими: tests/test_job_result.py,
tests/test_webhooks.py, tests/test_job_queue.py.