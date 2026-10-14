"""
tg/keyboards.py — клавиатуры. Подписи участникам — из content/texts.py,
набор кнопок меню зависит от включённых модулей.
"""

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import CHANNEL_URL, FEATURE_AGENDA, FEATURE_BINGO, TASKS
from content import texts


# ── Участник ─────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup | None:
    """Разделы ивента. None — если ни одного модуля с разделом не включено."""
    builder = InlineKeyboardBuilder()
    sections = 0
    if FEATURE_AGENDA:
        builder.button(text=texts.BTN_AGENDA, callback_data="menu:agenda")
        sections += 1
    if FEATURE_BINGO:
        builder.button(text=texts.BTN_BINGO, callback_data="menu:bingo")
        sections += 1
    if CHANNEL_URL:
        builder.button(text=texts.BTN_CHANNEL, url=CHANNEL_URL)
    if not sections and not CHANNEL_URL:
        return None
    builder.adjust(sections or 1, 1)
    return builder.as_markup()


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BACK_TO_MENU, callback_data="menu:main")
    return builder.as_markup()


def bingo_keyboard(done_tasks: dict[int, bool]) -> InlineKeyboardMarkup:
    """
    Сетка заданий, выполненные отмечены ✅. Ширина ряда считается от числа
    заданий (9 → 3×3, 16 → 4×4), так что сетка подстраивается сама.
    """
    builder = InlineKeyboardBuilder()
    for number, name in TASKS.items():
        label = f"✅ {name}" if done_tasks.get(number) else name
        builder.button(text=label, callback_data=f"task:{number}")
    builder.button(text=texts.BTN_BACK_TO_MENU, callback_data="menu:main")

    side = max(1, round(len(TASKS) ** 0.5))
    rows = [side] * (len(TASKS) // side)
    if len(TASKS) % side:
        rows.append(len(TASKS) % side)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def contact_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_SHARE_CONTACT, request_contact=True)],
            [KeyboardButton(text=texts.BTN_SKIP)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=texts.CONTACT_PLACEHOLDER,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def retry_check_keyboard(with_retry: bool = True) -> InlineKeyboardMarkup | None:
    """
    Для тех, чьё участие не подтверждено. with_retry=False — решение уже
    принято админом вручную, перепроверка ничего не изменит.
    """
    builder = InlineKeyboardBuilder()
    if with_retry:
        builder.button(text=texts.BTN_RETRY_CHECK, callback_data="screen:retry")
    if FEATURE_AGENDA:
        builder.button(text=texts.BTN_AGENDA, callback_data="menu:agenda")
    if CHANNEL_URL:
        builder.button(text=texts.BTN_CHANNEL, url=CHANNEL_URL)
    builder.adjust(1)
    markup = builder.as_markup()
    return markup if markup.inline_keyboard else None


# ── Админ-чат ─────────────────────────────────────────────────────────────────

def admin_review_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    """
    Принять / Отклонить пруф. В кнопке — номер пруфа в базе, поэтому
    решение привязано к конкретной заявке: повторное нажатие или кнопка
    от пруфа, удалённого сбросом, ничего не засчитают.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"approve:{submission_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject:{submission_id}")
    builder.adjust(2)
    return builder.as_markup()


def admin_unmute_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 Снять ограничение", callback_data=f"unmute:{user_id}")
    return builder.as_markup()


def admin_verify_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Ручное решение по участнику, которого не нашли в списке."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить участие", callback_data=f"verify:{user_id}")
    builder.button(text="🚫 Отказать", callback_data=f"unverify:{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def bulk_verify_keyboard(count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Подтвердить всех несверенных ({count})",
                   callback_data="bulkverify:ask")
    return builder.as_markup()


def bulk_verify_confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Да, подтвердить {count}", callback_data="bulkverify:go")
    builder.button(text="↩️ Отмена", callback_data="bulkverify:cancel")
    builder.adjust(1)
    return builder.as_markup()
