"""
handlers/bingo.py — игра со стороны участника (модуль FEATURE_BINGO).

  кнопка «Бинго» / /bingo → сетка заданий
  нажатие на задание      → условие + фото-образец, бот ждёт пруф
  пруф (фото, альбом, текст, ссылка) → в базу и в админ-чат на проверку
  /tasks                  → все задания списком

Пока пруф на проверке, второй по тому же заданию не принимается.
"""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from config import ADMIN_CHAT_ID, TASK_DESCRIPTIONS, TASK_SUBMISSION_TYPES, TASKS, WIN_THRESHOLD
from content import texts
from db.game import (
    get_all_tasks,
    get_task_by_number,
    get_user_tasks,
    has_pending_submission,
    is_task_done,
    save_task_submission,
)
from services import admin_queue, antispam
from services.screening import can_play
from tg.keyboards import (
    admin_review_keyboard,
    admin_unmute_keyboard,
    back_to_menu_keyboard,
    bingo_keyboard,
    retry_check_keyboard,
)
from tg.states import ProofUpload

logger = logging.getLogger(__name__)
router = Router(name="bingo")

_MESSAGE_LIMIT = 4096


def _nick(user) -> str:
    """Как показать участника в админ-чате: @ник или имя, безопасно для HTML."""
    if user.username:
        return f"@{user.username}"
    return html.escape(user.full_name or f"ID {user.id}")


# ── Сетка заданий ─────────────────────────────────────────────────────────────

async def _send_bingo(message: Message, user_id: int) -> None:
    if not await can_play(user_id):
        await message.answer(texts.BINGO_LOCKED, reply_markup=retry_check_keyboard())
        return
    done = await get_user_tasks(user_id)
    await message.answer(
        texts.BINGO_HEADER.format(done=sum(done.values()), total=WIN_THRESHOLD),
        reply_markup=bingo_keyboard(done),
    )


@router.message(Command("bingo"))
async def cmd_bingo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_bingo(message, message.from_user.id)


@router.callback_query(F.data == "menu:bingo")
async def menu_bingo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _send_bingo(callback.message, callback.from_user.id)
    await callback.answer()


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    if not await can_play(message.from_user.id):
        await message.answer(texts.BINGO_LOCKED, reply_markup=retry_check_keyboard())
        return

    lines = [texts.TASKS_LIST_HEADER]
    for task in await get_all_tasks():
        hint = texts.SUBMISSION_HINTS.get(task["submission_type"], task["submission_type"])
        lines.append(f"<b>#{task['task_number']}. {html.escape(task['name'])}</b>\n"
                     f"  {task['description']}\n  <i>{hint}</i>")

    # Длинный список режем на сообщения по лимиту Telegram
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 2 > _MESSAGE_LIMIT:
            await message.answer(chunk.strip())
            chunk = ""
        chunk += line + "\n\n"
    await message.answer(chunk.strip(), reply_markup=back_to_menu_keyboard())


