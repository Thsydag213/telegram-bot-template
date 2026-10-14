"""
handlers/review.py — проверка пруфов и антиспам (модуль FEATURE_BINGO).

Кнопки под пруфами в админ-чате: ✅ Принять / ❌ Отклонить. Нажимать их
может любой участник админ-чата. Решение применяется атомарно и только
к непроверенному пруфу: двойное нажатие, два админа одновременно или
кнопка от пруфа, удалённого сбросом игры, ничего не засчитают.

Команды: /muted, /unmute.
"""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import TASKS, WIN_THRESHOLD
from content import texts
from db.game import complete_task, get_user_tasks, review_submission, try_mark_winner
from db.users import find_user, get_user
from services import admin_queue, antispam, sheets
from tg.cards import mark_card
from tg.filters import FromAdminChat, IsAdmin
from tg.keyboards import bingo_keyboard

logger = logging.getLogger(__name__)
router = Router(name="review")

_ALREADY = {
    "approved": "уже одобрен",
    "rejected": "уже отклонён",
    "legacy": "из старой версии бота — попроси участника отправить его заново",
}


def _display(user) -> str:
    """@ник или ID участника для админ-чата."""
    if user and user["username"]:
        return f"@{user['username']}"
    return f"ID {user['user_id']}" if user else "участник"


async def _review(callback: CallbackQuery, status: str) -> dict | None:
    parts = callback.data.split(":")
    # Карточки старой версии несли approve:<user_id>:<task_id> — номера
    # пруфа в них нет, решение по ним принять нельзя
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Карточка из старой версии бота. Попроси участника "
                              "отправить пруф заново.", show_alert=True)
        await mark_card(callback, "\n\n⚪️ <i>Карточка устарела</i>")
        return None

    result = await review_submission(int(parts[1]), status, callback.from_user.full_name)
    if result["ok"]:
        return result
    reason = ("удалён сбросом игры" if result["status"] is None
              else _ALREADY.get(result["status"], result["status"]))
    await callback.answer(f"Этот пруф {reason}.", show_alert=True)
    await mark_card(callback, f"\n\n⚪️ <i>Пруф {reason}</i>")
    return None


@router.callback_query(F.data.startswith("approve:"), FromAdminChat())
async def approve(callback: CallbackQuery, bot: Bot) -> None:
    result = await _review(callback, "approved")
    if not result:
        return
    user_id, task_id = result["user_id"], result["task_number"]
    task = TASKS.get(task_id, f"#{task_id}")

    total = await complete_task(user_id, task_id)
    done = await get_user_tasks(user_id)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=texts.PROOF_APPROVED.format(task=f"#{task_id}: {html.escape(task)}",
                                             done=total, total=WIN_THRESHOLD),
            reply_markup=bingo_keyboard(done),
        )
    except Exception as exc:
        logger.warning("Не удалось уведомить %s об одобрении: %s", user_id, exc)

    admin = html.escape(callback.from_user.full_name)
    await mark_card(callback, f"\n\n✅ <b>Одобрено</b> — {admin}")
    await callback.answer(f"✅ «{task}» засчитано")

    if total >= WIN_THRESHOLD and await try_mark_winner(user_id):
        await _congratulate(bot, user_id)

    # Лист «Прогресс» пишем последним: время победы уже проставлено
    user = await get_user(user_id)
    sheets.queue_progress(
        user_id=user_id,
        username=(user["username"] if user else "") or "",
        status=(user["status"] if user else "") or "",
        done=done, total=total,
        ts_win=user["ts_win"] if user else None,
    )


async def _congratulate(bot: Bot, user_id: int) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=texts.WINNER.format(total=WIN_THRESHOLD))
    except Exception as exc:
        logger.warning("Не удалось поздравить %s: %s", user_id, exc)
    user = await get_user(user_id)
    admin_queue.enqueue(
        bot,
        text=(f"🏆 <b>ПОБЕДИТЕЛЬ!</b>\n\n{_display(user)} (ID: <code>{user_id}</code>) "
              f"выполнил(а) {WIN_THRESHOLD} заданий."),
        coalescable=False,
    )


@router.callback_query(F.data.startswith("reject:"), FromAdminChat())
async def reject(callback: CallbackQuery, bot: Bot) -> None:
    result = await _review(callback, "rejected")
    if not result:
        return
    user_id, task_id = result["user_id"], result["task_number"]
    task = TASKS.get(task_id, f"#{task_id}")
    try:
        await bot.send_message(
            chat_id=user_id,
            text=texts.PROOF_REJECTED.format(task=f"#{task_id}: {html.escape(task)}"))
    except Exception as exc:
        logger.warning("Не удалось уведомить %s об отклонении: %s", user_id, exc)
    admin = html.escape(callback.from_user.full_name)
    await mark_card(callback, f"\n\n❌ <b>Отклонено</b> — {admin}")
    await callback.answer("❌ Пруф отклонён")


# ── Антиспам ──────────────────────────────────────────────────────────────────

async def _notify_unmuted(bot: Bot, user_id: int) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=texts.UNMUTED)
    except Exception as exc:
        logger.warning("Не удалось уведомить %s о снятии ограничения: %s", user_id, exc)


@router.callback_query(F.data.startswith("unmute:"), FromAdminChat())
async def unmute_button(callback: CallbackQuery, bot: Bot) -> None:
    user_id = int(callback.data.split(":")[1])
    if not antispam.unmute(user_id):
        await callback.answer("Ограничение уже снято или истекло")
        return
    await _notify_unmuted(bot, user_id)
    admin = html.escape(callback.from_user.full_name)
    await mark_card(callback, f"\n\n🔓 <b>Ограничение снято</b> — {admin}")
    await callback.answer("🔓 Ограничение снято")


@router.message(Command("muted"), IsAdmin())
async def cmd_muted(message: Message) -> None:
    muted = antispam.muted_users()
    if not muted:
        await message.answer("✅ Сейчас никто не ограничен антиспамом.")
        return
    lines = [f"🚫 <b>Ограничены антиспамом ({len(muted)}):</b>\n"]
    for user_id, minutes in muted:
        lines.append(f"• {_display(await get_user(user_id))} — ещё {minutes} мин")
    lines.append("\nСнять: <code>/unmute ID_или_@ник</code>")
    await message.answer("\n".join(lines))


@router.message(Command("unmute"), IsAdmin())
async def cmd_unmute(message: Message, bot: Bot) -> None:
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Использование: <code>/unmute @ник</code> или "
                             "<code>/unmute 123456789</code>")
        return
    user = await find_user(query)
    if not user:
        await message.answer("❌ Такой участник ещё не запускал бота.")
        return
    if not antispam.unmute(user["user_id"]):
        await message.answer("Этот участник и так не ограничен.")
        return
    await _notify_unmuted(bot, user["user_id"])
    await message.answer("🔓 Ограничение снято.")
