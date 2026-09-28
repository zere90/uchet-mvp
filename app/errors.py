"""Единый формат ошибок API (PRD, раздел 11).

{
  "error": "validation_failed",
  "message": "Данные не сохранены",
  "details": [{"field": "code", "code": "duplicate", "message": "..."}]
}

Текст message пишется для бухгалтера, код error/code — для фронтенда.
"""
import re

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import errors as pg


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str, details: list | None = None):
        self.status = status
        self.error = error
        self.message = message
        self.details = details or []


def detail(field: str, code: str, message: str) -> dict:
    return {"field": field, "code": code, "message": message}


def _field_from_pg(e: psycopg.Error) -> str:
    """Достаёт имя колонки из текста ошибки PostgreSQL: 'Key (code)=(…)'."""
    m = re.search(r"Key \(([\w, ]+)\)", (e.diag.message_detail or ""))
    return m.group(1) if m else (e.diag.column_name or "")


# Сообщения pydantic -> понятный текст для бухгалтера
_PYDANTIC_MESSAGES = {
    "missing": "Обязательное поле не заполнено",
    "string_too_short": "Поле не может быть пустым",
    "string_too_long": "Слишком длинное значение",
    "string_pattern_mismatch": "Неверный формат",
    "uuid_parsing": "Неверная ссылка (ожидается UUID)",
    "literal_error": "Недопустимое значение",
    "date_from_datetime_parsing": "Неверная дата",
    "date_parsing": "Неверная дата",
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(
            status_code=e.status,
            content={"error": e.error, "message": e.message, "details": e.details},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        details = []
        for err in e.errors():
            loc = [str(x) for x in err["loc"] if x != "body"]
            details.append(detail(
                ".".join(loc),
                err["type"],
                _PYDANTIC_MESSAGES.get(err["type"], err["msg"]),
            ))
        return JSONResponse(
            status_code=422,
            content={"error": "validation_failed", "message": "Данные не сохранены", "details": details},
        )

    @app.exception_handler(pg.UniqueViolation)
    async def _unique(_: Request, e: pg.UniqueViolation):
        field = _field_from_pg(e)
        return JSONResponse(status_code=422, content={
            "error": "validation_failed", "message": "Данные не сохранены",
            "details": [detail(field, "duplicate", "Такое значение уже есть в справочнике")],
        })

    @app.exception_handler(pg.ForeignKeyViolation)
    async def _fk(_: Request, e: pg.ForeignKeyViolation):
        field = _field_from_pg(e)
        return JSONResponse(status_code=422, content={
            "error": "validation_failed", "message": "Данные не сохранены",
            "details": [detail(field, "not_found", "Выбранный элемент справочника не найден")],
        })

    @app.exception_handler(pg.CheckViolation)
    async def _check(_: Request, e: pg.CheckViolation):
        return JSONResponse(status_code=422, content={
            "error": "validation_failed", "message": "Данные не сохранены",
            "details": [detail(e.diag.constraint_name or "", "check_failed", "Недопустимое значение")],
        })
