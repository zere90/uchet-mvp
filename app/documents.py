"""REST API поступлений: черновики, проведение и просмотр движений."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from app import posting
from app.catalogs import DeletionMark
from app.config import DEV_USER_ID
from app.db import get_conn
from app.errors import ApiError, detail

PAGE_SIZE = 50
ALMATY = ZoneInfo("Asia/Almaty")
router = APIRouter(prefix="/api/v1/documents/goods-receipt", tags=["Поступление запасов"])


class ReceiptLine(BaseModel):
    """Принимать только исходные данные строки; суммы вычисляет сервер."""
    item_id: UUID
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=3)
    price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)


class Receipt(BaseModel):
    """Реквизиты черновика; номер, статус и суммы клиент не задаёт."""
    doc_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    org_id: UUID
    counterparty_id: UUID
    contract_id: UUID | None = None
    warehouse_id: UUID
    mol_id: UUID | None = None
    supplier_doc_no: str | None = Field(default=None, max_length=50)
    supplier_doc_date: date | None = None
    lines: list[ReceiptLine] = Field(default_factory=list)

    @field_validator("doc_date")
    @classmethod
    def utc_date(cls, value: datetime) -> datetime:
        """Считать дату без смещения местной и хранить момент времени в UTC."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=ALMATY)
        return value.astimezone(timezone.utc)


def _response(data, status=200):
    """Передать Decimal строками, чтобы JSON не терял точность денежных сумм."""
    return JSONResponse(status_code=status, content=jsonable_encoder(data, custom_encoder={Decimal: str}))


def _invalid(field, code, message):
    """Вернуть понятную ошибку реквизита в общем формате API."""
    raise ApiError(422, "validation_failed", "Данные не сохранены", [detail(field, code, message)])


def _get(conn, doc_id, *, lock=False):
    """Проверить существование документа и при изменении заблокировать его."""
    doc = conn.execute("SELECT * FROM doc_goods_receipt WHERE id = %s"
                       + (" FOR UPDATE" if lock else ""), (doc_id,)).fetchone()
    if doc is None:
        raise ApiError(404, "not_found", "Документ не найден")
    return doc


HEADER = """
    SELECT d.*, o.name AS org_name, c.name AS counterparty_name,
           t.name AS contract_name, w.name AS warehouse_name, p.name AS mol_name
    FROM doc_goods_receipt d
    JOIN ref_organization o ON o.id = d.org_id
    JOIN ref_counterparty c ON c.id = d.counterparty_id
    LEFT JOIN ref_contract t ON t.id = d.contract_id
    JOIN ref_warehouse w ON w.id = d.warehouse_id
    LEFT JOIN ref_person p ON p.id = d.mol_id
"""


def _document(conn, doc_id):
    """Собрать шапку и строки с наименованиями для ответа бухгалтеру."""
    doc = conn.execute(HEADER + " WHERE d.id = %s", (doc_id,)).fetchone()
    if doc is None:
        raise ApiError(404, "not_found", "Документ не найден")
    doc["lines"] = conn.execute("""
        SELECT l.*, i.name AS item_name, u.name AS uom_name
        FROM doc_goods_receipt_line l
        JOIN ref_item i ON i.id = l.item_id JOIN ref_uom u ON u.id = l.uom_id
        WHERE l.doc_id = %s ORDER BY l.line_no
        """, (doc_id,)).fetchall()
    return doc


