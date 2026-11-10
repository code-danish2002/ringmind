"""Postgres connection pool and schema migration."""

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, command_timeout=10)
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
        log.info("Database ready")
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised; call init_pool() at startup")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    try:
        async with pool().acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1
    except Exception:
        return False
