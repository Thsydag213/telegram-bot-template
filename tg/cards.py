"""
tg/cards.py — работа с карточками в админ-чате.
"""

import logging

from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def mark_card(callback: CallbackQuery, suffix: str) -> None:
    """
    Дописывает результат в карточку и убирает с неё кнопки (сообщение
    редактируется без reply_markup). Берём html_text, а не text: иначе при
    правке терялась бы разметка, а «<» из имени участника ломал бы сообщение.
    """
    original = callback.message.html_text or ""
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=original + suffix)
        else:
            await callback.message.edit_text(text=original + suffix)
    except Exception as exc:
        logger.warning("Не удалось обновить карточку в админ-чате: %s", exc)
