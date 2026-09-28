"""Проверки ограничений базы данных, заложенных в схему на этапе 1."""
import threading

import psycopg
import pytest
from psycopg import errors as pg

from tests.conftest import TEST_URL

ORG = "00000000-0000-0000-0000-000000000001"


def test_numbering_sequential_per_org_and_year(db):
    nums = [db.execute("SELECT next_doc_number('GoodsReceipt', %s, 2026, 'ПОС') AS n",
                       (ORG,)).fetchone()["n"] for _ in range(3)]
    assert nums == ["ПОС-000001", "ПОС-000002", "ПОС-000003"]
    # новый год — нумерация с начала
    n = db.execute("SELECT next_doc_number('GoodsReceipt', %s, 2027, 'ПОС') AS n", (ORG,)).fetchone()["n"]
    assert n == "ПОС-000001"


def test_numbering_no_gap_after_rollback():
    with psycopg.connect(TEST_URL) as conn:
        conn.execute("SELECT next_doc_number('GoodsReceipt', %s, 2026, 'ПОС')", (ORG,))
        conn.rollback()  # запись документа не удалась
        n = conn.execute("SELECT next_doc_number('GoodsReceipt', %s, 2026, 'ПОС')", (ORG,)).fetchone()[0]
        conn.commit()
    assert n == "ПОС-000001"  # номер не «сгорел»


def test_numbering_no_duplicates_in_parallel():
    results, lock = [], threading.Lock()

    def worker():
        with psycopg.connect(TEST_URL) as conn:
            for _ in range(20):
                n = conn.execute("SELECT next_doc_number('GoodsReceipt', %s, 2026, 'ПОС')",
                                 (ORG,)).fetchone()[0]
                conn.commit()
                with lock:
                    results.append(n)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 100
    assert sorted(results) == [f"ПОС-{i:06d}" for i in range(1, 101)]  # без дублей и без дыр


def test_line_qty_must_be_positive(db):
    doc = db.execute("""
        INSERT INTO doc_goods_receipt (number, doc_date, org_id, counterparty_id, warehouse_id, author_id)
        VALUES ('ПОС-000001', now(), %s, '00000000-0000-0000-0000-000000000101',
                '00000000-0000-0000-0000-000000000301', gen_random_uuid()) RETURNING id""",
        (ORG,)).fetchone()["id"]
    with pytest.raises(pg.CheckViolation):
        db.execute("""
            INSERT INTO doc_goods_receipt_line (doc_id, line_no, item_id, uom_id, qty, price, amount)
            VALUES (%s, 1, '00000000-0000-0000-0000-000000000601',
                    '00000000-0000-0000-0000-000000000501', 0, 1850, 0)""", (doc,))


def test_money_columns_are_numeric_not_float(db):
    bad = db.execute("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type IN ('real', 'double precision')""").fetchall()
    assert bad == []


def test_audit_log_is_read_only(db):
    db.execute("INSERT INTO audit_log (user_id, action, object_type, object_id) "
               "VALUES (gen_random_uuid(), 'create', 'test', gen_random_uuid())")
    with pytest.raises(pg.RaiseException):
        db.execute("UPDATE audit_log SET action = 'hacked'")
    with pytest.raises(pg.RaiseException):
        db.execute("DELETE FROM audit_log")


def test_posting_rules_are_stored_as_data(db):
    rules = db.execute("SELECT kind, count(*) AS n FROM meta_posting_rule "
                       "WHERE doc_type = 'GoodsReceipt' GROUP BY kind ORDER BY kind").fetchall()
    assert [(r["kind"], r["n"]) for r in rules] == [("entry", 3), ("register", 3)]
