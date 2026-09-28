"""Проведение, отмена и атомарность поступлений."""
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from app.errors import install
from app.posting import PostingError, post, unpost
from tests.conftest import TEST_URL

ORG, SUPPLIER, CONTRACT, WAREHOUSE, MOL = [UUID(f"00000000-0000-0000-0000-{n:012d}")
                                         for n in (1, 101, 201, 301, 401)]
USER = uuid4()
TABLES = ("reg_stock", "reg_settlement", "reg_price", "acc_entry")


def document(db, items=None):
    if items is None:
        items = [(601, "40", "1850"), (602, "6", "22500"), (603, "200", "190")]
    doc = db.execute("""
        INSERT INTO doc_goods_receipt
          (number, doc_date, org_id, counterparty_id, contract_id, warehouse_id, mol_id, author_id)
        VALUES ('ПОС-000001', '2026-09-14 00:00:00+00', %s, %s, %s, %s, %s, %s) RETURNING id
        """, (ORG, SUPPLIER, CONTRACT, WAREHOUSE, MOL, USER)).fetchone()["id"]
    for number, (item, qty, price) in enumerate(items, 1):
        db.execute("""
            INSERT INTO doc_goods_receipt_line (doc_id, line_no, item_id, uom_id, qty, price, amount)
            SELECT %s, %s, id, uom_id, %s::numeric, %s::numeric, round(%s::numeric * %s::numeric, 2)
            FROM ref_item WHERE id = %s
            """, (doc, number, Decimal(qty), Decimal(price), Decimal(qty), Decimal(price),
                  UUID(f"00000000-0000-0000-0000-{item:012d}")))
    db.execute("UPDATE doc_goods_receipt SET amount_total = "
               "(SELECT coalesce(sum(amount), 0) FROM doc_goods_receipt_line WHERE doc_id = %s) WHERE id = %s",
               (doc, doc))
    return doc


def records(db, doc, table):
    return db.execute(sql.SQL("SELECT * FROM {} WHERE recorder_type = 'GoodsReceipt' AND recorder_id = %s "
                              "ORDER BY line_no").format(sql.Identifier(table)), (doc,)).fetchall()


def state(db, doc):
    return db.execute("SELECT * FROM doc_goods_receipt WHERE id = %s", (doc,)).fetchone()


def assert_empty(db, doc):
    assert all(records(db, doc, table) == [] for table in TABLES)
    assert state(db, doc)["status"] == "draft"
    assert state(db, doc)["posted_at"] is None
    assert db.execute("SELECT * FROM audit_log WHERE object_id = %s", (doc,)).fetchall() == []


def test_post_inventory_and_balance(db):
    doc = document(db)
    post(db, doc, USER)
    assert [len(records(db, doc, t)) for t in TABLES] == [3, 1, 3, 3]
    assert records(db, doc, "reg_settlement")[0]["amount"] == Decimal("247000.00")
    entries = records(db, doc, "acc_entry")
    assert {(e["dt_account"], e["kt_account"]) for e in entries} == {("1310", "3210")}
    debit = sum((e["amount"] for e in entries if e["dt_account"] == "1310"), Decimal(0))
    credit = sum((e["amount"] for e in entries if e["kt_account"] == "3210"), Decimal(0))
    assert debit == credit == state(db, doc)["amount_total"] == Decimal("247000.00")
    assert entries[0]["dt_dims"] == {"warehouse": str(WAREHOUSE), "item": str(UUID(int=0x601)), "mol": str(MOL)}
    for table in TABLES:
        rows = records(db, doc, table)
        assert [r["line_no"] for r in rows] == list(range(1, len(rows) + 1))
        assert all(r["period"] == state(db, doc)["doc_date"] for r in rows)
    assert state(db, doc)["status"] == "posted"
    assert state(db, doc)["posted_at"] is not None
    audit = db.execute("SELECT * FROM audit_log WHERE object_id = %s", (doc,)).fetchone()
    assert (audit["action"], audit["user_id"], audit["object_type"]) == ("post", USER, "GoodsReceipt")


def test_repost_and_unpost(db):
    doc = document(db)
    post(db, doc, USER)
    post(db, doc, USER)
    assert [len(records(db, doc, t)) for t in TABLES] == [3, 1, 3, 3]
    unpost(db, doc, USER)
    assert all(not records(db, doc, t) for t in TABLES)
    assert state(db, doc)["status"] == "draft"
    assert state(db, doc)["posted_at"] is None
    assert [r["action"] for r in db.execute("SELECT action FROM audit_log WHERE object_id = %s ORDER BY id", (doc,))] == ["post", "post", "unpost"]


@pytest.mark.parametrize("item,account", [(604, "2410"), (605, "7010")])
def test_other_item_kinds(db, item, account):
    doc = document(db, [(item, "1", "15000")])
    post(db, doc, USER)
    assert records(db, doc, "reg_stock") == []
    assert records(db, doc, "acc_entry")[0]["dt_account"] == account


def test_account_change_and_first_matching_rule(db):
    doc = document(db)
    post(db, doc, USER)
    db.execute("UPDATE meta_posting_rule SET definition = jsonb_set(definition, '{debit,account}', '\"1080\"') "
               "WHERE kind = 'entry' AND sort_order = 10")
    db.execute("INSERT INTO meta_posting_rule (doc_type, kind, sort_order, definition) "
               "SELECT doc_type, kind, 40, definition FROM meta_posting_rule WHERE kind = 'entry' AND sort_order = 10")
    post(db, doc, USER)
    entries = records(db, doc, "acc_entry")
    assert len(entries) == 3
    assert {e["dt_account"] for e in entries} == {"1080"}


