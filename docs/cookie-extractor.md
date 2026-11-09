# Куки-экстрактор: несколько аккаунтов → один список

Скрипт `cookie_extractor.py` открывает **настоящий браузер** Camoufox
(Firefox), даёт войти в аккаунт руками
(пароль, 2FA, hCaptcha), забирает cookie домена `riotgames.com` и складывает
все аккаунты в один `data/input/accounts.jsonl`. Одна строка — один аккаунт.

Формат строки совпадает с job'ом для `POST /api/v1/jobs`, поэтому файл можно
сразу отдать `scripts/submit_accounts.py` или отправить в API пачкой.

> Экстрактор интерактивный и открывает видимое окно. Его запускают на рабочей
> машине, а не в контейнере API. После входа положите `accounts.jsonl` в
> `data/input/` (в Docker это `./data/input`) и отправляйте на сервер.

## Быстрый старт

```bash
cd project
python -m pip install -e .
python cookie_extractor.py        # браузер Camoufox скачается при первом запуске
```

Откроется меню:

```
1) Добавить аккаунт (открыть браузер)
2) Перелогинить аккаунт
3) Показать список
4) Экспорт для сервера
5) Удалить аккаунт
6) Добавить несколько подряд (auto label + proxy pool)
0) Выход
```

При добавлении спросит:

- **Название** — можно нажать Enter, тогда будет `acc-01`, `acc-02` и т.д.
- **Новая почта** — та, на которую сервер будет менять почту Riot.
- **Proxy** — если есть `data/input/proxies-top.txt` (или `data/input/proxies.txt`), выберется самый быстрый свободный;
  иначе можно оставить пустым, но сервер требует proxy для job'а.
  Лучше использовать тот же proxy, через который аккаунт будет работать:
  сессия и IP будут согласованы.

После этого откроется окно браузера. Войдите в аккаунт, дождитесь загрузки
страницы аккаунта. Скрипт сам увидит cookie `ssid`, проверит сессию через
`GET /api/account/v1/user` и сохранит запись. Окно можно закрыть после
сообщения `[+] добавлен аккаунт ...`.

## Команды без меню

```bash
# добавить / обновить аккаунт
python cookie_extractor.py add --label acc-01 --email new@example.com \
    --proxy socks5://user:pass@1.2.3.4:1080

# если не передать label/email/proxy — спросит интерактивно
python cookie_extractor.py add

# один аккаунт без proxy (прямое подключение)
python cookie_extractor.py add --store test-direct.jsonl --label test-direct \
    --email bwjntazn@telegmail.com --no-proxy --max-page-load 0

# заново войти в уже сохранённый аккаунт (email/proxy подставятся)
python cookie_extractor.py relogin acc-01

# список без раскрытия значений cookie
python cookie_extractor.py list

# удалить
python cookie_extractor.py remove acc-01

# экспорт
python cookie_extractor.py export --format jsonl
python cookie_extractor.py export --format json --out accounts.export.json
python cookie_extractor.py export --format batch --out batch.json
```

`--store` у всех команд задаёт другой файл:

```bash
python cookie_extractor.py --help
python cookie_extractor.py list --store D:\secrets\riot-accounts.jsonl
```

## Пачка proxy и автоматические label

Можно не вводить label и proxy руками. Для работы удобнее `data/input/proxies-top.txt`
(128 самых быстрых SOCKS5 из собранного списка), полный список лежит в
`data/input/proxies.txt`. Формат — один proxy на строку, адреса — в `data/input/emails.txt`:

```text
# data/input/proxies-top.txt
socks5://user:pass@1.2.3.4:1080
socks5://user:pass@5.6.7.8:1080
http://user:pass@9.10.11.12:8080
```

```text
# data/input/emails.txt
r7f3k2@example.com
m2p9xq@example.com
```

Запуск:

```bash
python cookie_extractor.py add-batch --emails data/input/emails.txt --count 2
```

Что произойдёт:

- label сгенерируются как `acc-01`, `acc-02`, ... (свободные номера);
- если рабочих proxy меньше, чем аккаунтов, `add-batch` **не остановится**,
  а продолжит искать в `--auto-proxy-pool` (по умолчанию
  `data/input/proxies-socks5-all.txt`) и начнёт запуск только когда наберёт нужное
  количество;
