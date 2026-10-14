"""
handlers/fallback.py — ответ на всё, что не подошло ни под один сценарий:
неизвестные команды, случайные фото, пересланные сообщения. Без него бот
молчал, и люди шли с вопросами к организаторам.

Подключается последним. Работает только в личке: в админ-чате бот не
должен вклиниваться в переписку организаторов.
"""

from aiogram import F, Router
from aiogram.types import Message

from content import texts
from tg.keyboards import main_menu_keyboard

router = Router(name="fallback")


@router.message(F.chat.type == "private")
async def fallback(message: Message) -> None:
    await message.answer(texts.FALLBACK, reply_markup=main_menu_keyboard())
