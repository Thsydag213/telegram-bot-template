"""
db/resets.py — три уровня сброса. Каждый сначала считает, что затронет
(это показывается админу до подтверждения), потом удаляет.

  game          — пруфы, засчитанные задания, победы, антиспам
  registrations — статусы сверки, телефоны, ручные решения
  everything    — всё вместе, участники удаляются целиком

Задания, фото-образцы и адженда не удаляются никогда.
"""

import logging

from config import STATUS_PENDING
from db.pool import get_pool

logger = logging.getLogger(__name__)


async def _count(sql: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(sql) or 0)


async def _marked_delegate_rows(conn) -> list[int]:
    rows = await conn.fetch(
        "SELECT DISTINCT delegate_row FROM users WHERE delegate_row IS NOT NULL")
    return [r["delegate_row"] for r in rows]


# ── Игра ──────────────────────────────────────────────────────────────────────

async def count_game() -> dict[str, int]:
    return {
        "сданных пруфов": await _count("SELECT COUNT(*) FROM task_submissions"),
        "из них ждут проверки": await _count(
            "SELECT COUNT(*) FROM task_submissions WHERE status = 'pending'"),
        "участников с прогрессом": await _count(
            "SELECT COUNT(DISTINCT user_id) FROM completions"),
        "победителей": await _count("SELECT COUNT(*) FROM users WHERE notified_win = 1"),
        "антиспам-ограничений": await _count("SELECT COUNT(*) FROM mutes"),
    }


async def reset_game() -> dict[str, int]:
    removed = await count_game()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM task_submissions")
            await conn.execute("DELETE FROM completions")
            await conn.execute("UPDATE users SET notified_win = 0, ts_win = NULL")
            await conn.execute("DELETE FROM mutes")
    logger.warning("СБРОС ИГРЫ: %s", removed)
    return removed


# ── Регистрации ──────────────────────────────────────────────────────────────

async def count_registrations() -> dict[str, int]:
    return {
        "участников": await _count("SELECT COUNT(*) FROM users"),
        "подтверждённых": await _count(
            "SELECT COUNT(*) FROM users WHERE status IN ('confirmed', 'manual')"),
        "ручных решений админов": await _count(
            "SELECT COUNT(*) FROM users WHERE status_locked"),
        "сохранённых телефонов": await _count(
            "SELECT COUNT(*) FROM users WHERE COALESCE(phone, '') <> ''"),
    }


async def reset_registrations() -> tuple[dict[str, int], list[int]]:
    """
    Все участники снова «не сверены». Прогресс в игре остаётся, но играть
    можно только после новой сверки. Возвращает (что затронуто, строки
    листа «Делегаты» с отметками бота — их нужно очистить в таблице).
    """
    affected = await count_registrations()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            marked = await _marked_delegate_rows(conn)
            await conn.execute(
                "UPDATE users SET status = $1, status_comment = NULL, "
                "delegate_row = NULL, phone = NULL, status_locked = FALSE",
                STATUS_PENDING)
    logger.warning("СБРОС РЕГИСТРАЦИЙ: %s", affected)
    return affected, marked


# ── Всё ───────────────────────────────────────────────────────────────────────

async def count_everything() -> dict[str, int]:
    return {
        "участников": await _count("SELECT COUNT(*) FROM users"),
        "подтверждённых": await _count(
            "SELECT COUNT(*) FROM users WHERE status IN ('confirmed', 'manual')"),
        "сданных пруфов": await _count("SELECT COUNT(*) FROM task_submissions"),
        "засчитанных заданий": await _count("SELECT COUNT(*) FROM completions"),
        "записей в кэше списка участников": await _count("SELECT COUNT(*) FROM delegates"),
        "антиспам-ограничений": await _count("SELECT COUNT(*) FROM mutes"),
    }


async def reset_everything() -> tuple[dict[str, int], list[int]]:
    """
    Удаляет участников со всем, что к ним относится, и кэш списка
    участников (он перечитывается из таблицы). Лист «Делегаты» в самой
    таблице не трогается — это данные организаторов.
    """
    removed = await count_everything()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            marked = await _marked_delegate_rows(conn)
            await conn.execute("DELETE FROM task_submissions")
            await conn.execute("DELETE FROM completions")
            await conn.execute("DELETE FROM users")
            await conn.execute("DELETE FROM delegates")
            await conn.execute("DELETE FROM mutes")
            await conn.execute("DELETE FROM fsm_states")
    logger.warning("ПОЛНЫЙ СБРОС: %s", removed)
    return removed, marked
