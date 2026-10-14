"""Сценарии хендлеров с фейковым Telegram на живой базе."""

import asyncio

from conftest import ADMIN_ID, answers, fake_callback, fake_message
from config import STATUS_NOT_FOUND, TASKS
from content import texts
from db import game, users
from db.delegates import replace_delegates
from handlers import admin, agenda, bingo, review, start, tasks_admin
from services import admin_queue, screening

FIRST = list(TASKS)[0]
DELEGATE = {"row": 2, "full_name": "Иван", "username": "ivan", "phone": "+77001112233"}


async def _with_delegate():
    await replace_delegates([DELEGATE])
    screening.reset_cache()


async def _confirmed(user_id=10, username="ivan", make_state=None, bot=None):
    await _with_delegate()
    await start.cmd_start(fake_message(user_id, "/start", username), make_state(user_id), bot)


# ── Сверка ────────────────────────────────────────────────────────────────────

async def test_start_confirms_by_username(db, bot, make_state):
    await _with_delegate()
    message = fake_message(10, "/start", "ivan")
    await start.cmd_start(message, make_state(10), bot)
    assert "подтверждена" in answers(message)
    assert (await users.get_user(10))["status"] == "confirmed"


async def test_contact_screening_by_phone(db, bot, make_state):
    await _with_delegate()
    state = make_state(11)
    await start.cmd_start(fake_message(11, "/start", "someone_else"), state, bot)
    assert await state.get_state() is not None, "бот ждёт контакт"

    message = fake_message(11, "", "someone_else")
    message.contact = type("C", (), {"user_id": 11, "phone_number": "87001112233"})()
    await start.screening_contact(message, state, bot)
    assert "подтверждена" in answers(message)


async def test_foreign_contact_rejected(db, bot, make_state):
    await _with_delegate()
    message = fake_message(11)
    message.contact = type("C", (), {"user_id": 999, "phone_number": "87001112233"})()
    await start.screening_contact(message, make_state(11), bot)
    assert texts.WRONG_CONTACT in answers(message)


async def test_manual_rejection_not_overridden_by_retry(db, bot, make_state):
    await _with_delegate()
    await users.ensure_user(20, "ivan", "Самозванец")  # ник совпадает со списком
    await screening.set_manual_status(20, STATUS_NOT_FOUND, "A")
    callback = fake_callback(20, "screen:retry", username="ivan")
    await start.screening_retry(callback, make_state(20), bot)
    assert texts.MANUAL_DECISION_FINAL in answers(callback.message)


async def test_empty_list_does_not_claim_confirmed(db, bot, make_state):
    message = fake_message(30, "/start", "nobody")
    await start.cmd_start(message, make_state(30), bot)
    assert texts.WELCOME_UNVERIFIED in answers(message)
    assert "подтверждена!" not in answers(message)


# ── Бинго ─────────────────────────────────────────────────────────────────────

async def _submit_text(user_id, state, bot, text="мой пруф <script>"):
    await bingo.task_selected(fake_callback(user_id, f"task:{FIRST}"), state)
    await bingo.proof_text(fake_message(user_id, text, "ivan"), state, bot)


