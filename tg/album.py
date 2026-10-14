"""
tg/album.py — сборка альбома из нескольких фото в один пруф.

Когда пользователь отправляет несколько фото одновременно, Telegram
присылает их как отдельные Message с одинаковым media_group_id.

Этот middleware:
  1. Получает первое фото — стартует таймер (ALBUM_LATENCY секунд)
  2. Каждое следующее фото добавляет в буфер и сбрасывает таймер
  3. Когда таймер срабатывает — передаёт хэндлеру ПЕРВОЕ сообщение,
     а полный список фото кладёт в data["album"]

Для одиночных сообщений (media_group_id == None) поведение не меняется:
  data["album"] == None, хэндлер работает как раньше.
"""

import asyncio
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class AlbumMiddleware(BaseMiddleware):
    """Собирает медиа-группы и передаёт их хэндлеру одним вызовом."""

    def __init__(self, latency: float = 0.7) -> None:
        """
        :param latency: сколько секунд ждать после последнего фото перед
                        передачей группы в хэндлер (0.5-1.0 обычно хватает)
        """
        self.latency = latency
        # media_group_id → список сообщений
        self._albums: dict[str, list[Message]] = {}
        # media_group_id → asyncio.Task (таймер)
        self._timers: dict[str, asyncio.Task] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        # Одиночное сообщение — пропускаем без изменений
        if not event.media_group_id:
            data["album"] = None
            return await handler(event, data)

        group_id = event.media_group_id

        # Добавляем сообщение в буфер
        if group_id not in self._albums:
            self._albums[group_id] = []
        self._albums[group_id].append(event)

        # Отменяем предыдущий таймер (если был) и запускаем новый
        if group_id in self._timers:
            self._timers[group_id].cancel()

        self._timers[group_id] = asyncio.create_task(
            self._flush(group_id, handler, data)
        )

        # Возвращаем None — хэндлер будет вызван из _flush
        return None

    async def _flush(
        self,
        group_id: str,
        handler: Callable,
        data: dict[str, Any],
    ) -> None:
        """Ждёт latency секунд, затем вызывает хэндлер с собранным альбомом."""
        await asyncio.sleep(self.latency)

        messages = self._albums.pop(group_id, [])
        self._timers.pop(group_id, None)

        if not messages:
            return

        # Передаём первое сообщение как event, все — через data["album"]
        data["album"] = messages
        await handler(messages[0], data)
