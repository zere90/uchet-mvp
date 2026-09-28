"""Создаёт базу с нуля: удаляет все таблицы, применяет schema.sql и seed.sql.

    python scripts/init_db.py            # схема + тестовые данные
    python scripts/init_db.py --no-seed  # только схема

ВНИМАНИЕ: все данные в базе будут удалены. Только для разработки!
"""
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DATABASE_URL  # noqa: E402

DB_DIR = Path(__file__).resolve().parent.parent / "db"


def init_db(url: str = DATABASE_URL, seed: bool = True) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        conn.execute((DB_DIR / "schema.sql").read_text(encoding="utf-8"))
        if seed:
            conn.execute((DB_DIR / "seed.sql").read_text(encoding="utf-8"))


if __name__ == "__main__":
    init_db(seed="--no-seed" not in sys.argv)
    print("База создана:", DATABASE_URL.rsplit("@", 1)[-1])
