# Доступ к API смены почты

**Base URL:** `https://YOUR-URL.ngrok-free.app`
**Ключ:** `X-API-Key: <API_KEY>` (выдаётся отдельно, в URL не передавать)

Если API запущен в Docker, базовый URL — `http://<host>:8000`
(порт задаётся `RIOT_PORT` в `docker-compose.yml`). Подробности:
[docker.md](docker.md).

Все методы, кроме `GET /api/v1/health`, требуют заголовок `X-API-Key`.
Без ключа — `401`.

**Через ngrok:** добавляйте `ngrok-skip-browser-warning: 1` (любое значение).
Без него браузерный клиент может получить HTML-заглушку ngrok вместо JSON.
`scripts/submit_accounts.py` и `scripts/client_example.py` уже отправляют этот заголовок.

## 1. Один аккаунт — один POST

```http
POST /api/v1/job
X-API-Key: <API_KEY>
Content-Type: application/json

{
  "external_id": "order-42-acc-001",
  "cookies": [
    {"name": "ssid", "value": "...", "domain": ".riotgames.com", "path": "/"}
  ],
  "proxy": "socks5://user:pass@1.2.3.4:1080",
  "new_email": "new@example.com",
  "headless": true
}
```

`external_id` — ваш идентификатор; по нему работает идемпотентность. Повторный
POST с тем же `external_id` не запустит вторую смену почты.

Ответ `202`:

```json
{
  "id": "5d0c...",
  "external_id": "order-42-acc-001",
  "status": "queued",
  "attempts": 0,
  "shouldPoll": null,
  "riot_status": null,
  "error_code": null,
  "error": null,
  "details": null,
  "duration_ms": null,
  "created_at": 1793918761.9,
  "updated_at": 1793918761.9,
  "started_at": null,
  "finished_at": null
}
```

Дальше ничего опрашивать не нужно: передайте `webhook_url` (раздел 3), и сервер
сам пришлёт результат, когда job завершится. Если webhook не настроен, статус
можно забрать вручную:

```http
GET /api/v1/jobs/{id}
X-API-Key: <API_KEY>
```

Пока `status` = `queued` / `running` / `retry_wait` — подождите. Терминальные:
`succeeded`, `dead`, `canceled`, `needs_resubmit`.

## 2. Пачка аккаунтов (если удобнее)

```http
POST /api/v1/jobs
X-API-Key: <API_KEY>

{
  "priority": 0,
  "jobs": [ { ... }, { ... } ]
}
```

До 500 job'ов за запрос. Ответ: `accepted`, `duplicates`, `errors` (по индексам)
и список `jobs` с `id`/`status`. Дубликаты по `external_id` не создаются.

## 3. Webhook: результат приходит сам

Передайте `webhook_url` при постановке job'а — сервер отправит POST, когда job
перейдёт в терминальный статус (`succeeded`, `dead`, `canceled`,
`needs_resubmit`). Клиенту не нужно поллить.

Для одного аккаунта:

```http
POST /api/v1/job
X-API-Key: <API_KEY>

{
  "external_id": "order-42-acc-001",
  "webhook_url": "https://client.example/hook",
  "cookies": [{"name": "ssid", "value": "...", "domain": ".riotgames.com", "path": "/"}],
  "proxy": "socks5://user:pass@1.2.3.4:1080",
  "new_email": "new@example.com"
}
```

Для пачки можно указать один URL на весь batch (job-level `webhook_url`
переопределяет batch-level):

```http
POST /api/v1/jobs
X-API-Key: <API_KEY>

{
  "webhook_url": "https://client.example/hook",
  "priority": 0,
  "jobs": [ { ... }, { ... } ]
}
```

Payload webhook'а:

