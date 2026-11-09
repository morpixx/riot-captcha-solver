# Contributing

## Подготовка

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

## Правила

- Не коммить `.env`, cookies, прокси, email-файлы, БД и `data/output`.
- В тестах — только синтетические аккаунты и фиктивные прокси.
- Не логируй cookies, proxy-креды и API-ключи.
- Держи модули разделёнными: `api`, `jobs`, `riot`, `browser`, `proxy`,
  `extractor`.
- Меняешь поведение API — обнови `docs/jobs-api.md` и README.

## Стиль

- Python 3.10+, `ruff` (line-length 100).
- Type hints и `from __future__ import annotations` там, где уже принято.
- Без новых тяжёлых зависимостей без необходимости.
- Комментарии — по делу; русский UI/docs допустим.

## Pull request

1. Опиши изменение и риск.
2. Добавь/обнови тест.
3. Прогони `pytest` и `ruff`.
4. Убедись, что в diff нет секретов.

## Лицензия

Отправляя PR, подтверждаешь, что у тебя есть право на этот код. Проект
распространяется по MIT — см. [LICENSE](LICENSE).
