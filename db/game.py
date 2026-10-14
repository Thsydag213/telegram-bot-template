"""
db/game.py — бинго: задания, пруфы, засчитанные задания, победы.

Жизненный цикл пруфа:
  участник отправил  → task_submissions, статус 'pending'
  админ нажал ✅     → статус 'approved' + строка в completions
  админ нажал ❌     → статус 'rejected', участник может отправить снова

Прогресс считается только по заданиям, которые сейчас есть в
content/tasks.py: если задание убрали, его отметки не влияют на победу.
"""

import json
import logging
from datetime import datetime, timezone

import asyncpg

from config import TASKS
from content.tasks import TASKS_DATA
from db.pool import get_pool

logger = logging.getLogger(__name__)


def _current_numbers() -> list[int]:
    return list(TASKS)


# ── Задания ───────────────────────────────────────────────────────────────────

async def get_all_tasks() -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM tasks WHERE task_number = ANY($1::int[]) ORDER BY task_number",
            _current_numbers(),
        )


async def get_task_by_number(task_number: int) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM tasks WHERE task_number = $1", task_number)


async def reseed_tasks(force: bool = False) -> tuple[int, int, list[int]]:
    """
    Перезаливает названия, описания и типы сдачи из content/tasks.py.
    Фото-образцы сохраняются. Описания, которые админ поменял вручную,
    не трогаются без force=True.

    Возвращает (обновлено, добавлено, номера с сохранённым ручным описанием).
    """
    updated = inserted = 0
    kept: list[int] = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not force:
                rows = await conn.fetch(
                    "SELECT task_number FROM tasks WHERE description_custom "
                    "AND task_number = ANY($1::int[])", _current_numbers())
                kept = sorted(r["task_number"] for r in rows)
            for task in TASKS_DATA:
                result = await conn.execute(
                    """
                    UPDATE tasks
                    SET name = $1,
                        description = CASE WHEN description_custom AND NOT $5
                                           THEN description ELSE $2 END,
                        description_custom = description_custom AND NOT $5,
                        submission_type = $3,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_number = $4
                    """,
                    task["name"], task["description"], task["submission_type"],
                    task["task_number"], force,
                )
                if result == "UPDATE 0":
                    await conn.execute(
                        "INSERT INTO tasks (task_number, name, description, submission_type) "
                        "VALUES ($1, $2, $3, $4) ON CONFLICT (task_number) DO NOTHING",
                        task["task_number"], task["name"], task["description"],
                        task["submission_type"],
                    )
                    inserted += 1
                else:
                    updated += 1
            # Задания, которых больше нет в файле, не удаляем: на них
            # ссылаются сданные пруфы. Они просто перестают показываться.
            stale = await conn.fetch(
                "SELECT task_number, name FROM tasks WHERE task_number <> ALL($1::int[])",
                _current_numbers())
            for row in stale:
                logger.warning("В базе есть задание #%s «%s», которого нет в content/tasks.py",
                               row["task_number"], row["name"])
    return updated, inserted, kept


async def update_task_reference_photo(task_number: int, photo_file_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE tasks SET reference_photo_url = $1, updated_at = CURRENT_TIMESTAMP "
            "WHERE task_number = $2", photo_file_id, task_number)
    return result != "UPDATE 0"


async def update_task_description(task_number: int, description: str) -> bool:
    """Ручная правка описания админом — помечается, чтобы /reseed_tasks её не затёр."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE tasks SET description = $1, description_custom = TRUE, "
            "updated_at = CURRENT_TIMESTAMP WHERE task_number = $2",
            description, task_number)
    return result != "UPDATE 0"


# ── Пруфы ─────────────────────────────────────────────────────────────────────

async def save_task_submission(user_id: int, task_number: int,
                               submission_data: dict) -> asyncpg.Record | None:
    """
    Сохраняет пруф в статусе 'pending'. Возвращает запись (её id уходит
    в кнопки админ-чата) или None, если задания нет.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT id FROM tasks WHERE task_number = $1", task_number)
        if not task:
            return None
        return await conn.fetchrow(
            "INSERT INTO task_submissions (user_id, task_id, submission_data) "
            "VALUES ($1, $2, $3) RETURNING *",
            user_id, task["id"], json.dumps(submission_data, ensure_ascii=False),
        )


