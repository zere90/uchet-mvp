"""Общие настройки тестов.

Тесты работают с ОТДЕЛЬНОЙ базой (TEST_DATABASE_URL). В начале прогона
схема создаётся заново, а перед каждым тестом таблицы очищаются и
заполняются тестовыми данными из seed.sql — тесты не влияют друг на друга.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
TEST_URL = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/uchet_test")
os.environ["DATABASE_URL"] = TEST_URL  # важно: до импорта app

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.main import app  # noqa: E402
from scripts.init_db import init_db  # noqa: E402

SEED = (Path(__file__).resolve().parent.parent / "db" / "seed.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db(TEST_URL, seed=False)


@pytest.fixture(autouse=True)
def _fresh_data(_schema):
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        conn.execute(SEED)
    yield


@pytest.fixture(scope="session")
def client(_schema):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    with psycopg.connect(TEST_URL, row_factory=dict_row, autocommit=True) as conn:
        yield conn
