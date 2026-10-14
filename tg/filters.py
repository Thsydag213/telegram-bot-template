"""
tg/filters.py — фильтры доступа к админским командам и кнопкам.
"""

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from config import ADMIN_CHAT_ID, ADMIN_IDS


class IsAdmin(BaseFilter):
    """Пропускает только пользователей из списка ADMIN_IDS."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id = event.from_user.id if event.from_user else None
        return user_id in ADMIN_IDS


class FromAdminChat(BaseFilter):
    """
    Пропускает действия, сделанные в админ-чате, либо администраторами
    из ADMIN_IDS в любом чате.

    Нужен для кнопок под карточками (одобрить пруф, подтвердить участие,
    снять мут): жмут их организаторы, которых может не быть в ADMIN_IDS,
    но они находятся в админ-чате. При этом чужой человек, подсунувший
    боту такой callback из личного чата, ничего не добьётся.
    """

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id = event.from_user.id if event.from_user else None
        if user_id in ADMIN_IDS:
            return True

        # У CallbackQuery чат лежит в .message, у Message — в нём самом.
        # Берём по наличию атрибута, а не по типу: так фильтр не привязан
        # к конкретным классам aiogram и его можно проверить тестом.
        carrier = getattr(event, "message", None) or event
        chat = getattr(carrier, "chat", None)
        return bool(ADMIN_CHAT_ID and chat and chat.id == ADMIN_CHAT_ID)