```json
{
  "event": "job.terminal",
  "id": "5d0c...",
  "external_id": "order-42-acc-001",
  "batch_id": "a1b2c3d4e5f6",
  "status": "succeeded",
  "shouldPoll": true,
  "riot_status": 200,
  "error_code": null,
  "error": null,
  "details": {
    "message": "email_verification_sent",
    "mode": "update",
    "shouldPoll": true
  },
  "duration_ms": 12345,
  "average_duration_ms": 11800.5,
  "created_at": 1793918761.9,
  "updated_at": 1793918800.1,
  "started_at": 1793918765.0,
  "finished_at": 1793918800.1
}
```

- `shouldPoll` — `true`, `false` или `null` (если Riot не вернул поле);
- `details` — тело ответа Riot, в том числе `message`/`mode`;
- для `dead` смотрите `error_code` и `error`, `shouldPoll` будет `null`;
- `canceled` и `needs_resubmit` тоже приходят вебхуком.

Если Riot вернул `200`, но `shouldPoll=false`, это **не успех**: job получает
`status=needs_resubmit` и `error_code=email_change_not_accepted`. Riot не создал
pending-смену — обычно потому, что уже есть заявка на другой адрес или текущий
email не подтверждён. Подтвердите текущий/pending-адрес, дождитесь истечения
заявки или пришлите job повторно с тем же `external_id` и новым `new_email`
(например, gmx/gmail); автоматического ретрая не будет.

Заголовки запроса:

```http
Content-Type: application/json
X-Webhook-Event: job.terminal
X-Webhook-Id: 5d0c...
X-Webhook-Signature: sha256=<hex>   # если на сервере задан WEBHOOK_SECRET
```

`X-Webhook-Signature` — HMAC-SHA256 от **сырого тела** запроса с секретом
`WEBHOOK_SECRET`. Сравнивайте подпись до разбора JSON. Отвечайте `2xx` сразу;
при ошибке/таймауте сервер повторит доставку (`WEBHOOK_RETRIES`).

URL должен быть доступен с сервера API. Это может быть ваш внешний приёмник
(любой хост/порт) или same-port адрес API:

```text
https://<api-host>/api/v1/webhook
```

Для same-port режима **отдельный приёмник не нужен**: сервер принимает
событие на том же порту и дописывает его в
`data/output/webhook-results.jsonl` (по строке на job). Если задан
`WEBHOOK_PROXY_URL`, событие вместо этого пересылается на локальный
приёмник; это опционально.

Опциональный отдельный приёмник: `python scripts/webhook_receiver.py --port 9000`
(пишет в свой `--out` и проверяет подпись при `--secret`).

Адреса webhook'ов хранятся только в памяти процесса. После рестарта сервера
незавершённые job'ы получают `needs_resubmit`, и их нужно прислать повторно с
тем же `external_id` и `webhook_url`. Повторный submit уже завершённой job'ы
вернёт её без нового webhook'а — результат можно забрать через `GET /jobs/{id}`.

## 4. Ручная проверка статуса

Постоянный polling клиентом не используется. Если webhook потерян или нужно
разобраться вручную, можно точечно запросить job:

```http
GET /api/v1/jobs/{id}
X-API-Key: <API_KEY>
```

Это диагностический запрос, а не рабочий цикл клиента. Штатный результат
приходит только webhook'ом.

## 5. Отмена

```http
POST /api/v1/jobs/{id}/cancel
X-API-Key: <API_KEY>
```

Можно отменить `queued`/`retry_wait`. `running` — `409` (дождитесь завершения).

## 6. Статусы и ошибки

| status | Значение |
|---|---|
| `queued` | в очереди |
| `running` | браузер уже работает |
| `retry_wait` | временный сбой, будет повтор |
| `succeeded` | POST в Riot выполнен (смотрите `shouldPoll`/`details`) |
| `dead` | окончательный сбой, смотрите `error_code` |
| `canceled` | отменена |
| `needs_resubmit` | cookies потеряны при рестарте или Riot отклонил смену (`shouldPoll=false`), пришлите job заново |

