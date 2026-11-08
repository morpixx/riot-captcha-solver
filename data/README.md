# Данные

- `input/` — то, что кладёшь сам: `accounts*.jsonl`, `emails*.txt`,
  `proxies*.txt`, `proxies-bad.txt`.
- `output/` — то, что пишут скрипты: `job-results*.jsonl`,
  `webhook-results*.jsonl`.
- `jobs.sqlite3` — живое состояние очереди сервера (не датасет).

В Docker `./data` монтируется в `/app/data`; входные файлы кладите в
`data/input` на хосте, результаты появятся в `data/output`.

Файлы могут содержать cookie и адреса. Они исключены из git и не должны
попадать в публичные архивы или логи. Пути переопределяются через
`COOKIES_FILE`, `--store`, `--emails`, `--out`.
