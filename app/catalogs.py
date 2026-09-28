"""Справочники (FR-01).

Восемь справочников устроены одинаково: код, наименование, пометка на
удаление и несколько своих полей. Поэтому SQL здесь общий, а отличаются
только модели данных (какие поля есть у справочника).

Для каждого справочника создаются адреса:
    GET    /api/v1/catalogs/{имя}?q=&page=&include_deleted=   список + поиск
    GET    /api/v1/catalogs/{имя}/{id}                         один элемент
    POST   /api/v1/catalogs/{имя}                              создать       -> 201
    PUT    /api/v1/catalogs/{имя}/{id}                         изменить
    POST   /api/v1/catalogs/{имя}/{id}/deletion-mark           пометить / снять пометку

Физического удаления нет: вместо DELETE ставится пометка на удаление.
"""
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.config import DEV_USER_ID
from app.db import get_conn
from app.errors import ApiError

PAGE_SIZE = 50

# --- Модели: какие поля пользователь заполняет в каждом справочнике ---------

Code = Field(min_length=1, max_length=20, description="Код элемента, уникальный в справочнике")
Name = Field(min_length=1, max_length=255, description="Наименование")
Bin = Field(default=None, pattern=r"^[0-9]{12}$", description="БИН/ИИН — 12 цифр")


class Organization(BaseModel):
    code: str = Code
    name: str = Name
    bin: str | None = Bin


class Counterparty(BaseModel):
    code: str = Code
    name: str = Name
    bin: str | None = Bin


class Contract(BaseModel):
    code: str = Code
    name: str = Name
    org_id: UUID
    counterparty_id: UUID
    number: str | None = Field(default=None, max_length=50)
    contract_date: date | None = None


class Uom(BaseModel):
    code: str = Code
    name: str = Field(min_length=1, max_length=100)


class Item(BaseModel):
    code: str = Code
    name: str = Name
    kind: Literal["inventory", "fixed_asset", "service"] = Field(
        default="inventory", description="inventory — запасы, fixed_asset — основное средство, service — услуга"
    )
    uom_id: UUID
    batch_tracked: bool = False


class Warehouse(BaseModel):
    code: str = Code
    name: str = Name
    org_id: UUID


class Person(BaseModel):
    code: str = Code
    name: str = Field(min_length=1, max_length=255, description="ФИО")
    iin: str | None = Bin


class Account(BaseModel):
    code: str = Field(min_length=1, max_length=10, description="Номер счёта, например 1310")
    name: str = Name


@dataclass
class Catalog:
    url: str          # имя в адресе: /api/v1/catalogs/{url}
    table: str        # таблица в базе
    title: str        # название для документации
    model: type[BaseModel]


CATALOGS = [
    Catalog("organizations",  "ref_organization", "Организации",       Organization),
    Catalog("counterparties", "ref_counterparty", "Контрагенты",       Counterparty),
    Catalog("contracts",      "ref_contract",     "Договоры",          Contract),
    Catalog("units",          "ref_uom",          "Единицы измерения", Uom),
    Catalog("items",          "ref_item",         "Номенклатура",      Item),
    Catalog("warehouses",     "ref_warehouse",    "Склады",            Warehouse),
    Catalog("persons",        "ref_person",       "Физические лица",   Person),
    Catalog("accounts",       "ref_account",      "План счетов",       Account),
]


class DeletionMark(BaseModel):
    mark: bool = True


# --- Общие операции с таблицей справочника -----------------------------------

def _audit(conn: psycopg.Connection, action: str, table: str, obj_id: UUID, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, action, object_type, object_id, details) "
        "VALUES (%s, %s, %s, %s, %s)",
        (DEV_USER_ID, action, table, obj_id, Jsonb(details)),
    )


def _get_or_404(conn: psycopg.Connection, cat: Catalog, obj_id: UUID) -> dict:
    row = conn.execute(
        sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(cat.table)), (obj_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"Элемент справочника «{cat.title}» не найден")
    return row


def _make_router(cat: Catalog) -> APIRouter:
    router = APIRouter(prefix=f"/api/v1/catalogs/{cat.url}", tags=[cat.title])
    table = sql.Identifier(cat.table)
    Model = cat.model

    @router.get("", summary=f"{cat.title}: список и поиск")
    def list_items(
        q: str | None = Query(None, description="Поиск по коду или наименованию"),
        page: int = Query(1, ge=1),
        include_deleted: bool = Query(False, description="Показывать помеченные на удаление"),
        conn: psycopg.Connection = Depends(get_conn),
    ):
        where = sql.SQL("(%(all)s OR NOT deletion_mark) AND "
                        "(%(q)s::text IS NULL OR code ILIKE %(like)s OR name ILIKE %(like)s)")
        params = {"all": include_deleted, "q": q, "like": f"%{q}%", "limit": PAGE_SIZE,
                  "offset": (page - 1) * PAGE_SIZE}
        total = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {} WHERE ").format(table) + where, params
        ).fetchone()["n"]
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} WHERE ").format(table) + where
            + sql.SQL(" ORDER BY code LIMIT %(limit)s OFFSET %(offset)s"),
            params,
        ).fetchall()
        return {"items": rows, "total": total, "page": page, "page_size": PAGE_SIZE}

    @router.get("/{obj_id}", summary=f"{cat.title}: получить элемент")
    def get_item(obj_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
        return _get_or_404(conn, cat, obj_id)

    @router.post("", status_code=201, summary=f"{cat.title}: создать элемент")
    def create_item(body: Model, conn: psycopg.Connection = Depends(get_conn)):  # type: ignore[valid-type]
        data = body.model_dump()
        cols = list(data)
        row = conn.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                table,
                sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
            ),
            data,
        ).fetchone()
        _audit(conn, "create", cat.table, row["id"], {"code": row["code"]})
        conn.commit()
        return row

    @router.put("/{obj_id}", summary=f"{cat.title}: изменить элемент")
    def update_item(obj_id: UUID, body: Model, conn: psycopg.Connection = Depends(get_conn)):  # type: ignore[valid-type]
        _get_or_404(conn, cat, obj_id)
        data = body.model_dump()
        row = conn.execute(
            sql.SQL("UPDATE {} SET {} WHERE id = %(id)s RETURNING *").format(
                table,
                sql.SQL(", ").join(
                    sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder(c)) for c in data
                ),
            ),
            {**data, "id": obj_id},
        ).fetchone()
        _audit(conn, "update", cat.table, obj_id, {"code": row["code"]})
        conn.commit()
        return row

    @router.post("/{obj_id}/deletion-mark", summary=f"{cat.title}: пометить на удаление / снять пометку")
    def set_deletion_mark(obj_id: UUID, body: DeletionMark, conn: psycopg.Connection = Depends(get_conn)):
        _get_or_404(conn, cat, obj_id)
        row = conn.execute(
            sql.SQL("UPDATE {} SET deletion_mark = %s WHERE id = %s RETURNING *").format(table),
            (body.mark, obj_id),
        ).fetchone()
        _audit(conn, "mark", cat.table, obj_id, {"deletion_mark": body.mark})
        conn.commit()
        return row

    return router


routers = [_make_router(c) for c in CATALOGS]