- для каждого аккаунта будет выбран **свой** proxy;
- proxy проверяются: сначала TCP-connect, потом реальный HTTPS-запрос через
  proxy на `account.riotgames.com`; выбирается самый быстрый рабочий;
- уже занятые proxy из `data/input/accounts.jsonl` пропускаются;
- для каждого аккаунта открывается отдельный Camoufox-профиль, и вы входите руками.

Логины/пароли скрипт не принимает и не читает — только email, label и proxy.

Порядок автоподхвата: `data/input/proxies-working.txt` → `data/input/proxies-top.txt` →
`data/input/proxies.txt`. То есть после `proxy-scan` ничего указывать не нужно. Ещё
можно задать путь в `.env`:

```ini
PROXY_POOL=data/input/proxies-top.txt
# PROXY_TEST_URL=https://account.riotgames.com/
```

Файлы:

- `data/input/proxies-working.txt` — проверенные рабочие proxy, используется автоматически;
- `data/input/proxies-top.txt` — 128 быстрых SOCKS5;
- `data/input/proxies.txt` — все 12 304 поддерживаемых proxy (SOCKS5 + HTTP + HTTPS);
- `data/input/proxies-socks5-all.txt` — только все SOCKS5 (5 019);
- `data/input/proxies-socks4.txt` — SOCKS4 (380), текущий Camoufox/скрипт их не принимает;
- `data/input/proxies-bad.txt` — сюда автоматически попадают заблокированные/битые proxy
  (`scheme://[user@]host:port`, без паролей).

Если рабочих proxy меньше, чем аккаунтов, ищите их постепенно:

```bash
python cookie_extractor.py proxy-scan \
    --proxy-pool data/input/proxies-socks5-all.txt --need 16
```

Сканер идёт батчами и не проверяет все 5 019 сразу. Результат пишется в
`data/input/proxies-working.txt`, после чего `add-batch` подхватит его сам.

Не подавайте полный `data/input/proxies.txt` в `add-batch` без необходимости: скрипт
проверяет весь пул, это долго. Начните с `data/input/proxies-top.txt`.

### Одна пачка с одним proxy

Если выдан только один proxy, его можно явно использовать для всех аккаунтов
пачки. В этом режиме пул не проверяется, а `--count` задаёт число аккаунтов:

```bash
python cookie_extractor.py add-batch \
    --store data/input/accounts-new.jsonl --count 5 --label-prefix fresh \
    --proxy "http://user:pass@host:port"
```

Без `--emails` скрипт спросит почту для `fresh-01`, `fresh-02`, ... перед
запуском каждого браузера. Если почты уже записаны в файл:

```bash
python cookie_extractor.py add-batch \
    --store data/input/accounts-new.jsonl --emails data/input/emails5.txt --count 5 \
    --label-prefix fresh --proxy "http://user:pass@host:port"
```

Для одиночного добавления:

```bash
python cookie_extractor.py add --auto-label \
    --proxy-pool data/input/proxies-top.txt --email r7f3k2@example.com
```

### Собрать cookie без proxy, потом выдать proxy

Если нужно быстро получить cookie для теста без прокси:

```bash
python cookie_extractor.py add-batch --emails data/input/emails.txt --no-proxy
```

Для каждого аккаунта откроется Camoufox напрямую, вход ручной. Записи
сохранятся с пустым `proxy`. Сервер такие job'ы не примет, пока не выдать
proxy:

```bash
python cookie_extractor.py assign-proxies --store data/input/accounts.jsonl \
    --proxy-pool data/input/proxies-working.txt
```

Команда не перелогинивает аккаунты — она только подставляет свободные proxy
в записи, где `proxy` пустой. После этого `data/input/accounts.jsonl` можно отправлять
на сервер. Помни: cookie, снятые с прямого IP, могут дать
`shouldPoll: false` при работе через proxy.

Полезные флаги:

- `--proxy-timeout 8` — сколько ждать проверку одного proxy;
- `--max-proxy-latency 8` — не брать proxy, если HTTP-проверка дольше 8 с
  (0 = без лимита);