def _prepare(conn, body):
    """Проверить договор, подобрать цены и рассчитать округлённые суммы строк."""
    if body.contract_id is not None:
        contract = conn.execute("SELECT * FROM ref_contract WHERE id = %s", (body.contract_id,)).fetchone()
        if contract is None or (contract["org_id"], contract["counterparty_id"]) != (body.org_id, body.counterparty_id):
            _invalid("contract_id", "contract_mismatch",
                     "Договор должен принадлежать выбранным поставщику и организации")
    lines = []
    with localcontext() as context:
        context.prec = 50
        for number, line in enumerate(body.lines, 1):
            item = conn.execute("SELECT uom_id FROM ref_item WHERE id = %s", (line.item_id,)).fetchone()
            if item is None:
                _invalid(f"lines[{number - 1}].item_id", "not_found", f"Номенклатура в строке {number} не найдена")
            price = line.price
            if price is None:
                last = conn.execute("""
                    SELECT price FROM reg_price WHERE counterparty_id = %s AND item_id = %s AND period <= %s
                    ORDER BY period DESC, id DESC LIMIT 1
                    """, (body.counterparty_id, line.item_id, body.doc_date)).fetchone()
                if last is None:
                    _invalid(f"lines[{number - 1}].price", "price_required", f"Укажите цену в строке {number}")
                price = last["price"]
            amount = (line.qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            lines.append({"line_no": number, "item_id": line.item_id, "uom_id": item["uom_id"],
                          "qty": line.qty, "price": price, "amount": amount})
        total = sum((line["amount"] for line in lines), Decimal("0.00"))
        if total >= Decimal("10000000000000000"):
            _invalid("amount_total", "amount_too_large", "Сумма документа превышает допустимый размер")
    return lines, total


def _write_lines(conn, doc_id, lines):
    """Сохранить подготовленные строки в порядке, заданном пользователем."""
    for line in lines:
        conn.execute("""
            INSERT INTO doc_goods_receipt_line (doc_id, line_no, item_id, uom_id, qty, price, amount)
            VALUES (%(doc_id)s, %(line_no)s, %(item_id)s, %(uom_id)s, %(qty)s, %(price)s, %(amount)s)
            """, {**line, "doc_id": doc_id})


def _audit(conn, doc_id, action, details=None):
    """Записать автора и действие вместе с изменениями документа."""
    conn.execute("INSERT INTO audit_log (user_id, action, object_type, object_id, details) "
                 "VALUES (%s, %s, %s, %s, %s)",
                 (DEV_USER_ID, action, posting.DOC_TYPE, doc_id, Jsonb(details or {})))


@router.post("", status_code=201)
def create_document(body: Receipt, conn: psycopg.Connection = Depends(get_conn)):
    """Создать черновик с номером и суммами, рассчитанными сервером."""
    lines, total = _prepare(conn, body)
    year = body.doc_date.astimezone(ALMATY).year
    number = conn.execute("SELECT next_doc_number(%s, %s, %s, %s) AS number",
                          (posting.DOC_TYPE, body.org_id, year, "ПОС")).fetchone()["number"]
    data = {**body.model_dump(exclude={"lines"}), "number": number, "number_year": year, "amount_total": total, "author_id": DEV_USER_ID}
    doc_id = conn.execute("""
        INSERT INTO doc_goods_receipt (number, number_year, doc_date, org_id, counterparty_id, contract_id,
          warehouse_id, mol_id, supplier_doc_no, supplier_doc_date, amount_total, author_id)
        VALUES (%(number)s, %(number_year)s, %(doc_date)s, %(org_id)s, %(counterparty_id)s, %(contract_id)s,
          %(warehouse_id)s, %(mol_id)s, %(supplier_doc_no)s, %(supplier_doc_date)s, %(amount_total)s, %(author_id)s)
        RETURNING id
        """, data).fetchone()["id"]
    _write_lines(conn, doc_id, lines)
    _audit(conn, doc_id, "create")
    result = _document(conn, doc_id)
    conn.commit()
    return _response(result, 201)


@router.get("")
def list_documents(
    page: int = Query(1, ge=1), q: str | None = None,
    status: Literal["draft", "posted", "deleted"] | None = None,
    org_id: UUID | None = None, date_from: date | None = None, date_to: date | None = None,
    include_deleted: bool = False, conn: psycopg.Connection = Depends(get_conn),
):
    """Показать страницу документов с поиском и фильтрами по местным датам."""
    conditions, params = [], []
    if not include_deleted:
        conditions.append("d.status <> 'deleted'")
    if q is not None:
        conditions.append("(d.number ILIKE %s OR c.name ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    for column, value in (("d.status", status), ("d.org_id", org_id)):
        if value is not None:
            conditions.append(column + " = %s")
            params.append(value)
    if date_from:
        conditions.append("d.doc_date >= %s")
        params.append(datetime.combine(date_from, time.min, ALMATY))
    if date_to:
        conditions.append("d.doc_date < %s")
        params.append(datetime.combine(date_to, time.min, ALMATY) + timedelta(days=1))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = conn.execute("SELECT count(*) AS n FROM doc_goods_receipt d "
                         "JOIN ref_counterparty c ON c.id = d.counterparty_id" + where, params).fetchone()["n"]
    items = conn.execute(HEADER + where + " ORDER BY d.doc_date DESC, d.id LIMIT %s OFFSET %s",
                         [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return _response({"items": items, "total": total, "page": page, "page_size": PAGE_SIZE})


@router.get("/{doc_id}")
def get_document(doc_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
    """Вернуть документ целиком с расшифровкой ссылок на справочники."""
    return _response(_document(conn, doc_id))


@router.put("/{doc_id}")
def update_document(doc_id: UUID, body: Receipt, conn: psycopg.Connection = Depends(get_conn)):
    """Заменить реквизиты и строки черновика, сохранив номер и организацию."""
    doc = _get(conn, doc_id, lock=True)
    if doc["status"] == "posted":
        raise ApiError(409, "document_posted", "Проведённый документ нельзя изменить. Сначала отмените проведение")
    if doc["status"] == "deleted":
        raise ApiError(409, "document_deleted", "Документ помечен на удаление")
    if body.org_id != doc["org_id"]:
        _invalid("org_id", "organization_immutable", "Организацию существующего документа изменить нельзя")
    if body.doc_date.astimezone(ALMATY).year != doc["number_year"]:
        _invalid("doc_date", "year_change", "Нельзя перенести документ в другой год")
    lines, total = _prepare(conn, body)
    data = body.model_dump(exclude={"lines", "org_id"})
    data["amount_total"] = total
    conn.execute(sql.SQL("UPDATE doc_goods_receipt SET {} WHERE id = %(id)s").format(
        sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in data)),
        {**data, "id": doc_id})
    conn.execute("DELETE FROM doc_goods_receipt_line WHERE doc_id = %s", (doc_id,))
    _write_lines(conn, doc_id, lines)
    _audit(conn, doc_id, "update")
    result = _document(conn, doc_id)
    conn.commit()
    return _response(result)


@router.post("/{doc_id}/post")
def post_document(doc_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
    """Передать проведение движку и вернуть документ с количеством движений."""
    _get(conn, doc_id)
    posting.post(conn, doc_id, DEV_USER_ID)
    result = _document(conn, doc_id)
    result["record_counts"] = {name: len(rows) for name, rows in _records(conn, doc_id).items()}
    conn.commit()
    return _response(result)


@router.post("/{doc_id}/unpost")
def unpost_document(doc_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
    """Отменить проведение через движок и вернуть актуальное состояние."""
    _get(conn, doc_id)
    posting.unpost(conn, doc_id, DEV_USER_ID)
    result = _document(conn, doc_id)
    conn.commit()
    return _response(result)


RECORD_QUERIES = {
    "reg_stock": """SELECT r.*, w.name AS warehouse_name, i.name AS item_name, p.name AS mol_name
        FROM reg_stock r JOIN ref_warehouse w ON w.id = r.warehouse_id
        JOIN ref_item i ON i.id = r.item_id LEFT JOIN ref_person p ON p.id = r.mol_id""",
    "reg_settlement": """SELECT r.*, c.name AS counterparty_name, t.name AS contract_name
        FROM reg_settlement r JOIN ref_counterparty c ON c.id = r.counterparty_id
        LEFT JOIN ref_contract t ON t.id = r.contract_id""",
    "reg_price": """SELECT r.*, c.name AS counterparty_name, i.name AS item_name
        FROM reg_price r JOIN ref_counterparty c ON c.id = r.counterparty_id
        JOIN ref_item i ON i.id = r.item_id""",
    "acc_entry": """SELECT r.*, dt.name AS dt_account_name, kt.name AS kt_account_name
        FROM acc_entry r JOIN ref_account dt ON dt.code = r.dt_account
        JOIN ref_account kt ON kt.code = r.kt_account""",
}
DIM_TABLES = {"warehouse": "ref_warehouse", "item": "ref_item", "counterparty": "ref_counterparty",
              "contract": "ref_contract", "mol": "ref_person"}


def _records(conn, doc_id):
    """Прочитать движения и дополнить аналитику проводок понятными наименованиями."""
    result = {name: conn.execute(query + " WHERE r.recorder_type = %s AND r.recorder_id = %s ORDER BY r.line_no",
                                 (posting.DOC_TYPE, doc_id)).fetchall() for name, query in RECORD_QUERIES.items()}
    names = {}
    for dimension, table in DIM_TABLES.items():
        ids = {e[side].get(dimension) for e in result["acc_entry"] for side in ("dt_dims", "kt_dims")}
        ids.discard(None)
        if ids:
            rows = conn.execute(sql.SQL("SELECT id::text, name FROM {} WHERE id::text = ANY(%s)")
                                .format(sql.Identifier(table)), (list(ids),)).fetchall()
            names[dimension] = {r["id"]: r["name"] for r in rows}
    for entry in result["acc_entry"]:
        for side in ("dt_dims", "kt_dims"):
            entry[side + "_names"] = {key: names.get(key, {}).get(value) for key, value in entry[side].items()}
    return result


@router.get("/{doc_id}/records")
def get_records(doc_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
    """Показать регистры и бухгалтерские проводки существующего документа."""
    _get(conn, doc_id)
    return _response(_records(conn, doc_id))


@router.post("/{doc_id}/deletion-mark")
def set_deletion_mark(doc_id: UUID, body: DeletionMark, conn: psycopg.Connection = Depends(get_conn)):
    """Изменить пометку без физического удаления и не оставить движений у черновика."""
    doc = _get(conn, doc_id, lock=True)
    # Отменяем только реальное проведение, чтобы не добавлять лишний аудит.
    if doc["status"] == "posted":
        posting.unpost(conn, doc_id, DEV_USER_ID)
    conn.execute("UPDATE doc_goods_receipt SET status = %s, posted_at = NULL WHERE id = %s",
                 ("deleted" if body.mark else "draft", doc_id))
    _audit(conn, doc_id, "mark", {"mark": body.mark})
    result = _document(conn, doc_id)
    conn.commit()
    return _response(result)
