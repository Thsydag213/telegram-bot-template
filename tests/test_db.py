"""Слой данных на живом PostgreSQL."""

import asyncio

from config import STATUS_CONFIRMED, STATUS_MANUAL, STATUS_NOT_FOUND, STATUS_PENDING, TASKS
from db import game, resets, users
from db.exports import export_csv, export_xlsx
from db.schema import init_db
from services.timefmt import local_to_iso, to_local

FIRST, SECOND = list(TASKS)[:2]


# ── Миграция ──────────────────────────────────────────────────────────────────

async def test_migration_from_task_columns(db):
    """База старой версии: прогресс колонками task_N/ts_N, пруфы без статуса."""
    async with db.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute("""
            CREATE TABLE users (user_id BIGINT PRIMARY KEY, username TEXT,
                task_1 INTEGER NOT NULL DEFAULT 0, ts_1 TEXT,
                task_2 INTEGER NOT NULL DEFAULT 0, ts_2 TEXT,
                notified_win INTEGER NOT NULL DEFAULT 0, ts_win TEXT)""")
        await conn.execute("""
            CREATE TABLE tasks (id SERIAL PRIMARY KEY, task_number INT UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL, description TEXT NOT NULL,
                submission_type VARCHAR(100) NOT NULL, reference_photo_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await conn.execute("""
            CREATE TABLE task_submissions (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
                task_id INT NOT NULL REFERENCES tasks(id), submission_data JSONB NOT NULL,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await conn.execute(
            "INSERT INTO users (user_id, username, task_1, ts_1, task_2, ts_2) VALUES "
            "(7, 'old', 1, '2026-08-31T07:00:00+00:00', 1, 'мусор'), (8, 'none', 0, NULL, 0, NULL)")
        await conn.execute(
            "INSERT INTO tasks (task_number, name, description, submission_type) "
            "VALUES (1, 'a', 'b', 'photo')")
        await conn.execute(
            "INSERT INTO task_submissions (user_id, task_id, submission_data) VALUES (7, 1, '{}')")

    await init_db()
    await init_db()  # повторный старт ничего не ломает

    async with db.acquire() as conn:
        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'")}
        rows = await conn.fetch("SELECT * FROM completions ORDER BY task_number")
        legacy = await conn.fetchval("SELECT status FROM task_submissions WHERE user_id = 7")

    assert not any(c.startswith(("task_", "ts_")) for c in cols - {"ts_win"})
    assert [(r["user_id"], r["task_number"]) for r in rows] == [(7, 1), (7, 2)]
    assert rows[0]["completed_at"].isoformat().startswith("2026-08-31T07:00:00")
    assert (await game.get_user_tasks(7))[1] and not (await game.get_user_tasks(8))[1]
    # Старый пруф ничего не блокирует и кнопкой не решается
    assert legacy == "legacy"
    assert not await game.has_pending_submission(7, 1)
    assert (await game.review_submission(1, "approved", "A"))["status"] == "legacy"


async def test_new_tasks_seeded_on_start(db):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE task_number = $1", SECOND)
    await init_db()
    assert await game.get_task_by_number(SECOND) is not None


# ── Пруфы и прогресс ─────────────────────────────────────────────────────────

async def test_submission_lifecycle(db):
    await users.ensure_user(100, "ivan", "Иван")
    sub = await game.save_task_submission(100, FIRST, {"text": "x"})
    assert sub["status"] == "pending"
    assert await game.has_pending_submission(100, FIRST)

    r1, r2 = await asyncio.gather(
        game.review_submission(sub["id"], "approved", "A"),
        game.review_submission(sub["id"], "approved", "B"),
    )
    assert [r1["ok"], r2["ok"]].count(True) == 1, "засчитан ровно один из двух кликов"
    assert not await game.has_pending_submission(100, FIRST)
    assert (await game.review_submission(sub["id"], "rejected", "C"))["status"] == "approved"
    assert await game.save_task_submission(100, 999, {}) is None


async def test_complete_task_is_idempotent(db):
    await users.ensure_user(100, "ivan")
    assert await game.complete_task(100, FIRST) == 1
    assert await game.complete_task(100, FIRST) == 1
    assert await game.complete_task(100, SECOND) == 2
    assert await game.is_task_done(100, SECOND)


async def test_removed_task_does_not_count(db):
    """Отметка по заданию, которого больше нет в content/tasks.py, не идёт в зачёт."""
    await users.ensure_user(100, "ivan")
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO completions (user_id, task_number) VALUES (100, 999)")
    assert await game.complete_task(100, FIRST) == 1
    assert 999 not in await game.get_user_tasks(100)


async def test_winner_marked_once(db):
    await users.ensure_user(100, "ivan")
    results = await asyncio.gather(game.try_mark_winner(100), game.try_mark_winner(100))
    assert results.count(True) == 1


async def test_reseed_keeps_manual_descriptions(db):
    await game.update_task_description(FIRST, "РУЧНОЕ")
    _, _, kept = await game.reseed_tasks()
    assert kept == [FIRST]
    assert (await game.get_task_by_number(FIRST))["description"] == "РУЧНОЕ"
    await game.reseed_tasks(force=True)
    task = await game.get_task_by_number(FIRST)
    assert task["description"] != "РУЧНОЕ" and not task["description_custom"]


# ── Участники ─────────────────────────────────────────────────────────────────

async def test_find_user(db):
    await users.ensure_user(100, "Ivan_K", "Иван")
    await users.save_screening_result(100, STATUS_CONFIRMED, "", None, "+7 (700) 111-22-33")
    for query in ("100", "@ivan_k", "https://t.me/Ivan_K", "87001112233"):
        assert (await users.find_user(query))["user_id"] == 100, query
    assert await users.find_user("@nobody") is None


async def test_manual_decision_locks_status(db):
    await users.ensure_user(200, "petr")
    await users.set_manual_decision(200, STATUS_NOT_FOUND, "отклонён вручную (A)")
    user = await users.get_user(200)
    assert user["status_locked"] and user["status"] == STATUS_NOT_FOUND


# ── Сбросы ────────────────────────────────────────────────────────────────────

async def _populate(db):
    await users.ensure_user(100, "ivan")
    await users.save_screening_result(100, STATUS_CONFIRMED, "", 7, "+77001112233")
    await users.ensure_user(200, "petr")
    await users.set_manual_decision(200, STATUS_MANUAL, "A")
    await game.complete_task(100, FIRST)
    await game.try_mark_winner(100)
    pending = await game.save_task_submission(100, SECOND, {"text": "y"})
    from db.mutes import save_mute
    await save_mute(100, 600)
    return pending


async def test_reset_game(db):
    pending = await _populate(db)
    counts = await resets.count_game()
    assert counts["из них ждут проверки"] == 1 and counts["победителей"] == 1

    await resets.reset_game()
    user = await users.get_user(100)
    assert not any((await game.get_user_tasks(100)).values())
    assert user["notified_win"] == 0 and user["status"] == STATUS_CONFIRMED
    assert (await game.review_submission(pending["id"], "approved", "A"))["status"] is None


async def test_reset_registrations(db):
    await _populate(db)
    _, rows = await resets.reset_registrations()
    assert rows == [7]
    ivan, petr = await users.get_user(100), await users.get_user(200)
    assert ivan["status"] == STATUS_PENDING and not ivan["phone"]
    assert not petr["status_locked"]
    assert (await game.get_user_tasks(100))[FIRST], "прогресс сохранён"


async def test_reset_everything(db):
    await _populate(db)
    _, rows = await resets.reset_everything()
    assert rows == [7]
    assert await users.count_users() == 0
    assert await game.get_task_by_number(FIRST) is not None, "задания не удаляются"


# ── Восстановление из таблицы ─────────────────────────────────────────────────

async def test_restore_keeps_time_and_wins(db):
    local = "2026-08-31 12:00:00"
    iso = local_to_iso(local)
    assert to_local(iso) == local

    restored = await users.restore_users([{
        "user_id": 300, "username": "x", "full_name": "X", "phone": "",
        "status": STATUS_MANUAL, "status_comment": "подтверждён вручную (A)",
        "first_seen": iso, "last_seen": iso,
    }])
    user = await users.get_user(300)
    assert restored == 1 and to_local(user["first_seen"]) == local
    assert user["status_locked"]

    await game.restore_progress([{"user_id": 300, "done": {n: True for n in TASKS}, "ts_win": iso}])
    user = await users.get_user(300)
    assert all((await game.get_user_tasks(300)).values())
    assert user["notified_win"] == 1 and user["ts_win"] == iso
    assert not await game.try_mark_winner(300), "повторного поздравления не будет"


# ── Выгрузки ──────────────────────────────────────────────────────────────────

async def test_exports(db):
    await _populate(db)
    assert len(await export_xlsx()) > 1000
    csv_text = (await export_csv()).decode("utf-8-sig")
    header, *rows = csv_text.strip().splitlines()
    assert "всего выполнено" in header and len(rows) == 2
    assert rows[0].startswith("100,"), "победитель первым"
