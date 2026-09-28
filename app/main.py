"""Точка входа: uvicorn app.main:app --reload

Документация API (Swagger) после запуска: http://localhost:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import catalogs, errors
from app.db import pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(
    title="Учётная система — MVP «Поступление запасов»",
    version="0.1.0",
    lifespan=lifespan,
)
errors.install(app)

for router in catalogs.routers:
    app.include_router(router)


@app.get("/health", tags=["Служебное"])
def health():
    return {"status": "ok"}
