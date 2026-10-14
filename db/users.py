"""
db/users.py — участники: регистрация, статус сверки, поиск.
"""

from datetime import datetime, timezone

import asyncpg

from config import STATUS_MANUAL, STATUS_NOT_FOUND, STATUS_PENDING
from db.pool import get_pool
from db.delegates import normalize_phone, normalize_username


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ensure_user(user_id: int, username: str, full_name: str = "") -> asyncpg.Record:
    """
    Создаёт участника при первом /start либо обновляет ник, имя и время
    входа. first_seen ставится один раз. Возвращает актуальную строку.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO users (user_id, username, full_name, first_seen, last_seen)
            VALUES ($1, $2, $3, $4, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                username   = EXCLUDED.username,
                full_name  = COALESCE(NULLIF(EXCLUDED.full_name, ''), users.full_name),
                first_seen = COALESCE(users.first_seen, EXCLUDED.first_seen),
                last_seen  = EXCLUDED.last_seen
            RETURNING *
            """,
            user_id, username or "", full_name or "", _now(),
        )


async def get_user(user_id: int) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)


async def get_user_status(user_id: int) -> str:
    """Статус сверки. Для неизвестного участника — pending."""
    user = await get_user(user_id)
    return (user["status"] if user and user["status"] else STATUS_PENDING)


async def find_user_by_username(username: str) -> asyncpg.Record | None:
    norm = normalize_username(username)
    if not norm:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE LOWER(username) = $1", norm
        )


async def find_user(query: str) -> asyncpg.Record | None:
    """
    Ищет участника по тому, что ввёл админ: Telegram ID, @нику,
    ссылке t.me/… или номеру телефона.
    """
    value = query.strip().lstrip("@")
    if value.isdigit() and len(value) < 12:
        user = await get_user(int(value))
        if user:
            return user
    user = await find_user_by_username(value)
    if user:
        return user
    phone = normalize_phone(value)
    if len(phone) >= 10:
        # Телефон хранится как пришёл («+7 (700) …»), сравниваем только цифры
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE regexp_replace(phone, '\\D', '', 'g') LIKE $1",
                f"%{phone}",
            )
    return None


async def save_screening_result(user_id: int, status: str, comment: str,
                                delegate_row: int | None,
                                phone: str = "") -> asyncpg.Record | None:
    """Записывает результат сверки и телефон одним запросом."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE users
            SET status = $1,
                status_comment = $2,
                delegate_row = COALESCE($3, delegate_row),
                phone = COALESCE(NULLIF($4, ''), phone)
            WHERE user_id = $5
            RETURNING *
            """,
            status, comment, delegate_row, phone or "", user_id,
        )


async def set_manual_decision(user_id: int, status: str, comment: str) -> None:
    """
    Ручное решение админа. Блокирует статус: повторная автоматическая
    сверка («Проверить снова») его не перебьёт.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = $1, status_comment = $2, status_locked = TRUE "
            "WHERE user_id = $3",
            status, comment, user_id,
        )


async def count_users() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)


async def count_by_status(status: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE status = $1", status) or 0)


async def list_user_ids_by_status(status: str, limit: int = 500) -> list[int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM users WHERE status = $1 "
            "ORDER BY last_seen DESC NULLS LAST LIMIT $2",
            status, limit,
        )
    return [r["user_id"] for r in rows]


async def list_unverified(limit: int = 50) -> list[asyncpg.Record]:
    """Кто ждёт решения: не сверен или не найден в списке. Для /pending."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, username, full_name, phone, status, status_comment "
            "FROM users WHERE status IN ($1, $2) "
            "ORDER BY last_seen DESC NULLS LAST LIMIT $3",
            STATUS_PENDING, STATUS_NOT_FOUND, limit,
        )


async def all_users() -> list[asyncpg.Record]:
    """Все участники — для дозалива в таблицу и выгрузок."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM users ORDER BY first_seen ASC NULLS LAST, user_id ASC"
        )


async def restore_users(rows: list[dict]) -> int:
    """
    Восстанавливает участников из листа «Регистрации», если база
    потерялась. Существующие записи не трогаются. Ручные решения
    админов восстанавливаются заблокированными.
    """
    if not rows:
        return 0
    restored = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                result = await conn.execute(
                    """
                    INSERT INTO users (user_id, username, full_name, phone, status,
                                       status_comment, first_seen, last_seen,
                                       status_locked)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    row["user_id"], row["username"], row["full_name"], row["phone"],
                    row["status"], row["status_comment"],
                    row["first_seen"] or None,
                    row.get("last_seen") or row["first_seen"] or None,
                    row["status"] == STATUS_MANUAL
                    or "вручную" in (row["status_comment"] or ""),
                )
                if result != "INSERT 0 0":
                    restored += 1
    return restored