`error_code`: `session_invalid` (cookies невалидны), `proxy_error` (прокси
недоступен), `captcha_failed`, `timeout`, `browser_fingerprint`, `unknown`,
`payload_missing` (cookies потеряны после рестарта), `email_change_not_accepted`
(Riot ответил `shouldPoll=false`; подтвердите текущий/pending-адрес).

HTTP-коды: `400` — неверный прокси/email; `401` — ключ; `404` — чужая/неизвестная
джоба; `409` — конфликт отмены; `413` — слишком большая пачка; `422` — битый JSON;
`429` — лимит/очередь, смотрите `Retry-After`.

## 7. Лимиты

- до `1200` запросов `/job` + `/jobs` в минуту на клиента;
- очередь до `5000` job'ов; при переполнении `429`;
- одновременно сервер запускает `MAX_WORKERS` браузеров (задаётся на сервере);
- через ngrok free: `20000` HTTP-запросов/месяц и `1 ГБ` трафика; webhook
  расходует квоту только на доставку результата, без постоянного поллинга.

Ретраи: `429` и `5xx` — повторяйте с тем же `external_id` и `Retry-After`.
`400/401/404/422` — не повторять.

## 8. Требования к данным

- `cookies` — массив объектов `{name, value, domain, path}`; опционально
  `expires`, `httpOnly`, `secure`. Сессия должна быть валидной на момент прогона.
  Экспорт Cookie-Editor подходит как есть: вместо `expires` там `expirationDate`,
  сервер принимает оба варианта, лишние поля (`sameSite`, `hostOnly`, `session`) игнорирует.
- `proxy` — `socks5://`, `socks5h://`, `http://` или `https://`, можно с
  `user:pass@`. Лучше отдельный прокси на аккаунт; локальные/приватные адреса
  отклоняются.
- `new_email` — валидный email.
- `headless` — для Camoufox штатный режим `true`; headed можно включать
  точечно для отладки.
- Fingerprint и GeoIP на сервере всегда включены; отключить их через API нельзя.

## 9. Готовый клиент

Основной пример — только webhook:

```bash
python scripts/client_example.py job.json
```

Он поднимает локальный приёмник `http://127.0.0.1:9000/hook`, передаёт его в
`webhook_url`, ждёт POST от сервера и печатает `shouldPoll`. Если API и клиент
на разных машинах, приёмник нужно выставить наружу и указать публичный URL:

```bash
python scripts/client_example.py job.json --public-url https://client.example/hook
```

Если webhook не пришёл за `--timeout`, клиент завершается с ошибкой; polling
больше не используется.

Массовая построчная отправка:

```bash
python scripts/submit_accounts.py \
  --url https://YOUR-URL.ngrok-free.app \
  --api-key <API_KEY> \
  --input data/input/accounts.jsonl \
  --out data/output/job-results.jsonl \
  --webhook-url https://client.example/hook
```

`data/input/accounts.jsonl` — по одному объекту `{external_id, cookies, proxy, new_email}`
на строку. Скрипт шлёт по одному POST на аккаунт и ретраит `429/5xx`; результат
приходит только на webhook. Cookies/прокси в выходной файл не пишутся.

## 10. Безопасность

- Ключ — только в заголовке `X-API-Key`, не в URL и не в логах.
- Не передавайте ключ третьим лицам; при утечке он меняется на сервере, старый
  перестаёт работать после рестарта.
- Cookies — это доступ к аккаунту: не логируйте их, не отправляйте в чаты.
- `webhook_url` хранится только в памяти сервера и не попадает в SQLite/API-ответы.
  Используйте HTTPS и проверяйте `X-Webhook-Signature`, если задан `WEBHOOK_SECRET`.
- Туннель ngrok терминирует TLS и видит тела запросов; для
  продакшена с реальными сессиями используйте Tailscale или E2E-шифрование.
