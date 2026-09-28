"""Проверки REST API поступлений и полного жизненного цикла документа."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import DEV_USER_ID

URL = "/api/v1/documents/goods-receipt"
ORG = "00000000-0000-0000-0000-000000000001"
SUPPLIER = "00000000-0000-0000-0000-000000000101"
PAPER = "00000000-0000-0000-0000-000000000601"
COUNTS = {"reg_stock": 3, "reg_settlement": 1, "reg_price": 3, "acc_entry": 3}


@pytest.fixture
def body():
    return {
        "doc_date": "2026-09-14T10:00:00+05:00", "org_id": ORG, "counterparty_id": SUPPLIER,
        "contract_id": "00000000-0000-0000-0000-000000000201",
        "warehouse_id": "00000000-0000-0000-0000-000000000301",
        "mol_id": "00000000-0000-0000-0000-000000000401",
        "supplier_doc_no": "47", "supplier_doc_date": "2026-09-14",
        "lines": [{"item_id": PAPER, "qty": "40", "price": "1850"},
                  {"item_id": "00000000-0000-0000-0000-000000000602", "qty": "6", "price": "22500"},
                  {"item_id": "00000000-0000-0000-0000-000000000603", "qty": "200", "price": "190"}],
    }


def create(client, body):
    response = client.post(URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_number_totals_and_names(client, body):
    body["amount_total"] = "1"
    body["lines"][0]["amount"] = "1"
    doc = create(client, body)
    assert doc["number"] == "ПОС-000001"
    assert Decimal(doc["amount_total"]) == Decimal("247000.00")
    assert [Decimal(line["amount"]) for line in doc["lines"]] == list(map(Decimal, ["74000", "135000", "38000"]))
    assert [line["line_no"] for line in doc["lines"]] == [1, 2, 3]
    assert doc["lines"][0]["uom_name"] == "упак"
    assert doc["lines"][0]["item_name"].startswith("Бумага")
    assert all(doc[field] for field in ("org_name", "counterparty_name", "contract_name", "warehouse_name", "mol_name"))
    assert doc["status"] == "draft"
    assert doc["doc_date"] == "2026-09-14T05:00:00+00:00"
    assert client.get(f"{URL}/{doc['id']}").json() == doc
    assert create(client, body)["number"] == "ПОС-000002"


def test_price_history_on_document_date(client, body):
    doc = create(client, body)
    assert client.post(f"{URL}/{doc['id']}/post").status_code == 200
    body["lines"] = [{"item_id": PAPER, "qty": "1"}]
    assert Decimal(create(client, body)["lines"][0]["price"]) == Decimal("1850")
    body["doc_date"] = "2026-09-14T09:59:59+05:00"
    response = client.post(URL, json=body)
    assert response.status_code == 422
    assert response.json()["details"][0]["message"] == "Укажите цену в строке 1"


def test_missing_price_and_failed_creation_preserve_number(client, body, db):
    bad = deepcopy(body)
    del bad["lines"][0]["price"]
    response = client.post(URL, json=bad)
    assert response.status_code == 422
    assert response.json()["details"][0]["message"] == "Укажите цену в строке 1"
    # Ошибка после выдачи номера тоже откатывает счётчик.
    bad = deepcopy(body)
    bad["warehouse_id"] = str(uuid4())
    assert client.post(URL, json=bad).status_code == 422
    assert db.execute("SELECT count(*) AS n FROM doc_goods_receipt").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] == 0
    assert create(client, body)["number"] == "ПОС-000001"


@pytest.mark.parametrize("field,table", [("counterparty_id", "ref_counterparty"), ("org_id", "ref_organization")])
def test_wrong_contract(client, body, db, field, table):
    other = db.execute(f"INSERT INTO {table} (code, name) VALUES ('OTHER', 'Другой') RETURNING id").fetchone()["id"]
    body[field] = str(other)
    response = client.post(URL, json=body)
    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == "contract_id"
    assert response.json()["details"][0]["code"] == "contract_mismatch"


def test_lifecycle_records_and_audit(client, body, db):
    doc = create(client, body)
    url = f"{URL}/{doc['id']}"
    response = client.post(url + "/post")
    assert response.status_code == 200
    assert response.json()["record_counts"] == COUNTS
    records = client.get(url + "/records").json()
    assert {k: len(v) for k, v in records.items()} == COUNTS
    assert records["reg_stock"][0]["warehouse_name"] == "Центральный склад"
    assert records["reg_stock"][0]["item_name"].startswith("Бумага")
    assert records["reg_settlement"][0]["counterparty_name"]
    assert records["reg_settlement"][0]["contract_name"]
    assert all(e["dt_account_name"] and e["kt_account_name"] for e in records["acc_entry"])
    assert records["acc_entry"][0]["dt_dims_names"]["warehouse"] == "Центральный склад"
    assert records["acc_entry"][0]["kt_dims_names"]["counterparty"]
    assert client.post(url + "/post").json()["record_counts"] == COUNTS
    response = client.put(url, json=body)
    assert response.status_code == 409
    assert response.json()["error"] == "document_posted"
    assert response.json()["message"] == "Проведённый документ нельзя изменить. Сначала отмените проведение"
    assert client.post(url + "/unpost").json()["status"] == "draft"
    body["lines"] = [{"item_id": PAPER, "qty": "2", "price": "100"}]
    response = client.put(url, json=body)
    assert response.status_code == 200
    assert response.json()["number"] == doc["number"]
    assert len(response.json()["lines"]) == 1
    assert Decimal(response.json()["amount_total"]) == Decimal("200")
    assert client.post(url + "/post").status_code == 200
    assert client.post(url + "/deletion-mark", json={"mark": True}).status_code == 200
    actions = db.execute("SELECT * FROM audit_log WHERE object_id = %s ORDER BY id", (doc["id"],)).fetchall()
    assert {r["action"] for r in actions} >= {"create", "update", "post", "unpost", "mark"}
    assert all(r["user_id"] == DEV_USER_ID and r["object_type"] == "GoodsReceipt" for r in actions)


def test_deletion_mark_restore_and_no_delete(client, body, db):
    doc = create(client, body)
    url = f"{URL}/{doc['id']}"
    client.post(url + "/post")
    for _ in range(2):
        response = client.post(url + "/deletion-mark", json={"mark": True})
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
        assert all(not rows for rows in client.get(url + "/records").json().values())
    assert db.execute("SELECT status FROM doc_goods_receipt WHERE id = %s", (doc["id"],)).fetchone()["status"] == "deleted"
    assert client.get(URL).json()["total"] == 0
    assert client.get(URL, params={"include_deleted": True}).json()["total"] == 1
    assert client.put(url, json=body).json()["error"] == "document_deleted"
    for action in ("post", "unpost"):
        response = client.post(url + "/" + action)
        assert response.status_code == 422
        assert response.json()["error"] == "posting_failed"
        assert response.json()["details"][0]["code"] == "deleted"
    assert client.delete(url).status_code == 405
    assert client.post(url + "/deletion-mark", json={"mark": False}).json()["status"] == "draft"
    assert client.get(URL).json()["total"] == 1
    assert client.post(url + "/post").status_code == 200


def test_list_filters(client, body):
    first = create(client, body)
    second = create(client, body)
    client.post(f"{URL}/{second['id']}/post")
    for params, expected in [({"q": "000001"}, 1), ({"q": "Алматы Канцтовары"}, 2),
                             ({"status": "draft"}, 1), ({"status": "posted"}, 1),
                             ({"org_id": ORG}, 2), ({"org_id": str(uuid4())}, 0),
                             ({"date_from": "2026-09-14", "date_to": "2026-09-14"}, 2),
                             ({"date_from": "2026-09-15"}, 0), ({"date_to": "2026-09-13"}, 0)]:
        response = client.get(URL, params=params)
        assert response.status_code == 200
        assert response.json()["total"] == expected
    data = client.get(URL, params={"page": 2}).json()
    assert data == {"items": [], "total": 2, "page": 2, "page_size": 50}
    assert client.get(URL, params={"status": "draft"}).json()["items"][0]["id"] == first["id"]


@pytest.mark.parametrize("method,suffix", [("get", ""), ("put", ""), ("get", "/records"),
                                           ("post", "/post"), ("post", "/unpost"), ("post", "/deletion-mark")])
def test_not_found(client, body, method, suffix):
    kwargs = {"json": body} if method == "put" else {}
    if suffix == "/deletion-mark":
        kwargs["json"] = {"mark": True}
    response = getattr(client, method)(f"{URL}/{uuid4()}{suffix}", **kwargs)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.parametrize("field,value,message", [("qty", "0", "больше нуля"), ("qty", "-1", "больше нуля"),
                                                 ("price", "-1", "отрицательным")])
def test_line_validation(client, body, field, value, message):
    body["lines"][0][field] = value
    response = client.post(URL, json=body)
    assert response.status_code == 422
    assert message in response.json()["details"][0]["message"]


def test_rounding_empty_document_and_default_date(client, body):
    body["lines"] = [{"item_id": PAPER, "qty": "0.005", "price": "1"}] * 2
    doc = create(client, body)
    assert Decimal(doc["amount_total"]) == Decimal("0.02")
    assert client.post(f"{URL}/{doc['id']}/post").status_code == 200
    body["lines"] = []
    del body["doc_date"]
    empty = create(client, body)
    assert empty["doc_date"] and Decimal(empty["amount_total"]) == 0
    assert client.post(f"{URL}/{empty['id']}/post").status_code == 422


def test_failed_update_keeps_document(client, body):
    doc = create(client, body)
    url = f"{URL}/{doc['id']}"
    changed = deepcopy(body)
    changed["org_id"] = str(uuid4())
    response = client.put(url, json=changed)
    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == "org_id"
    changed = deepcopy(body)
    changed["warehouse_id"] = str(uuid4())
    assert client.put(url, json=changed).status_code == 422
    assert client.get(url).json() == doc


def test_numbering_year_uses_almaty(client, body, db):
    body["doc_date"] = "2026-12-31T20:00:00Z"
    create(client, body)
    assert db.execute("SELECT year FROM doc_number_counter").fetchone()["year"] == 2027


def test_local_date_filter_includes_late_utc_previous_day(client, body):
    body["doc_date"] = "2026-09-13T20:00:00Z"
    create(client, body)
    assert client.get(URL, params={"date_from": "2026-09-14", "date_to": "2026-09-14"}).json()["total"] == 1
    assert client.get(URL, params={"date_to": "2026-09-13"}).json()["total"] == 0


def test_numbers_restart_each_almaty_year(client, body, db):
    first = create(client, body)
    body["doc_date"] = "2026-12-31T23:30:00+05:00"
    second = create(client, body)
    body["doc_date"] = "2027-01-01T00:30:00+05:00"
    third = create(client, body)
    assert [(doc["number_year"], doc["number"]) for doc in (first, second, third)] == [
        (2026, "ПОС-000001"), (2026, "ПОС-000002"), (2027, "ПОС-000001"),
    ]
    rows = db.execute("SELECT id, number_year, number FROM doc_goods_receipt").fetchall()
    assert len(rows) == 3
    assert {str(row["id"]) for row in rows} == {doc["id"] for doc in (first, second, third)}


@pytest.mark.parametrize("new_date", ["2027-01-01T00:30:00+05:00", "2025-12-31T23:30:00+05:00"])
def test_update_rejects_numbering_year_change(client, body, db, new_date):
    doc = create(client, body)
    url = f"{URL}/{doc['id']}"
    body["doc_date"] = new_date
    response = client.put(url, json=body)
    assert response.status_code == 422
    assert response.json()["details"] == [{
        "field": "doc_date", "code": "year_change", "message": "Нельзя перенести документ в другой год",
    }]
    assert client.get(url).json() == doc
    assert [r["action"] for r in db.execute(
        "SELECT action FROM audit_log WHERE object_id = %s ORDER BY id", (doc["id"],)
    )] == ["create"]


def test_update_date_within_almaty_year_preserves_number(client, body):
    body["doc_date"] = "2026-01-01T00:30:00+05:00"
    doc = create(client, body)
    body["doc_date"] = "2026-12-31T23:30:00+05:00"
    response = client.put(f"{URL}/{doc['id']}", json=body)
    assert response.status_code == 200
    assert response.json()["number_year"] == doc["number_year"] == 2026
    assert response.json()["number"] == doc["number"]


@pytest.mark.parametrize("was_posted", [False, True])
def test_mark_audits_unpost_only_for_posted_document(client, body, db, was_posted):
    doc = create(client, body)
    url = f"{URL}/{doc['id']}"
    if was_posted:
        assert client.post(url + "/post").status_code == 200
    for mark, status in [(True, "deleted"), (True, "deleted"), (False, "draft"), (False, "draft")]:
        response = client.post(url + "/deletion-mark", json={"mark": mark})
        assert response.status_code == 200
        assert response.json()["status"] == status
        assert all(not rows for rows in client.get(url + "/records").json().values())
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM audit_log WHERE object_id = %s ORDER BY id", (doc["id"],)
    )]
    assert actions == ["create"] + (["post", "unpost"] if was_posted else []) + ["mark"] * 4