@pytest.mark.parametrize("disable", [False, True])
def test_missing_service_rule(db, disable):
    doc = document(db, [(601, "1", "10"), (605, "1", "20")])
    if disable:
        db.execute("UPDATE meta_posting_rule SET is_active = false WHERE kind = 'entry' AND sort_order = 30")
    else:
        db.execute("DELETE FROM meta_posting_rule WHERE kind = 'entry' AND sort_order = 30")
    with pytest.raises(PostingError) as error:
        post(db, doc, USER)
    assert error.value.details == [{"field": "lines[1]", "code": "no_entry_rule", "message": "нет правила проводок для строки 2"}]
    assert_empty(db, doc)


@pytest.mark.parametrize("case", ["empty", "qty", "price", "supplier", "org", "amount", "total", "deleted"])
def test_validation(db, case):
    doc = document(db, [] if case == "empty" else None)
    # Некорректные значения вводятся только в откатываемой тестовой транзакции.
    with db.transaction(force_rollback=True):
        if case in ("qty", "price"):
            constraint = f"doc_goods_receipt_line_{case}_check"
            db.execute(sql.SQL("ALTER TABLE doc_goods_receipt_line DROP CONSTRAINT {}")
                       .format(sql.Identifier(constraint)))
            db.execute(sql.SQL("UPDATE doc_goods_receipt_line SET {} = %s WHERE doc_id = %s AND line_no = 1")
                       .format(sql.Identifier(case)), (0 if case == "qty" else -1, doc))
        elif case in ("supplier", "org"):
            table = "ref_counterparty" if case == "supplier" else "ref_organization"
            other = db.execute(sql.SQL("INSERT INTO {} (code, name) VALUES ('OTHER', 'Другой') RETURNING id")
                               .format(sql.Identifier(table))).fetchone()["id"]
            db.execute(sql.SQL("UPDATE ref_contract SET {} = %s WHERE id = %s")
                       .format(sql.Identifier("counterparty_id" if case == "supplier" else "org_id")), (other, CONTRACT))
        elif case == "amount":
            db.execute("UPDATE doc_goods_receipt_line SET amount = amount + 1 WHERE doc_id = %s", (doc,))
        elif case == "total":
            db.execute("UPDATE doc_goods_receipt SET amount_total = 1 WHERE id = %s", (doc,))
        elif case == "deleted":
            db.execute("UPDATE doc_goods_receipt SET status = 'deleted' WHERE id = %s", (doc,))
        with pytest.raises(PostingError) as error:
            post(db, doc, USER)
        assert error.value.details
        if case == "deleted":
            assert state(db, doc)["status"] == "deleted"
        else:
            assert_empty(db, doc)
        if case == "qty":
            assert error.value.details[0]["code"] == "must_be_positive"


@pytest.mark.parametrize("failure", ["account_missing", "account_deleted", "sum", "condition", "audit"])
def test_failed_repost_preserves_previous_result(db, failure):
    doc = document(db)
    post(db, doc, USER)
    before = {t: records(db, doc, t) for t in TABLES}
    before_doc = state(db, doc)
    user = USER
    if failure == "account_missing":
        db.execute("UPDATE meta_posting_rule SET definition = jsonb_set(definition, '{debit,account}', '\"9999\"') WHERE kind = 'entry'")
    elif failure == "account_deleted":
        db.execute("UPDATE ref_account SET deletion_mark = true WHERE code = '1310'")
    elif failure == "sum":
        db.execute("UPDATE meta_posting_rule SET definition = jsonb_set(definition, '{amount}', '\"line.price\"') WHERE kind = 'entry'")
    elif failure == "condition":
        db.execute("UPDATE meta_posting_rule SET definition = jsonb_set(definition, '{when}', %s) WHERE kind = 'entry'",
                   (Jsonb("__import__('os')"),))
    else:
        user = None
    with pytest.raises(psycopg.errors.NotNullViolation if failure == "audit" else PostingError):
        post(db, doc, user)
    assert {t: records(db, doc, t) for t in TABLES} == before
    assert state(db, doc) == before_doc
    assert db.execute("SELECT count(*) AS n FROM audit_log WHERE object_id = %s", (doc,)).fetchone()["n"] == 1


def test_document_lock(db):
    doc = document(db)
    with psycopg.connect(TEST_URL) as other:
        other.execute("SELECT id FROM doc_goods_receipt WHERE id = %s FOR UPDATE", (doc,))
        with pytest.raises(PostingError) as error:
            post(db, doc, USER)
        assert error.value.details[0]["message"] == "Документ занят другим пользователем"
    assert_empty(db, doc)
    post(db, doc, USER)


def test_rounding_and_zero_price(db):
    doc = document(db, [(601, "0.005", "1"), (602, "0.005", "1"), (603, "1", "0")])
    post(db, doc, USER)
    assert state(db, doc)["amount_total"] == Decimal("0.02")
    assert len(records(db, doc, "reg_stock")) == 3
    assert [e["amount"] for e in records(db, doc, "acc_entry")] == [Decimal("0.01"), Decimal("0.01")]


def test_default_connection_commits(db):
    doc = document(db)
    with psycopg.connect(TEST_URL) as conn:
        post(conn, doc, USER)
        assert state(db, doc)["status"] == "posted"
        unpost(conn, doc, USER)
        assert state(db, doc)["status"] == "draft"


def test_posting_error_http_format():
    app = FastAPI()
    install(app)
    details = [{"field": "lines", "code": "empty", "message": "Добавьте строки"}]

    @app.get("/test-error")
    def fail():
        raise PostingError(details)

    with TestClient(app) as client:
        response = client.get("/test-error")
    assert response.status_code == 422
    assert response.json() == {"error": "posting_failed", "message": "Документ не проведён", "details": details}
