"""Подключение к PostgreSQL через пул соединений."""
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

# Время храним в UTC (PRD, раздел 12). Показывать в Asia/Almaty — задача интерфейса.
pool = ConnectionPool(
    DATABASE_URL,
    kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
    open=False,
)


def get_conn() -> Iterator[psycopg.Connection]:
    """Зависимость FastAPI: одно соединение = одна транзакция на запрос.

    Если обработчик завершился без ошибки — commit, иначе — rollback.
    """
    with pool.connection() as conn:
        yield conn
