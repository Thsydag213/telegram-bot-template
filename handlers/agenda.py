"""
handlers/agenda.py — программа ивента (модуль FEATURE_AGENDA).

Участник: /agenda или кнопка в меню.
Админ:    /set_agenda — прислать текст или фото с подписью,
          /clear_agenda — вернуть текст-заглушку.
Программа хранится в базе и переживает перезапуск.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import AGENDA_PHOTO_KEY, AGENDA_TEXT_KEY
from content import texts
from db.settings import delete_setting, get_setting, set_setting
from tg.filters import IsAdmin
from tg.keyboards import back_to_menu_keyboard
from tg.states import AgendaEdit

router = Router(name="agenda")

# Подпись к фото в Telegram ограничена 1024 символами
_CAPTION_LIMIT = 1024


async def _send_agenda(message: Message) -> None:
    text = await get_setting(AGENDA_TEXT_KEY, texts.AGENDA_DEFAULT)
    photo = await get_setting(AGENDA_PHOTO_KEY, "")
    if photo and len(text) <= _CAPTION_LIMIT:
        await message.answer_photo(photo=photo, caption=text,
                                   reply_markup=back_to_menu_keyboard())
        return
    if photo:
        await message.answer_photo(photo=photo)
    await message.answer(text, reply_markup=back_to_menu_keyboard(),
                         disable_web_page_preview=True)


@router.message(Command("agenda"))
async def cmd_agenda(message: Message) -> None:
    await _send_agenda(message)


@router.callback_query(F.data == "menu:agenda")
async def menu_agenda(callback: CallbackQuery) -> None:
    await _send_agenda(callback.message)
    await callback.answer()


# ── Админ ─────────────────────────────────────────────────────────────────────

@router.message(Command("set_agenda"), IsAdmin())
async def cmd_set_agenda(message: Message, state: FSMContext) -> None:
    await state.set_state(AgendaEdit.waiting_for_content)
    await message.answer(
        "📅 Пришли новую программу:\n\n"
        "• <b>текстом</b> — можно с разметкой (&lt;b&gt;, &lt;i&gt;, &lt;a&gt;)\n"
        "• <b>фото с подписью</b> — картинка расписания + текст\n\n"
        "Для отмены: /cancel",
    )


@router.message(AgendaEdit.waiting_for_content, Command("cancel"))
async def set_agenda_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("↩️ Отменено. Программа не изменена.")


@router.message(AgendaEdit.waiting_for_content, F.photo)
async def set_agenda_photo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await set_setting(AGENDA_PHOTO_KEY, message.photo[-1].file_id)
    if message.caption:
        await set_setting(AGENDA_TEXT_KEY, message.html_text or message.caption)
    await message.answer("✅ Программа обновлена (фото + текст). Проверь: /agenda")


@router.message(AgendaEdit.waiting_for_content, F.text)
async def set_agenda_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await set_setting(AGENDA_TEXT_KEY, message.html_text or message.text)
    # Новая программа текстом заменяет и старую картинку
    await delete_setting(AGENDA_PHOTO_KEY)
    await message.answer("✅ Программа обновлена. Проверь: /agenda")


@router.message(AgendaEdit.waiting_for_content)
async def set_agenda_wrong(message: Message) -> None:
    await message.answer("⚠️ Пришли текст или фото с подписью. Для отмены: /cancel")


@router.message(Command("clear_agenda"), IsAdmin())
async def cmd_clear_agenda(message: Message) -> None:
    await delete_setting(AGENDA_TEXT_KEY)
    await delete_setting(AGENDA_PHOTO_KEY)
    await message.answer("🗑 Программа сброшена до текста-заглушки.")
