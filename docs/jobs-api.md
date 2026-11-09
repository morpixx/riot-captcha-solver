# Jobs API (batch-очередь)

Очередь для массовых прогонов: клиент ставит job'ы, сервер сам ограничивает
параллелизм, ретраит транзиентные сбои и отдаёт статусы. Синхронный
`POST /api/v1/run` остаётся для отладки (можно выключить `ENABLE_SYNC_RUN=0`).

Базовый URL: локально `http://127.0.0.1:8000`, в Docker
`http://<host>:8000` (порт меняется через `RIOT_PORT`). Запуск в контейнере:
[docker.md](docker.md).

## Авторизация

Заголовок `X-API-Key`. Один ключ:

```ini
API_KEY=длинный-случайный
```

Несколько клиентов (для fairness и разделения статистики):

```ini
API_KEYS={"key-client1":"client1","key-client2":"client2"}
```

Если не задано ни то, ни другое — API открыт (только для localhost).

## Постановка

```http
POST /api/v1/jobs
X-API-Key: ...
Content-Type: application/json

{
  "priority": 0,
  "webhook_url": "https://client.example/hook",
  "jobs": [
    {
      "external_id": "order-42-acc-001",
      "cookies": [{"name":"ssid","value":"...","domain":".riotgames.com","path":"/"}],
      "proxy": "socks5://user:pass@1.2.3.4:1080",
      "new_email": "new@example.com",
      "headless": true,
      "sitekey": null,
      "webhook_url": null
    }
  ]
}
```

`webhook_url` можно указать на уровне batch или отдельного job'а (job-level
переопределяет batch-level). Если не указан нигде — используется глобальный
`WEBHOOK_URL` из `.env`, если он задан.

Поле `headless` необязательно: по умолчанию сервер запускает Camoufox без
окна. `false` включает видимое окно — это имеет смысл только при отладке на
машине с рабочим столом.

Для теста несколько job'ов могут делить один proxy:

```json
{
  "test_mode": true,
  "jobs": [{"proxy": "socks5://one-proxy:1080", "...": "..."}]
}
```

Это работает только при `ALLOW_SHARED_PROXY_TEST=1` на сервере. В обычном
режиме job'ы с одинаковым proxy выполняются последовательно; в тестовом —
параллельно в пределах `MAX_WORKERS`. Для 10 одновременных браузеров нужен
`MAX_WORKERS>=10` и достаточно RAM. Флаг можно ставить на batch или на отдельный job (`"test_mode": true`).
Построчная отправка поддерживает тот же режим флагом
`scripts/submit_accounts.py --test-mode`.

Домены из `BLOCKED_EMAIL_DOMAINS` (например, `outlook.com`) отклоняются
до запуска браузера с ошибкой в `errors`.

До 500 job'ов за запрос (`BATCH_MAX`). Ответ:

```json
{
  "accepted": 2,
  "duplicates": 0,
  "errors": [{"index": 2, "external_id": "x", "error": "proxy host must be a public address"}],
  "jobs": [{"id": "...", "external_id": "order-42-acc-001", "status": "queued", "attempts": 0, ...}]
}
```

`external_id` — идемпотентность: повторный submit с тем же id не создаёт
вторую джобу. Если джоба уже `dead`/`canceled`/`needs_resubmit`, повторный submit
возвращает её в очередь с новыми cookies/прокси (это же и есть замена мёртвого прокси).

## Статусы и webhook

```http
POST /api/v1/job            # один аккаунт — один POST
POST /api/v1/webhook        # same-port журнал (data/output/webhook-results.jsonl)
GET /api/v1/jobs?status=queued,running,retry_wait&limit=500
GET /api/v1/jobs?updated_since=1760000000.0&limit=500   # дельта после рестарта
GET /api/v1/jobs/{id}
POST /api/v1/jobs/{id}/cancel
GET /api/v1/stats
```

`/api/v1/stats` возвращает в том числе `should_poll: {"true": N, "false": M}` —
сколько завершённых job'ов Riot принял и сколько отклонил.

Основной способ получить результат — `webhook_url` в job'е/batch. Сервер
отправляет `POST` только при переходе в терминальный статус (`succeeded`,
`dead`, `canceled`, `needs_resubmit`) с payload'ом:

```json
{
  "event": "job.terminal",
  "id": "...",
  "external_id": "...",
  "batch_id": "a1b2c3d4e5f6",
  "status": "succeeded",
  "shouldPoll": true,
  "riot_status": 200,
  "error_code": null,
  "error": null,
  "details": {"message": "email_verification_sent", "mode": "update"},
  "duration_ms": 12345,
  "average_duration_ms": 11800.5,
  "batch_total": 9,
  "batch_terminal": 7,
  "batch_succeeded": 1,
  "batch_failed": 6,
  "batch_remaining": 2,
  "batch_average_duration_ms": 11800.5,
  "batch_average_success_duration_ms": 12345.0
}
```

