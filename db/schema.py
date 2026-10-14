"""
db/schema.py — создание схемы и миграции. Вызывается один раз при старте.

Таблицы:
  users            — участники: ник, имя, телефон, статус сверки, победа
  tasks            — задания бинго (засеиваются из content/tasks.py)
  task_submissions — пруфы, у каждого статус pending / approved / rejected
  completions      — засчитанные задания: (участник, номер задания, время)
  delegates        — локальный кэш списка участников из Google-таблицы
  mutes            — антиспам-ограничения, переживают перезапуск
  settings         — редактируемый контент (адженда)
  fsm_states       — состояния диалогов, переживают перезапуск

Всё идемпотентно: повторный запуск ничего не ломает, а база от старой
версии бота доводится до текущей схемы автоматически.
"""

import logging
import re
from datetime import datetime, timezone

import asyncpg

from config import STATUS_PENDING
from content.tasks import TASKS_DATA
from db.pool import get_pool

logger = logging.getLogger(__name__)


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _create_tables(conn)
        await _migrate(conn)
        await _seed_tasks(conn)


async def _create_tables(conn: asyncpg.Connection) -> None:
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id        BIGINT PRIMARY KEY,
            username       TEXT,
            full_name      TEXT,
            phone          TEXT,
            status         TEXT NOT NULL DEFAULT '{STATUS_PENDING}',
            status_comment TEXT,
            status_locked  BOOLEAN NOT NULL DEFAULT FALSE,
            delegate_row   INTEGER,
            first_seen     TEXT,
            last_seen      TEXT,
            notified_win   INTEGER NOT NULL DEFAULT 0,
            ts_win         TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id                  SERIAL PRIMARY KEY,
            task_number         INT UNIQUE NOT NULL,
            name                VARCHAR(255) NOT NULL,
            description         TEXT NOT NULL,
            description_custom  BOOLEAN NOT NULL DEFAULT FALSE,
            submission_type     VARCHAR(100) NOT NULL,
            reference_photo_url TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS task_submissions (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL,
            task_id         INT NOT NULL REFERENCES tasks(id),
            submission_data JSONB NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            reviewed_by     TEXT,
            reviewed_at     TIMESTAMPTZ,
            submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_submissions_user ON task_submissions(user_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_submissions_task ON task_submissions(task_id)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS completions (
            user_id      BIGINT NOT NULL,
            task_number  INT NOT NULL,
            completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, task_number)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS delegates (
            id        SERIAL PRIMARY KEY,
            sheet_row INTEGER,
            full_name TEXT,
            username  TEXT,
            phone     TEXT,
            email     TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_delegates_username ON delegates(username)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_delegates_phone ON delegates(phone)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            user_id     BIGINT PRIMARY KEY,
            muted_until TIMESTAMPTZ NOT NULL,
            reason      TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fsm_states (
            key        TEXT PRIMARY KEY,
            state      TEXT,
            data       JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


# ── Миграции со старых версий ────────────────────────────────────────────────

async def _columns(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1",
        table,
    )
    return {r["column_name"] for r in rows}


async def _migrate(conn: asyncpg.Connection) -> None:
    # Колонки, появившиеся в users в разных версиях
    users_cols = await _columns(conn, "users")
    for name, definition in (
        ("full_name", "TEXT"),
        ("phone", "TEXT"),
        ("status", f"TEXT NOT NULL DEFAULT '{STATUS_PENDING}'"),
        ("status_comment", "TEXT"),
        ("status_locked", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("delegate_row", "INTEGER"),
        ("first_seen", "TEXT"),
        ("last_seen", "TEXT"),
        ("notified_win", "INTEGER NOT NULL DEFAULT 0"),
        ("ts_win", "TEXT"),
    ):
        if name not in users_cols:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

    await conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS "
        "description_custom BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # Статус пруфа. Пруфы, созданные до его появления, помечаются 'legacy':
    # решить их кнопкой нельзя, и новую отправку они не блокируют.
    if "status" not in await _columns(conn, "task_submissions"):
        await conn.execute(
            "ALTER TABLE task_submissions ADD COLUMN status TEXT NOT NULL DEFAULT 'legacy'"
        )
        await conn.execute(
            "ALTER TABLE task_submissions ALTER COLUMN status SET DEFAULT 'pending'"
        )
    await conn.execute(
        "ALTER TABLE task_submissions ADD COLUMN IF NOT EXISTS reviewed_by TEXT"
    )
    await conn.execute(
        "ALTER TABLE task_submissions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ"
    )

    await _migrate_task_columns(conn, users_cols)


def _parse_ts(value: str | None) -> datetime:
    """ISO-время из старой колонки ts_N. Мусор и пустота — текущее время."""
    try:
        dt = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _migrate_task_columns(conn: asyncpg.Connection, users_cols: set[str]) -> None:
    """
    Старые версии хранили прогресс колонками users.task_1…task_N и ts_1…ts_N.
    Из-за этого любое изменение числа заданий меняло схему. Переносим
    отметки в таблицу completions и удаляем колонки.
    """
    numbers = sorted(
        int(c[5:]) for c in users_cols if re.fullmatch(r"task_\d+", c)
    )
    if not numbers:
        return

    async with conn.transaction():
        moved = 0
        for n in numbers:
            ts_col = f"ts_{n}" if f"ts_{n}" in users_cols else "NULL"
            rows = await conn.fetch(
                f"SELECT user_id, {ts_col} AS ts FROM users WHERE task_{n} = 1"
            )
            await conn.executemany(
                "INSERT INTO completions (user_id, task_number, completed_at) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                [(r["user_id"], n, _parse_ts(r["ts"])) for r in rows],
            )
            moved += len(rows)
        for n in numbers:
            await conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS task_{n}")
            await conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS ts_{n}")

    logger.warning(
        "Миграция: прогресс перенесён из колонок task_1…task_%s в таблицу "
        "completions (%s отметок)", max(numbers), moved,
    )


async def _seed_tasks(conn: asyncpg.Connection) -> None:
    """
    Досеивает задания, которых ещё нет в базе. Существующие не трогаются —
    их обновляет /reseed_tasks.
    """
    await conn.executemany(
        "INSERT INTO tasks (task_number, name, description, submission_type) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT (task_number) DO NOTHING",
        [(t["task_number"], t["name"], t["description"], t["submission_type"])
         for t in TASKS_DATA],
    )
