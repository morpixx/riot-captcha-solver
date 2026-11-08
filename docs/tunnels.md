# Туннели: ngrok

Сервер ставится локально (`BIND_HOST=127.0.0.1`), туннель ходит на
`http://127.0.0.1:8000`. Наружу отдаётся только HTTPS-URL ngrok.

В Docker API опубликован на `8000` (или `RIOT_PORT`): туннель на хосте
направляйте на `http://127.0.0.1:8000`. Если туннель запущен в compose,
используйте `http://api:8000`. Подробности: [docker.md](docker.md).

## Быстрый старт

`scripts/start.sh` умеет сам скачать ngrok и поднять туннель:

```bash
START_TUNNEL=1 NGROK_AUTHTOKEN=<TOKEN> bash scripts/start.sh
```

На Kaggle туннель включается автоматически, но `NGROK_AUTHTOKEN` нужен:

```bash
NGROK_AUTHTOKEN=<TOKEN> bash scripts/start.sh
```

Или запусти ngrok вручную:

```powershell
ngrok config add-authtoken <TOKEN>
ngrok http 8000 --inspect=false --log=stdout --log-level=warn
# или со стабильным dev-доменом:
ngrok http --url=https://your-name.ngrok-free.app 8000 --inspect=false --log=stdout --log-level=warn
```

`--inspect=false` отключает тяжёлую инспекцию запросов и снижает CPU,
`--log-level=warn` уменьшает объём логов.

Готовый конфиг: [deploy/ngrok.yml](../deploy/ngrok.yml)

```powershell
ngrok start --config deploy/ngrok.yml riot-api
```

## Webhook на том же URL

Один публичный URL обслуживает и API, и webhook через `/api/v1/webhook`.
Результаты пишутся в `data/output/webhook-results.jsonl`; второй туннель
для порта 9000 не нужен.

В job'е указывай `webhook_url` как локальный адрес **с точки зрения
сервера**: `http://127.0.0.1:8000/api/v1/webhook`. Сервер сам запишет
терминальное событие; публичный URL для этого не нужен.

## Лимиты ngrok free

| Ресурс | Free |
|---|---|
| HTTP-запросов | 20 000 / месяц |
| Исходящий трафик | 1 ГБ / месяц |
| Скорость HTTP | 4 000 запросов / мин |
| TCP-соединений | 5 000 / месяц (100/мин) |
| Онлайн-эндпоинтов | до 3 |
| Dev-домен | 1, не меняется при рестарте агента |
| Interstitial | только HTML в браузере; API не затрагивает |

В браузере при первом открытии URL покажется страница «Visit Site» — это
нормально, нажми её один раз. API-клиенты её не видят:
`scripts/submit_accounts.py` и `scripts/client_example.py` шлют
`ngrok-skip-browser-warning: 1`. Для `curl.exe`:

```powershell
curl.exe -H "ngrok-skip-browser-warning: 1" https://xxxx.ngrok-free.dev/api/v1/health
```

Источник: <https://ngrok.com/docs/pricing-limits/free-plan-limits>.

Для 3500 аккаунтов в режиме «один POST на аккаунт»: 3500 POST и 3500
webhook-доставок. Это несколько тысяч запросов, в месячную квоту укладывается.
1 ГБ трафика тоже хватает: POST'ы и результаты — килобайты, браузерный трафик
идёт через прокси, а не через туннель.

Если отправлять 3500 запросов в одну минуту — это ниже лимита ngrok (4000/мин),
но выше нашего `RATE_LIMIT_PER_MIN`; поставь `RATE_LIMIT_PER_MIN=1200`
(20/с) и клиенту `--rate 10`.

## Режим «один аккаунт — один POST»

```bash
python scripts/submit_accounts.py \
  --url https://<tunnel> \
  --api-key "$API_KEY" \
  --input data/input/accounts.jsonl \
  --out data/output/job-results.jsonl \
  --concurrency 8 --rate 10 \
  --webhook-url http://127.0.0.1:8000/api/v1/webhook
```

Каждая строка `data/input/accounts.jsonl` — объект с `cookies`, `proxy`,
`new_email`, опционально `external_id`/`sitekey`/`headless`. Скрипт:
- шлёт **отдельный** `POST /api/v1/job` на аккаунт;
- держит `--rate` запросов/с и ретраит 429/5xx с `Retry-After`;
- результат пишется в журнал на сервере; постоянного поллинга нет;
- пишет результаты без cookies/прокси.

## Безопасность

ngrok терминирует TLS на своей стороне и **видит cookies/прокси в открытом
виде**. Для передачи чувствительных сессий:
- лучше Tailscale (WireGuard E2E) или свой VPS;
- либо E2E-шифрование payload на уровне приложения (пока не реализовано).

Ключ `X-API-Key` обязателен, `ENABLE_SYNC_RUN=0`, `BIND_HOST=127.0.0.1`.