- `average_duration_ms` — среднее по **всем успешным** job'ам этого клиента;
- `batch_*` — статистика текущей пачки: total/terminal/succeeded/failed/remaining;
- `batch_average_duration_ms` — среднее время обработки **всех терминальных**
  job'ов пачки (включая dead/needs_resubmit);
- `batch_average_success_duration_ms` — среднее только по `succeeded`;
- на каждом webhook'е статистика накопленная; после последнего job'а
  `batch_remaining=0`.

`batch_id` можно передать в batch (`POST /api/v1/jobs`) или в отдельном
job'е. `scripts/submit_accounts.py` генерирует его сам и печатает в строке
`batch=...`.

URL хранится в памяти вместе с cookies, в SQLite не пишется. При
`WEBHOOK_SECRET` подпись тела — в `X-Webhook-Signature: sha256=<hex>`; доставка
повторяется до `WEBHOOK_RETRIES` раз.

### Один публичный URL для API и webhook

Если есть только один туннель, укажите webhook на тот же хост:

```text
https://<api-host>/api/v1/webhook
```

По умолчанию (`WEBHOOK_PROXY_URL` пуст) сервер **сам принимает** это событие на
том же порту и дописывает JSON в `data/output/webhook-results.jsonl`. Второй
ngrok/приёмник не нужен. Если задан `WEBHOOK_PROXY_URL`, событие вместо этого
пересылается на локальный приёмник; цель ограничена localhost, чтобы endpoint
не стал открытым прокси. Ошибка приёмника даёт `502` и повтор по
`WEBHOOK_RETRIES`.

Если `/api/v1/webhook` доступен публично, задайте `WEBHOOK_SECRET`: сервер
проверит `X-Webhook-Signature` и не даст писать в журнал посторонним.

GET-эндпоинты остаются только для ручной диагностики; клиент их постоянно не
опрашивает.

`POST /api/v1/job` принимает один объект job'а (тот же формат, что элемент
`jobs`) и возвращает `JobView` со статусом 202. Это удобный режим для туннеля,
когда клиент шлёт по одному запросу на аккаунт. 

Статусы: `queued`, `running`, `retry_wait`, `succeeded`,
`dead`, `canceled`, `needs_resubmit`.

`succeeded` содержит `shouldPoll`, `riot_status` и `details` (тело ответа Riot);
`details` отдаётся и в списке, и в одиночном GET. Fingerprint и GeoIP всегда
включены на уровне браузера; в запросе для них нет параметров.
`dead` — `error_code` (`session_invalid`, `proxy_error`, `captcha_failed`,
`browser_fingerprint`, `timeout`, `unknown`) и `error`.

Если Riot ответил `200`, но `shouldPoll=false`, смена email не принята: job
получает `needs_resubmit`, `error_code=email_change_not_accepted`, а в `error`
и `details` сохраняется причина от Riot. Клиент может подтвердить текущий/pending-
адрес, дождаться истечения заявки или прислать job повторно с тем же
`external_id` и новым `new_email` — например, gmx/gmail. Это не ретраится
автоматически.

Ретраи: до `JOB_ATTEMPTS` (3) с backoff `RETRY_BASE`·2^n (30 с, 60 с) + jitter.
Не ретраятся: `session_invalid`, `proxy_error` — такие джобы сразу `dead`.

## Лимиты

- `MAX_QUEUE` (5000): при переполнении `429` + `Retry-After`;
- `RATE_LIMIT_PER_MIN` (1200): запросов `/jobs` и `/job` в минуту на клиента;
- `MAX_WORKERS`: одновременных браузеров;
- `MIN_FREE_MB` (2048): при нехватке RAM диспетчер ставит очередь на паузу
  (`paused` в `/stats`);
- `MAX_PER_CLIENT` (0 = MAX_WORKERS): fairness между клиентами.

## Рестарт и секреты

Cookies, прокси-креды и email живут только в памяти процесса; SQLite хранит
метаданные job'ов (id, статус, попытки, длительность, ошибки). После рестарта
незавершённые джобы получают `needs_resubmit` (`error_code=payload_missing`) —
клиент присылает их повторно с тем же `external_id`. Второй случай
`needs_resubmit` — отказ Riot при `shouldPoll=false`
(`error_code=email_change_not_accepted`).

**Один процесс на одну `JOBS_DB`.** Второй uvicorn-worker с той же базой будет
красть джобы у первого: запускай `--workers 1`. Синхронный `/run` использует
отдельный пул на `MAX_WORKERS` — не запускай его одновременно с батчем.

## Клиентский таймаут

Сами job-запросы короткие; долгий прогон идёт в очереди. Штатно клиент ждёт
webhook. Если webhook не дошёл, конкретный job можно проверить вручную через
`GET /api/v1/jobs/{id}`; постоянный polling клиентом не используется.
