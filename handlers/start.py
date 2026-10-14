"""
handlers/start.py — вход в бота, сверка участника и меню.

  /start → сверка со списком участников (если FEATURE_SCREENING включён):
           ник → телефон → «Пропустить» → приветствие и меню
  /menu  → разделы ивента

Подключается всегда.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import (
    ADMIN_CHAT_ID,
    FEATURE_SCREENING,
    SCREENING_STRICT,
    STATUS_CONFIRMED,
    STATUS_MANUAL,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
)
from content import texts
from db.users import ensure_user, get_user, save_screening_result
from services import admin_queue, sheets
from services.screening import (
    apply_result,
    match_delegate,
    notify_admins,
    screening_possible,
    should_notify,
)
from services.timefmt import to_local
from tg.keyboards import (
    contact_request_keyboard,
    main_menu_keyboard,
    remove_keyboard,
    retry_check_keyboard,
)
from tg.states import Screening

logger = logging.getLogger(__name__)
router = Router(name="start")


async def _show_menu(target: Message, welcome: str) -> None:
    """Приветствие, убирающее клавиатуру контакта, и меню разделов."""
    await target.answer(welcome, reply_markup=remove_keyboard(),
                        disable_web_page_preview=True)
    menu = main_menu_keyboard()
    if menu:
        await target.answer(texts.MENU, reply_markup=menu)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    row = await ensure_user(user.id, user.username or "", user.full_name or "")

    if not FEATURE_SCREENING:
        # Сверка выключена: все участники допущены
        if row["status"] != STATUS_CONFIRMED:
            row = await save_screening_result(
                user.id, STATUS_CONFIRMED, "сверка выключена", None)
        _log_visit(user, row)
        await _show_menu(message, texts.WELCOME)
        return

    if row["status"] in (STATUS_CONFIRMED, STATUS_MANUAL):
        _log_visit(user, row)
        await message.answer(texts.WELCOME_CONFIRMED, reply_markup=main_menu_keyboard(),
                             disable_web_page_preview=True)
        return

    await _run_screening(message, user, state, bot)


def _log_visit(user, row) -> None:
    """Отмечает вход в листе «Регистрации»."""
    sheets.queue_registration(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name or "",
        phone=row["phone"] or "",
        status=row["status"],
        comment=row["status_comment"] or "",
        first_seen=to_local(row["first_seen"]),
    )


# ── Сверка ────────────────────────────────────────────────────────────────────

async def _run_screening(target: Message, user, state: FSMContext, bot: Bot,
                         phone: str = "") -> None:
    """
    Сверяет человека со списком. Не хватает данных — просит контакт.

    target — сообщение, в чат которого отвечаем;
    user   — сам участник (в callback это НЕ target.from_user: там бот).
    """
    username = user.username or ""
    full_name = user.full_name or ""

    # Ручное решение админа автоматика не перебивает
    row = await get_user(user.id)
    if row and row["status_locked"]:
        await state.clear()
        if row["status"] == STATUS_MANUAL:
            await _show_menu(target, texts.WELCOME_CONFIRMED)
        else:
            await target.answer(texts.MANUAL_DECISION_FINAL,
                                reply_markup=retry_check_keyboard(with_retry=False))
        return

    # Сверять не с чем: список не загрузился. Вердикт «нет в списке» был бы
    # ложным, поэтому помечаем как несверенного и честно об этом говорим.
    if not await screening_possible():
        logger.error("Список участников пуст — сверка не работает, участник %s (@%s)",
                     user.id, username or "—")
        await state.clear()
        await apply_result(user.id, username, full_name, {
            "status": STATUS_PENDING,
            "comment": "список участников не загружен — сверка не проводилась",
            "row": None,
        })
        _warn_screening_disabled(bot)
        if SCREENING_STRICT:
            await target.answer(texts.SCREENING_UNAVAILABLE,
                                reply_markup=retry_check_keyboard())
        else:
            await _show_menu(target, texts.WELCOME_UNVERIFIED)
        return

    result = await match_delegate(username, phone)

    if result is None:
        await state.set_state(Screening.waiting_for_contact)
        await target.answer(
            texts.ASK_CONTACT if username else texts.ASK_CONTACT_NO_USERNAME,
            reply_markup=contact_request_keyboard(),
        )
        return

    await state.clear()
    notify = await should_notify(user.id, result["status"], result.get("comment", ""))
    await apply_result(user.id, username, full_name, result, phone)

    if result["status"] == STATUS_CONFIRMED:
        await _show_menu(target, texts.WELCOME_CONFIRMED)
        return

    await target.answer(texts.NOT_FOUND, reply_markup=remove_keyboard())
    await target.answer(texts.CHECK_AGAIN_HINT, reply_markup=retry_check_keyboard())
    if notify:
        notify_admins(bot, user.id, username, full_name,
                      STATUS_NOT_FOUND, result.get("comment", ""), phone)


_screening_warned = False


def _warn_screening_disabled(bot: Bot) -> None:
    """Один раз за запуск сообщает админам, что сверка не работает."""
    global _screening_warned
    if _screening_warned or not ADMIN_CHAT_ID:
        return
    _screening_warned = True
    admin_queue.enqueue(
        bot,
        text=(
            "🔴 <b>СВЕРКА НЕ РАБОТАЕТ</b>\n\n"
            "Список участников пуст, сверять не с чем.\n\n"
            "Что проверить:\n"
            "• /delegates — загрузился ли список и какие колонки распознаны\n"
            "• /sync_delegates — перечитать лист со списком\n\n"
            "<i>Это сообщение приходит один раз за запуск бота.</i>"
        ),
        coalescable=False,
    )


@router.message(Screening.waiting_for_contact, F.contact)
async def screening_contact(message: Message, state: FSMContext, bot: Bot) -> None:
    # Telegram позволяет переслать чужой контакт — такой не принимаем
    if message.contact.user_id != message.from_user.id:
        await message.answer(texts.WRONG_CONTACT, reply_markup=contact_request_keyboard())
        return
    await _run_screening(message, message.from_user, state, bot,
                         phone=message.contact.phone_number or "")


@router.message(Screening.waiting_for_contact)
async def screening_skip(message: Message, state: FSMContext, bot: Bot) -> None:
    """«Пропустить» — отказ от сверки; любое другое сообщение — повторная просьба."""
    if (message.text or "").strip().lower() not in texts.SKIP_WORDS:
        await message.answer(texts.ASK_CONTACT_AGAIN, reply_markup=contact_request_keyboard())
        return

    user = message.from_user
    comment = "отказался делиться контактом — сверка не проводилась"
    await state.clear()
    notify = await should_notify(user.id, STATUS_PENDING, comment)
    await apply_result(user.id, user.username or "", user.full_name or "",
                       {"status": STATUS_PENDING, "comment": comment, "row": None})

    await message.answer(texts.PENDING_SKIPPED, reply_markup=remove_keyboard())
    await message.answer(texts.CHECK_AGAIN_HINT, reply_markup=retry_check_keyboard())
    if notify:
        notify_admins(bot, user.id, user.username or "", user.full_name or "",
                      STATUS_PENDING, comment)


@router.callback_query(F.data == "screen:retry")
async def screening_retry(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer(texts.CHECKING)
    if not FEATURE_SCREENING:
        await _show_menu(callback.message, texts.WELCOME)
        return
    await _run_screening(callback.message, callback.from_user, state, bot)


# ── Меню ──────────────────────────────────────────────────────────────────────

@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.MENU, reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text(texts.MENU, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(texts.MENU, reply_markup=main_menu_keyboard())
    await callback.answer()