async def has_pending_submission(user_id: int, task_number: int) -> bool:
    """Есть ли у участника непроверенный пруф по этому заданию."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            """
            SELECT 1 FROM task_submissions s JOIN tasks t ON t.id = s.task_id
            WHERE s.user_id = $1 AND t.task_number = $2 AND s.status = 'pending'
            LIMIT 1
            """,
            user_id, task_number))


async def review_submission(submission_id: int, status: str, admin_name: str) -> dict:
    """
    Атомарно переводит пруф из 'pending' в 'approved' / 'rejected'.

    Успех: {"ok": True, "user_id", "task_number"}.
    Пруф уже разобран (двойное нажатие, два админа сразу) или удалён
    сбросом игры: {"ok": False, "status": текущий статус или None}.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE task_submissions s
            SET status = $2, reviewed_by = $3, reviewed_at = NOW()
            FROM tasks t
            WHERE s.id = $1 AND s.status = 'pending' AND t.id = s.task_id
            RETURNING s.user_id, t.task_number
            """,
            submission_id, status, admin_name)
        if row:
            return {"ok": True, "user_id": row["user_id"],
                    "task_number": row["task_number"]}
        current = await conn.fetchval(
            "SELECT status FROM task_submissions WHERE id = $1", submission_id)
    return {"ok": False, "status": current}


# ── Засчитанные задания ──────────────────────────────────────────────────────

async def get_user_tasks(user_id: int) -> dict[int, bool]:
    """{номер задания: выполнено ли} по всем текущим заданиям."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT task_number FROM completions WHERE user_id = $1", user_id)
    done = {r["task_number"] for r in rows}
    return {n: n in done for n in TASKS}


async def is_task_done(user_id: int, task_number: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM completions WHERE user_id = $1 AND task_number = $2",
            user_id, task_number))


async def complete_task(user_id: int, task_number: int) -> int:
    """
    Засчитывает задание. Повторный вызов ничего не меняет.
    Возвращает, сколько текущих заданий у участника выполнено.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO completions (user_id, task_number) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING", user_id, task_number)
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM completions "
            "WHERE user_id = $1 AND task_number = ANY($2::int[])",
            user_id, _current_numbers()) or 0)


async def all_completions() -> dict[int, dict[int, datetime]]:
    """{user_id: {номер задания: когда засчитано}} — для выгрузок и дозалива."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, task_number, completed_at FROM completions "
            "WHERE task_number = ANY($1::int[])", _current_numbers())
    result: dict[int, dict[int, datetime]] = {}
    for r in rows:
        result.setdefault(r["user_id"], {})[r["task_number"]] = r["completed_at"]
    return result


async def try_mark_winner(user_id: int) -> bool:
    """
    Отмечает победу, если её ещё не было. True — победа зафиксирована
    именно сейчас (значит, нужно поздравить). Атомарно: два одобрения
    подряд не поздравят дважды.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET notified_win = 1, ts_win = $1 "
            "WHERE user_id = $2 AND notified_win = 0",
            datetime.now(timezone.utc).isoformat(), user_id)
    return result != "UPDATE 0"


async def restore_progress(rows: list[dict]) -> int:
    """
    Восстанавливает засчитанные задания из листа «Прогресс». Уже
    засчитанные не трогаются. Победа восстанавливается вместе с флагом
    notified_win, чтобы победитель не получил поздравление второй раз.
    """
    if not rows:
        return 0
    restored = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                exists = await conn.fetchval(
                    "SELECT 1 FROM users WHERE user_id = $1", row["user_id"])
                if not exists:
                    continue
                await conn.executemany(
                    "INSERT INTO completions (user_id, task_number) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    [(row["user_id"], n) for n, done in row["done"].items() if done],
                )
                if row.get("ts_win"):
                    await conn.execute(
                        "UPDATE users SET ts_win = COALESCE(ts_win, $2), notified_win = 1 "
                        "WHERE user_id = $1", row["user_id"], row["ts_win"])
                restored += 1
    return restored
