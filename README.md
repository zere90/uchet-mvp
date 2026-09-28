# Учётная система — MVP «Поступление запасов»

Первый рабочий контур учётной системы для госучреждений: от накладной поставщика
до остатков на складе и бухгалтерских проводок. Требования описаны в PRD
«Первый рабочий контур учётной системы: поступление запасов» (версия 0.1).

**Стек:** Python 3.11+, FastAPI, PostgreSQL 16, psycopg 3, pytest.

## Как устроено

Одно событие («поставщик привёз товар») записывается в три слоя:

| Слой | Таблицы | Отвечает на вопрос |
|---|---|---|
| 1. Документ | `doc_goods_receipt`, `doc_goods_receipt_line` | что произошло |
| 2. Регистры | `reg_stock`, `reg_settlement`, `reg_price` | сколько чего у нас |
| 3. Проводки | `acc_entry` | сходится ли баланс |

Регистры и проводки заполняются только при проведении документа, по правилам из
таблицы `meta_posting_rule`. Правила — это данные, а не код: номера счетов в коде
не встречаются.

## Структура проекта

```
app/
  main.py        точка входа FastAPI
  catalogs.py    справочники: API и модели (FR-01)
  db.py          подключение к PostgreSQL
  errors.py      единый формат ошибок API
  config.py      настройки из .env
db/
  schema.sql     все таблицы базы (PRD, раздел 9)
  seed.sql       тестовые данные из примера PRD + правила проводок
scripts/
  init_db.py     пересоздать базу со схемой и тестовыми данными
tests/           автотесты
docs/PLAN.md     план работ и статус требований
```

## Запуск

1. **PostgreSQL.** Проще всего через Docker:
   ```bash
   docker compose up -d
   ```
   Создадутся две базы: `uchet` (рабочая) и `uchet_test` (для тестов).
   Без Docker можно поставить [Postgres.app](https://postgresapp.com) и создать базы
   `uchet` и `uchet_test` вручную.

2. **Python-окружение:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. **База:** создать таблицы и загрузить тестовые данные
   ```bash
   python scripts/init_db.py
   ```

4. **Сервер:**
   ```bash
   uvicorn app.main:app --reload
   ```
   Документация API с кнопками «попробовать»: http://localhost:8000/docs

5. **Тесты:**
   ```bash
   pytest -v
   ```

## API справочников (этап 1)

Восемь справочников: `organizations`, `counterparties`, `contracts`, `units`,
`items`, `warehouses`, `persons`, `accounts`.

```
GET   /api/v1/catalogs/{справочник}?q=&page=&include_deleted=   список, поиск по коду и наименованию
GET   /api/v1/catalogs/{справочник}/{id}                         один элемент
POST  /api/v1/catalogs/{справочник}                              создать            → 201
PUT   /api/v1/catalogs/{справочник}/{id}                         изменить           → 200
POST  /api/v1/catalogs/{справочник}/{id}/deletion-mark           пометка на удаление → 200
```

Физического удаления нет — только пометка. Ошибки возвращаются в формате PRD:

```json
{
  "error": "validation_failed",
  "message": "Данные не сохранены",
  "details": [{ "field": "code", "code": "duplicate", "message": "Такое значение уже есть в справочнике" }]
}
```

## Допущения на текущем этапе

- **Аутентификации пока нет.** В PRD предусмотрен вход через OIDC; до его подключения
  все действия пишутся в журнал от имени `DEV_USER_ID` из `.env`.
- **Номера счетов в `seed.sql` — иллюстрация из PRD.** Точные субсчета нужно сверить
  с приказом Министра финансов РК от 16.04.2025 № 170.
- **Нумерация документов — отдельная по организации и году** (как в FR-03). Открытый
  вопрос PRD о сквозной нумерации по всей базе решается правкой одной функции
  `next_doc_number`.
