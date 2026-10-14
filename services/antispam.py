"""
services/antispam.py — ограничение частоты отправки пруфов.

Задача: не дать одному участнику завалить админ-чат заявками.
Работает как скользящее окно: если за SPAM_WINDOW_SECONDS пришло больше
SPAM_MAX_SUBMISSIONS пруфов, приём заявок от этого участника ставится
на паузу на SPAM_MUTE_SECONDS. Пруфы в это время в админ-чат не уходят
и в базу не сохраняются.

Скользящее окно попыток держится в памяти: оно короткое, и лишний поход
в БД на каждое сообщение того не стоит. А вот сами МУТЫ пишутся в
PostgreSQL и восстанавливаются при старте — иначе передеплой посреди
ивента снимал бы наказание со всех спамеров разом.
"""

import asyncio
import logging
import time
from math import ceil

from config import (
    SPAM_MAX_SUBMISSIONS,
    SPAM_MUTE_SECONDS,
    SPAM_WINDOW_SECONDS,
)

logger = logging.getLogger(__name__)

# user_id → список моментов отправки (monotonic-секунды) внутри окна
_history: dict[int, list[float]] = {}
# user_id → момент, до которого приём заявок приостановлен
_muted_until: dict[int, float] = {}

# Функции сохранения в БД. Подставляются из bot.py, чтобы antispam
# не зависел от database напрямую и оставался тестируемым.
_save_mute = None
_clear_mute = None


def bind_storage(save_mute, clear_mute) -> None:
    """Подключает постоянное хранилище мутов."""
    global _save_mute, _clear_mute
    _save_mute, _clear_mute = save_mute, clear_mute


def _persist(coro_factory) -> None:
    """Пишет в БД в фоне: хэндлер не должен ждать запись."""
    if coro_factory is None:
        return
    try:
        asyncio.get_running_loop().create_task(_safe(coro_factory))
    except RuntimeError:
        pass


async def _safe(coro_factory) -> None:
    try:
        await coro_factory()
    except Exception as exc:
        logger.error("Антиспам: не удалось сохранить мут — %s", exc)


async def load_from_db(loader) -> int:
    """
    Восстанавливает активные муты после запуска.
    loader — корутина, возвращающая [(user_id, секунд_осталось)].
    """
    try:
        rows = await loader()
    except Exception as exc:
        logger.error("Антиспам: не удалось загрузить муты — %s", exc)
        return 0
    now = time.monotonic()
    for user_id, left in rows:
        _muted_until[user_id] = now + left
    return len(rows)


def _minutes_left(seconds: float) -> int:
    """Округляем вверх, чтобы не писать участнику «через 0 мин»."""
    return max(1, ceil(seconds / 60))


def check_and_register(user_id: int) -> tuple[bool, int, bool]:
    """
    Регистрирует попытку отправки пруфа.

    Возвращает (разрешено, минут_до_разблокировки, только_что_замучен):
      (True,  0, False) — можно принимать пруф;
      (False, N, True)  — этой попыткой участник превысил лимит,
                          админам стоит отправить одно уведомление;
      (False, N, False) — участник уже был в муте.
    """
    now = time.monotonic()

    until = _muted_until.get(user_id)
    if until is not None:
        if now < until:
            return False, _minutes_left(until - now), False
        # Мут истёк — начинаем историю с чистого листа
        del _muted_until[user_id]
        _history.pop(user_id, None)

    window_start = now - SPAM_WINDOW_SECONDS
    attempts = [ts for ts in _history.get(user_id, []) if ts > window_start]
    attempts.append(now)
    _history[user_id] = attempts

    if len(attempts) > SPAM_MAX_SUBMISSIONS:
        _muted_until[user_id] = now + SPAM_MUTE_SECONDS
        _history.pop(user_id, None)
        _persist(lambda: _save_mute(
            user_id, SPAM_MUTE_SECONDS,
            f"{len(attempts)} заявок за {SPAM_WINDOW_SECONDS} сек",
        ) if _save_mute else None)
        logger.warning(
            "Антиспам: участник %s замучен на %s сек (%s заявок за %s сек)",
            user_id, SPAM_MUTE_SECONDS, len(attempts), SPAM_WINDOW_SECONDS,
        )
        return False, _minutes_left(SPAM_MUTE_SECONDS), True

    return True, 0, False


def is_muted(user_id: int) -> tuple[bool, int]:
    """Проверка без регистрации попытки — для кнопок и меню."""
    until = _muted_until.get(user_id)
    if until is None:
        return False, 0
    left = until - time.monotonic()
    if left <= 0:
        del _muted_until[user_id]
        _history.pop(user_id, None)
        return False, 0
    return True, _minutes_left(left)


def unmute(user_id: int) -> bool:
    """Снимает мут вручную. True если участник действительно был в муте."""
    was_muted = _muted_until.pop(user_id, None) is not None
    _history.pop(user_id, None)
    if was_muted:
        logger.info("Антиспам: с участника %s снят мут", user_id)
    _persist(lambda: _clear_mute(user_id) if _clear_mute else None)
    return was_muted


def muted_users() -> list[tuple[int, int]]:
    """Список [(user_id, минут до разблокировки)] для админской команды."""
    now = time.monotonic()
    result = []
    for user_id, until in list(_muted_until.items()):
        left = until - now
        if left <= 0:
            del _muted_until[user_id]
            _history.pop(user_id, None)
            continue
        result.append((user_id, _minutes_left(left)))
    return sorted(result, key=lambda x: -x[1])


def reset_all() -> int:
    """Снимает все муты и чистит историю. Возвращает, сколько мутов снято."""
    count = len(_muted_until)
    _muted_until.clear()
    _history.clear()
    if count:
        logger.warning("Антиспам: сброшены все муты (%s)", count)
    return count