# ── Выбор задания ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("task:"))
async def task_selected(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    try:
        task_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return

    if not await can_play(user.id):
        await callback.answer(texts.BINGO_LOCKED_ALERT, show_alert=True)
        return
    muted, minutes = antispam.is_muted(user.id)
    if muted:
        await callback.answer(texts.MUTED_ALERT.format(minutes=minutes), show_alert=True)
        return
    if task_id not in TASKS:
        await callback.answer(texts.TASK_GONE, show_alert=True)
        return
    if await is_task_done(user.id, task_id):
        await callback.answer(texts.TASK_ALREADY_DONE, show_alert=True)
        return
    if await has_pending_submission(user.id, task_id):
        await callback.answer(texts.TASK_ON_REVIEW, show_alert=True)
        return

    await state.set_state(ProofUpload.waiting_for_proof)
    await state.update_data(task_id=task_id)

    # Описание и фото-образец — из базы (их правят админы), запасной
    # вариант — из content/tasks.py
    task = await get_task_by_number(task_id)
    submission_type = TASK_SUBMISSION_TYPES.get(task_id, "photo")
    description = TASK_DESCRIPTIONS.get(task_id, f"#{task_id}")
    if task:
        submission_type = task["submission_type"]
        description = f"<b>#{task_id}. {html.escape(task['name'])}</b>\n\n{task['description']}"

    prompt = texts.TASK_PROMPT.format(
        description=description,
        format_hint=texts.FORMAT_HINTS.get(submission_type, texts.FORMAT_HINT_DEFAULT),
    )
    if task and task["reference_photo_url"]:
        await callback.message.answer_photo(photo=task["reference_photo_url"], caption=prompt)
    else:
        await callback.message.answer(prompt)
    await callback.answer()


@router.message(ProofUpload.waiting_for_proof, Command("cancel"))
async def cancel_proof(message: Message, state: FSMContext) -> None:
    await state.clear()
    done = await get_user_tasks(message.from_user.id)
    await message.answer(texts.PROOF_CANCELLED, reply_markup=bingo_keyboard(done))


# ── Антиспам ──────────────────────────────────────────────────────────────────

async def _spam_gate(message: Message, state: FSMContext, bot: Bot) -> bool:
    """
    True — пруф можно принимать. Иначе диалог сбрасывается, пруф не
    сохраняется, а админы получают ровно одно уведомление о муте.
    """
    allowed, minutes, just_muted = antispam.check_and_register(message.from_user.id)
    if allowed:
        return True

    await state.clear()
    await message.answer(
        (texts.MUTED_NOW if just_muted else texts.STILL_MUTED).format(minutes=minutes))
    if just_muted:
        user = message.from_user
        admin_queue.enqueue(
            bot,
            text=(f"🚫 <b>Антиспам</b>\n\n👤 {_nick(user)} (ID: <code>{user.id}</code>) "
                  f"отправил(а) слишком много пруфов подряд.\n"
                  f"Приём заявок приостановлен на {minutes} мин."),
            reply_markup=admin_unmute_keyboard(user.id),
            digest_line=f"• {_nick(user)} — антиспам, {minutes} мин",
        )
    return False


async def _finish(message: Message, state: FSMContext) -> int | None:
    """Достаёт задание из состояния и закрывает диалог."""
    task_id = (await state.get_data()).get("task_id")
    await state.clear()
    return task_id


async def _after_submit(message: Message) -> None:
    done = await get_user_tasks(message.from_user.id)
    await message.answer(texts.PROOF_SENT, reply_markup=bingo_keyboard(done))


# ── Пруф: фото или альбом ────────────────────────────────────────────────────

@router.message(ProofUpload.waiting_for_proof, F.photo)
async def proof_photo(message: Message, state: FSMContext, bot: Bot,
                      album: list[Message] | None = None) -> None:
    """album — все фото медиагруппы, собранные tg/album.py."""
    if not await _spam_gate(message, state, bot):
        return
    task_id = await _finish(message, state)
    if task_id is None:
        return
    user = message.from_user

    messages = album or [message]
    photos = [m.photo[-1].file_id for m in messages]
    # Подпись — тоже часть пруфа; в альбоме она лежит в одном из сообщений
    caption_text = next((m.caption for m in messages if m.caption), "") or ""

    data = {"photo_file_ids": photos}
    if caption_text:
        data["text"] = caption_text
    submission = await save_task_submission(user.id, task_id, data)
    if submission is None:
        await message.answer(texts.TASK_NOT_FOUND)
        return

    task_name = html.escape(TASKS.get(task_id, f"#{task_id}"))
    caption = (f"📬 <b>Новый пруф ({len(photos)} фото)</b>\n"
               f"👤 {_nick(user)} (ID: <code>{user.id}</code>)\n"
               f"📋 Задание #{task_id}: <b>{task_name}</b>")
    if caption_text:
        caption += f"\n\n💬 <i>{html.escape(caption_text[:700])}</i>"
    keyboard = admin_review_keyboard(submission["id"])

    # Через очередь: у Telegram лимит ~20 сообщений в минуту в одну группу
    if len(photos) == 1:
        admin_queue.enqueue_send(
            lambda: bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=photos[0],
                                   caption=caption, reply_markup=keyboard),
            tag=admin_queue.TAG_PROOF)
    else:
        def send_album():
            media = [InputMediaPhoto(media=fid) for fid in photos]
            media[0].caption = caption
            media[0].parse_mode = "HTML"
            return bot.send_media_group(chat_id=ADMIN_CHAT_ID, media=media)

        # Кнопки нельзя повесить на альбом — отдельным сообщением следом
        admin_queue.enqueue_send(send_album, tag=admin_queue.TAG_PROOF)
        admin_queue.enqueue_send(
            lambda: bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"👆 Пруф выше от {_nick(user)} | Задание #{task_id}: <b>{task_name}</b>",
                reply_markup=keyboard),
            tag=admin_queue.TAG_PROOF)

    await _after_submit(message)


# ── Пруф: текст или ссылка ───────────────────────────────────────────────────

@router.message(ProofUpload.waiting_for_proof, F.text)
async def proof_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await _spam_gate(message, state, bot):
        return
    task_id = await _finish(message, state)
    if task_id is None:
        return
    user = message.from_user
    content = message.text.strip()

    is_link = content.startswith(("http://", "https://", "www."))
    if is_link or TASK_SUBMISSION_TYPES.get(task_id) == "link":
        label, data = "🔗 Ссылка", {"link": content}
    else:
        label, data = "💬 Текст", {"text": content}

    submission = await save_task_submission(user.id, task_id, data)
    if submission is None:
        await message.answer(texts.TASK_NOT_FOUND)
        return

    task_name = html.escape(TASKS.get(task_id, f"#{task_id}"))
    admin_queue.enqueue(
        bot,
        text=(f"📬 <b>Новый пруф ({label.lower()})</b>\n"
              f"👤 {_nick(user)} (ID: <code>{user.id}</code>)\n"
              f"📋 Задание #{task_id}: <b>{task_name}</b>\n\n"
              f"{label}:\n<i>{html.escape(content)}</i>"),
        reply_markup=admin_review_keyboard(submission["id"]),
        coalescable=False,  # у пруфа кнопки решения, в сводку его не схлопнуть
        tag=admin_queue.TAG_PROOF,
    )
    await _after_submit(message)


@router.message(ProofUpload.waiting_for_proof)
async def proof_wrong_type(message: Message, state: FSMContext) -> None:
    task_id = (await state.get_data()).get("task_id")
    submission_type = TASK_SUBMISSION_TYPES.get(task_id, "photo")
    await message.answer(texts.PROOF_WRONG_TYPE.format(
        format_hint=texts.FORMAT_HINTS.get(submission_type, texts.FORMAT_HINT_DEFAULT)))