- `--max-page-load 30` — считать proxy медленным, если страница входа
  грузилась дольше 30 с (0 = без лимита);
- `--timeout 900` — сколько секунд ждать ручной вход в аккаунт
  (0 = ждать бесконечно);
- `--no-proxy` — один аккаунт напрямую, без пула и проверок proxy;
- `--proxy-top 512` — проверить по HTTP только 512 лучших по TCP (0 = все);
- `--auto-proxy-pool FILE` — большой пул для автодобора
  (по умолчанию `data/input/proxies-socks5-all.txt`);
- `--proxy-test-url ...` — другой URL для проверки;
- `--allow-unverified-proxy` — если HTTP-проверка не прошла, взять лучший по TCP
  (менее надёжно);
- `--new-proxy` — перевыбрать proxy для уже существующего label.

Если пул закончился, скрипт остановится с ошибкой «все прокси уже заняты»;
добавьте ещё proxy в файл.

### Отдельный sticky-порт на аккаунт

Если у провайдера порт задаёт sticky-сессию, положите несколько портов в файл
и используйте пул:

```powershell
python cookie_extractor.py add-batch `
  --store .\data\accounts-test.jsonl `
  --emails .\data\emails-test.txt `
  --count 9 `
  --label-prefix test `
  --proxy-pool .\data\input\proxies-ports-test.txt `
  --max-proxy-latency 15 `
  --timeout 900
```

Каждый аккаунт получит отдельный свободный proxy. Логин и последующий job
должны идти через один и тот же порт/IP; иначе cookie могут стать
невалидными.

## Что внутри data/input/accounts.jsonl

Одна строка (значения cookie сокращены):

```json
{"external_id":"riot-acc-01","label":"acc-01","new_email":"new@example.com","proxy":"socks5://user:pass@1.2.3.4:1080","cookies":[{"name":"ssid","value":"...","domain":".riotgames.com","path":"/","httpOnly":true,"secure":true},{"name":"csrftoken","value":"...","domain":".riotgames.com","path":"/"}],"headless":true,"updated_at":"2026-11-06T00:00:00Z"}
```

- `external_id` — стабильный id для идемпотентности сервера. При повторном
  сохранении того же label он не меняется.
- `label` — ваш ярлык, сервер его игнорирует.
- `cookies` — массив `{name, value, domain, path}` + необязательные
  `expires`, `httpOnly`, `secure`.
- `proxy` — тот же proxy, через который вы входили.
- `new_email` — цель смены почты.
- `headless` — для Camoufox штатный режим `true`; headed использовать для отладки.

## Отправка на сервер

### Вариант 1: по одному job'у через готовый клиент

`data/input/accounts.jsonl` уже подходит как вход для `scripts/submit_accounts.py`:

```bash
python scripts/submit_accounts.py \
  --url https://YOUR-URL.ngrok-free.app \
  --api-key "$API_KEY" \
  --input data/input/accounts.jsonl \
  --out data/output/job-results.jsonl \
  --concurrency 8 --rate 10 \
  --webhook-url https://client.example/hook