async def test_proof_flow_and_double_approve(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    state = make_state(10)
    await _submit_text(10, state, bot)

    item = admin_queue._queue[-1]
    assert item["tag"] == admin_queue.TAG_PROOF

    again = fake_callback(10, f"task:{FIRST}")
    await bingo.task_selected(again, make_state(10))
    assert texts.TASK_ON_REVIEW in again.answer.call_args.args[0]

    async with db.acquire() as conn:
        sid = await conn.fetchval("SELECT id FROM task_submissions WHERE user_id = 10")
    a1 = fake_callback(ADMIN_ID, f"approve:{sid}", full_name="Админ <b>")
    a2 = fake_callback(ADMIN_ID, f"approve:{sid}", full_name="Админ <b>")
    await asyncio.gather(review.approve(a1, bot), review.approve(a2, bot))

    assert (await game.get_user_tasks(10))[FIRST]
    sent = [c for c in bot.send_message.call_args_list
            if c.kwargs.get("chat_id") == 10 and "засчитано" in c.kwargs.get("text", "")]
    assert len(sent) == 1, "участник получил одно уведомление"
    edited = (a1.message.edit_text.call_args or a2.message.edit_text.call_args).kwargs["text"]
    assert "&lt;b&gt;" in edited, "имя админа экранировано"


async def test_proof_text_is_escaped_for_admin_chat(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    await _submit_text(10, make_state(10), bot, text="<b>сломать</b>")
    await admin_queue._queue[-1]["send"]()
    text = bot.send_message.call_args.kwargs["text"]
    assert "&lt;b&gt;сломать" in text


async def test_reject_allows_resubmit(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    await _submit_text(10, make_state(10), bot)
    async with db.acquire() as conn:
        sid = await conn.fetchval("SELECT id FROM task_submissions")
    await review.reject(fake_callback(ADMIN_ID, f"reject:{sid}"), bot)
    assert not await game.has_pending_submission(10, FIRST)


async def test_old_card_format_is_refused(db, bot):
    callback = fake_callback(ADMIN_ID, "approve:10:1")
    await review.approve(callback, bot)
    assert "старой версии" in callback.answer.call_args.args[0]


async def test_winner_congratulated_once(db, bot, make_state, monkeypatch):
    monkeypatch.setattr(review, "WIN_THRESHOLD", 1)
    await _confirmed(make_state=make_state, bot=bot)
    await _submit_text(10, make_state(10), bot)
    async with db.acquire() as conn:
        sid = await conn.fetchval("SELECT id FROM task_submissions")
    await review.approve(fake_callback(ADMIN_ID, f"approve:{sid}"), bot)
    wins = [c for c in bot.send_message.call_args_list if "БИНГО" in c.kwargs.get("text", "")]
    assert len(wins) == 1


async def test_photo_caption_saved(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    state = make_state(10)
    await bingo.task_selected(fake_callback(10, f"task:{FIRST}"), state)
    message = fake_message(10, username="ivan")
    message.photo = [type("P", (), {"file_id": "PHOTO"})()]
    message.caption = "подпись"
    await bingo.proof_photo(message, state, bot)
    async with db.acquire() as conn:
        data = await conn.fetchval("SELECT submission_data FROM task_submissions")
    assert "подпись" in data


# ── Админ ─────────────────────────────────────────────────────────────────────

async def test_reset_requires_exact_word(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    await game.complete_task(10, FIRST)
    state = make_state(ADMIN_ID)

    ask = fake_message(ADMIN_ID, "/reset_game")
    await admin.cmd_reset_game(ask, state)
    assert "СБРОСИТЬ ИГРУ" in answers(ask)

    wrong = fake_message(ADMIN_ID, "да")
    await admin.cmd_reset_confirm(wrong, state)
    assert "Не совпало" in answers(wrong) and (await game.get_user_tasks(10))[FIRST]

    await admin.cmd_reset_confirm(fake_message(ADMIN_ID, "СБРОСИТЬ ИГРУ"), state)
    assert not (await game.get_user_tasks(10))[FIRST]
    assert (await users.get_user(10))["status"] == "confirmed"


async def test_reset_all_cancel_and_confirm(db, bot, make_state):
    await _confirmed(make_state=make_state, bot=bot)
    state = make_state(ADMIN_ID)
    await admin.cmd_reset_all(fake_message(ADMIN_ID), state)
    await admin.cmd_reset_cancel(fake_message(ADMIN_ID, "/cancel"), state)
    assert await users.count_users() == 1 and await state.get_state() is None

    await admin.cmd_reset_all(fake_message(ADMIN_ID), state)
    await admin.cmd_reset_confirm(fake_message(ADMIN_ID, "СБРОСИТЬ ВСЁ"), state)
    assert await users.count_users() == 0


async def test_admin_help_lists_enabled_modules(db):
    message = fake_message(ADMIN_ID, "/admin")
    await admin.cmd_admin(message)
    text = answers(message)
    assert "/pending" in text and "/list_tasks" in text and "/set_agenda" in text
    assert "/resync_sheets" not in text, "без таблицы её команды не показываются"


async def test_upload_task_cancel_and_skip(db, make_state):
    state = make_state(ADMIN_ID)
    await tasks_admin.upload_start(fake_message(ADMIN_ID, "/upload_task"), state)
    await tasks_admin.upload_number(fake_message(ADMIN_ID, str(FIRST)), state)
    await tasks_admin.cancel(fake_message(ADMIN_ID, "/cancel"), state)
    assert await state.get_state() is None

    await tasks_admin.upload_start(fake_message(ADMIN_ID), state)
    await tasks_admin.upload_number(fake_message(ADMIN_ID, str(FIRST)), state)
    await tasks_admin.upload_skip_photo(fake_message(ADMIN_ID, "/skip"), state)
    await tasks_admin.upload_description(fake_message(ADMIN_ID, "новое описание"), state)
    task = await game.get_task_by_number(FIRST)
    assert task["description"] == "новое описание" and task["description_custom"]


async def test_agenda_text_replaces_photo(db, make_state):
    state = make_state(ADMIN_ID)
    photo = fake_message(ADMIN_ID)
    photo.photo = [type("P", (), {"file_id": "OLD"})()]
    photo.caption = photo.html_text = "старая"
    await agenda.set_agenda_photo(photo, state)
    await agenda.set_agenda_text(fake_message(ADMIN_ID, "новая программа"), state)

    viewer = fake_message(50)
    await agenda.cmd_agenda(viewer)
    assert not viewer.answer_photo.called, "старая картинка не показывается"
    assert "новая программа" in answers(viewer)
