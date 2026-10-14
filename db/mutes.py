"""
db/mutes.py — антиспам-ограничения. Хранятся в базе, чтобы перезапуск
бота посреди ивента не снимал их со всех разом.
"""

from db.pool import get_pool


async def save_mute(user_id: int, seconds: int, reason: str = "") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mutes (user_id, muted_until, reason)
            VALUES ($1, NOW() + make_interval(secs => $2), $3)
            ON CONFLICT (user_id) DO UPDATE
            SET muted_until = EXCLUDED.muted_until, reason = EXCLUDED.reason
            """,
            user_id, float(seconds), reason)


async def clear_mute(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mutes WHERE user_id = $1", user_id)


async def load_active_mutes() -> list[tuple[int, float]]:
    """Активные ограничения: [(user_id, секунд осталось)]. Истёкшие удаляются."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mutes WHERE muted_until <= NOW()")
        rows = await conn.fetch(
            "SELECT user_id, EXTRACT(EPOCH FROM (muted_until - NOW())) AS left_sec "
            "FROM mutes")
    return [(r["user_id"], float(r["left_sec"])) for r in rows if r["left_sec"] > 0]
