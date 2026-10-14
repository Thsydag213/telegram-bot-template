"""
db/delegates.py — локальный кэш списка участников из Google-таблицы.

Нужен, чтобы сверка продолжала работать, когда Google недоступен.
Ники и телефоны хранятся нормализованными, поэтому сравнение простое.
"""

import logging

import asyncpg

from db.pool import get_pool

logger = logging.getLogger(__name__)


def normalize_username(username: str | None) -> str:
    """@Ivan_K, https://t.me/Ivan_K, ivan_k → ivan_k."""
    if not username:
        return ""
    value = username.strip().lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip().strip("/")


def normalize_phone(phone: str | None) -> str:
    """
    Только цифры, последние 10: так +7 (700) 123-45-67, 87001234567
    и 77001234567 сходятся друг с другом.
    """
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def replace_delegates(delegates: list[dict]) -> int:
    """
    Заменяет кэш данными из таблицы. Пустой список игнорируется: случайно
    очищенная таблица не должна закрыть вход всем участникам.
    """
    if not delegates:
        logger.warning("Список участников пуст — кэш в БД оставлен без изменений")
        return 0
    rows = [
        (
            d.get("row"),
            d.get("full_name", ""),
            normalize_username(d.get("username")),
            normalize_phone(d.get("phone")),
            (d.get("email") or "").strip().lower(),
        )
        for d in delegates
    ]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM delegates")
            await conn.executemany(
                "INSERT INTO delegates (sheet_row, full_name, username, phone, email) "
                "VALUES ($1, $2, $3, $4, $5)",
                rows,
            )
    logger.info("Список участников обновлён: %s записей", len(rows))
    return len(rows)


async def count_delegates() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM delegates") or 0)


async def find_delegate_by_username(username: str) -> asyncpg.Record | None:
    norm = normalize_username(username)
    if not norm:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM delegates WHERE username = $1 LIMIT 1", norm)


async def find_delegate_by_phone(phone: str) -> asyncpg.Record | None:
    norm = normalize_phone(phone)
    if not norm:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM delegates WHERE phone = $1 LIMIT 1", norm)


async def sample_delegates(limit: int = 3) -> list[asyncpg.Record]:
    """Несколько записей — для диагностики командой /delegates."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT full_name, username, phone FROM delegates "
            "ORDER BY sheet_row LIMIT $1", limit)


async def count_unmatchable_delegates() -> int:
    """
    Сколько записей нельзя сверить: нет ни ника, ни телефона. Большое
    число обычно значит, что колонки в таблице распознались неверно.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM delegates "
            "WHERE COALESCE(username, '') = '' AND COALESCE(phone, '') = ''") or 0)