```

Перед отправкой можно проверить, что все записи готовы:

```bash
python cookie_extractor.py export --format jsonl --out accounts.export.jsonl
```

Экспорт проверяет cookie, `ssid`, email и наличие proxy. Если чего-то нет —
скажет, какой аккаунт сломан, и ничего не запишет. `--skip-incomplete`
пропускает неполные записи, `--allow-empty-proxy` разрешает пустой proxy
(например, для локального теста).

### Вариант 2: одна пачка в `POST /api/v1/jobs`

```bash
python cookie_extractor.py export --format batch --out batch.json
```

`batch.json` — это готовое тело:

```json
{"priority": 0, "jobs": [ ... ]}
```

Его можно отправить любым HTTP-клиентом с заголовком `X-API-Key`.

## Важные детали

- **Cookie — это доступ к аккаунту.** `data/input/accounts.jsonl` содержит действующие
  сессии и proxy-пароли. Не коммитьте его, не пересылайте в чаты. Файл
  создаётся с правами `600` на Linux/macOS; на Windows проверьте папку сами.
- **Один аккаунт — один контекст.** Каждое добавление открывает чистый
  временный Camoufox-профиль, поэтому cookie разных аккаунтов не смешиваются.
- **Proxy лучше указывать сразу.** Если войти напрямую, а job отправить через
  резидентский proxy, Riot может счесть сессию подозрительной и вернуть
  `shouldPoll: false`. Для входа и job'а используйте один и тот же выходной IP.
- **Fingerprint и GeoIP всегда включены.** Camoufox получает настоящий
  Windows-профиль (navigator, screen, WebGL, canvas/audio seeds) и разрешает
  timezone/locale/geolocation через GeoIP выходного IP. Отключить fingerprint
  или GeoIP нельзя: это часть рабочей конфигурации.
- **Тест сервера без proxy.** В `.env` можно включить `ALLOW_NO_PROXY=1`,
  перезапустить сервер и отправить записи с пустым `proxy`:
  ```bash
  python scripts/submit_accounts.py ... --input data/input/accounts-new.jsonl --allow-empty-proxy
  ```
  Это только для локальных тестов; после теста верните `ALLOW_NO_PROXY=0`.
- **Плохие proxy удаляются сами.** При старте `add-batch` выкидывает из
  `data/input/proxies-working.txt`/`data/input/proxies-good.txt` всё, что не прошло проверку или
  грузится дольше 8 секунд. Если выбранный proxy дал Cloudflare-блок,
  ошибку соединения или страница через него грузилась дольше
  `--max-page-load` (по умолчанию 30 с), он удаляется из рабочего списка и
  пишется в `data/input/proxies-bad.txt`; при следующем запуске он больше не выбирается.
  Сам batch при этом останавливается — автоподмену прокси на лету скрипт не делает.
- **GeoIP включён всегда.** Camoufox определяет выходной IP (через proxy,
  если он задан) и подстраивает timezone/locale/geolocation. Прокси должен
  пропускать запрос GeoIP; при ошибке запуска проверьте proxy, а не отключайте
  GeoIP.
- **Relogin.** Когда `ssid` истечёт, `list` покажет `EXPIRED` или `session`
  (для session-cookie срок не хранится). Сделайте
  `python cookie_extractor.py relogin acc-01` — запись обновится, label и
  `external_id` останутся прежними.
- **Проверка API.** Без сессии Riot отвечает `403 not_logged_in`; скрипт
  отличает это от сетевого сбоя и не сохраняет «пустую» сессию.
- **Лимит cookie.** Сервер принимает не больше 200 cookie на job; Riot
  укладывается с большим запасом. Если лимит превышен, скрипт скажет.

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| `camoufox is not installed` | `python -m pip install -e .` |
| Camoufox долго стартует впервые | скачивает движок Firefox и GeoLite2 в `%LOCALAPPDATA%\camoufox` |
| Camoufox download failed | проверьте сеть; скачивание повторится при следующем запуске |
| окно есть, но вход не сохраняется | войдите до конца; дождитесь страницы аккаунта, а не формы Sign in |
| `Failed to get IP address ...` | GeoIP включён всегда; проверьте, что proxy пропускает запрос к сервису определения IP, или смените proxy |
| `Sorry, you have been blocked` / `Attention Required` | Cloudflare заблокировал IP proxy; нужен нормальный residential/mobile proxy или прямое подключение, free-прокси тут не подходят |
| `proxy` не подключается | проверьте `host:port`, логин/пароль; для SOCKS5 с авторизацией иногда нужен HTTP-proxy |
| `в пуле только N свободных proxy` | запустите `--count N` или возьмите `data/input/proxies-socks5-all.txt` с `--proxy-top 512` |
| `все прокси уже заняты` | добавьте proxy в `data/input/proxies-top.txt`/`data/input/proxies.txt` или удалите лишние аккаунты |
| `ни один прокси не прошёл HTTP-проверку` | проверьте proxy; если он работает только в браузере, попробуйте `--allow-unverified-proxy` |
| `403 not_logged_in` | сессия ещё не создана; завершите 2FA/вход |
| экспорт ругается на proxy | укажите proxy в `add`/`relogin` или используйте `--allow-empty-proxy` только для теста |
