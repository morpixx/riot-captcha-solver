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
4. **Простые функции вместо фреймворка.** Нет DI-контейнера и менеджеров:
   там, где достаточно функции или dataclass, используется она.

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
  -> заполнение email + клик по SAVE AND VERIFY (родной hCaptcha widget)
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

- **server.py** — единственная точка сборки: создание FastAPI, lifespan,
  endpoints и фоновые пулы. Запускается как `python server.py`.
- **src/api/models.py** — все Pydantic-модели и валидация URL. Здесь нет
  доступа к БД или браузеру.
- **src/api/auth.py** — чтение API_KEY/API_KEYS и FastAPI-зависимость,
  которая превращает X-API-Key в client_id.
- **src/api/run.py** — отображение результата браузера в RunResponse.
- **src/api/webhooks.py** — сериализация, HMAC-подпись и повторная доставка
  terminal-событий. URL и payload живут только в памяти.
- **src/api/webhook_receiver.py** — приём webhook на том же порту: проверка
  подписи, запись в JSONL или пересылка на локальный приёмник.

### Очередь

- **src/jobs/queue.py** — admission, ретраи, fairness, диспетчер и terminal
  callbacks. Это единственное место, где принимается решение о статусе job'а.
- **src/jobs/store.py** — SQLite-схема, все запросы и атомарный claim. Не знает
  о браузере, FastAPI и webhook'ах.
- **src/jobs/payload.py** — валидация JobSpec и in-memory payload с cookies.
  Секреты не попадают в store.
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
  Firefox-prefs, human-поведение; блокировка тяжёлых запросов — опционально
  (`CAMOUFOX_BLOCK_RESOURCES=1`).
- **src/riot/flow.py** — только оркестрация одного прогона: браузер, порядок
  проверок, dry-run или submit, итоговый `{status, shouldPoll, body}`.
- **src/riot/navigation.py** — origin-проверки и ожидание цепочки редиректов
  account -> authenticate -> account.
- **src/riot/state.py** — состояние аккаунта через API самой страницы: сессия,
  `email_status`, уже pending-запрос на смену почты.
- **src/riot/email_form.py** — карточка email: заполнить поле, нажать
  `SAVE AND VERIFY` и получить ответ POST.
- **src/riot/minter.py** — отдельный invisible-минтер для dry-run; submit идёт
  через родной виджет страницы, потому что только его n-token Riot принимает.

### Cookie extractor

- **cookie_extractor.py** — тонкая точка входа и фасад для старых импортов;
  сама логика лежит в `src/extractor/`.
- **src/extractor/cli.py** — только argparse и интерактивное меню.
- **src/extractor/commands.py** — команды add/relogin/add-batch/list/remove/
  export/proxy-scan/assign-proxies.
- **src/extractor/proxies.py** — пул прокси, выбор свободного и ведение
  `data/input/proxies-*.txt`.
- **src/extractor/browser.py** — ожидание ручного входа, проверка сессии,
  обнаружение Cloudflare-блокировки.
- **src/extractor/data.py** — нормализация cookie, CookieStore (JSONL),
  валидация email/external_id, экспорт jobs.
- **src/extractor/ui.py** — prompt/confirm и таблица аккаунтов без секретов.
- **src/proxy/pool.py** и **src/proxy/scan.py** — проверка и ранжирование
  прокси; не знают об аккаунтах.

### Инфраструктура

- **src/config.py** — env-флаги, Config/Account, пути data/input и data/output.
- **src/security.py** — redaction, маскирование proxy, email_tag (общий модуль).
- **data/** — входные файлы в data/input, результаты в data/output,
  SQLite-база заданий в data/jobs.sqlite3; это состояние сервера, а не код.

## Потокобезопасность

- JobStore использует threading.RLock вокруг одного SQLite-соединения;
  JobQueue держит отдельную блокировку для in-memory состояния (_payloads,
  _proxy_busy, webhook URL).
- Payload публикуется до INSERT: диспетчер может взять job сразу после
  коммита, но cookies уже будут в памяти.
- Webhook-очередь имеет одного воркера. Медленный получатель не блокирует
  job'ы, но и не создаёт неограниченное число потоков.
- SQLite рассчитан на один процесс. Не запускайте несколько uvicorn workers
  на одной JOBS_DB.

## Куда добавлять изменения

- Новый HTTP-эндпоинт: модель в src/api/models.py, обработчик в server.py.
- Новый статус/правило job'а: src/jobs/models.py + src/jobs/queue.py + тест.
- Новый SQL-запрос или поле job'а: src/jobs/store.py (+ миграция в _migrate).
- Новое правило допуска job'а: src/jobs/payload.py.
- Новое правило ответа Riot: src/jobs/result.py (не дублировать в server/queue).
- Новый способ доставки результата: рядом с src/api/webhooks.py, не внутри очереди.
- Новый шаг браузерного сценария: отдельный модуль в src/riot/, flow.py только
  вызывает его по порядку.
- Новая команда cookie extractor: src/extractor/commands.py + cli.py.
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