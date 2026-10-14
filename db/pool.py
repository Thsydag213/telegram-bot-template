"""
db/pool.py — пул соединений с PostgreSQL.

Подключение идёт с повторами: в облаках (Railway, Render, Docker Compose)
сеть контейнера или сама база часто поднимается на несколько секунд позже
бота. Без повторов процесс падает на первой же попытке, оркестратор его
перезапускает, и так по кругу.
"""

import asyncio
import logging
import socket
from urllib.parse import urlsplit

import asyncpg

from config import (
    DATABASE_FALLBACK_URL,
    DATABASE_URL,
    DB_CONNECT_ATTEMPTS,
    DB_CONNECT_DELAY,
)

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def describe_dsn(dsn: str = "") -> str:
    """Хост, порт и имя базы — без логина и пароля, чтобы писать в логи."""
    dsn = dsn or DATABASE_URL
    if not dsn:
        return "(DATABASE_URL не задан)"
    try:
        parts = urlsplit(dsn)
        name = (parts.path or "/").lstrip("/") or "?"
        return f"{parts.hostname or '?'}:{parts.port or 5432}/{name}"
    except Exception:
        return "(не удалось разобрать строку подключения)"


def _candidates() -> list[tuple[str, str]]:
    """Адреса базы в порядке предпочтения: основной, затем запасной."""
    result = []
    if DATABASE_URL:
        result.append(("DATABASE_URL", DATABASE_URL))
    if DATABASE_FALLBACK_URL and DATABASE_FALLBACK_URL != DATABASE_URL:
        result.append(("DATABASE_FALLBACK_URL", DATABASE_FALLBACK_URL))
    return result


async def _create_pool() -> asyncpg.Pool:
    candidates = _candidates()
    if not candidates:
        raise RuntimeError("DATABASE_URL не задан — подключаться не к чему.")

    last_error: Exception | None = None
    delay = DB_CONNECT_DELAY
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        # На каждой попытке пробуем все адреса: основной может подняться
        # позже, а запасной работать сразу
        for label, dsn in candidates:
            try:
                pool = await asyncpg.create_pool(dsn)
                if attempt > 1 or label != candidates[0][0]:
                    logger.info("PostgreSQL: подключились через %s (%s), попытка %s",
                                label, describe_dsn(dsn), attempt)
                return pool
            except (socket.gaierror, OSError) as exc:
                last_error = exc
                logger.warning("PostgreSQL недоступен через %s (%s), попытка %s из %s: %s",
                               label, describe_dsn(dsn), attempt, DB_CONNECT_ATTEMPTS, exc)
        if attempt < DB_CONNECT_ATTEMPTS:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)

    tried = ", ".join(f"{label} → {describe_dsn(dsn)}" for label, dsn in candidates)
    logger.error(
        "Не удалось подключиться к PostgreSQL за %s попыток. Пробовали: %s\n"
        "Что проверить:\n"
        "  1. База запущена и доступна из того места, где работает бот;\n"
        "  2. Адрес, порт, логин и пароль в DATABASE_URL верные;\n"
        "  3. Если у хостинга есть внутренний и публичный адрес базы, задай\n"
        "     публичный в DATABASE_FALLBACK_URL — бот переключится сам.\n"
        "Проверить адрес по шагам: python tools/check_db.py \"<DATABASE_URL>\"",
        DB_CONNECT_ATTEMPTS, tried,
    )
    raise last_error or RuntimeError("PostgreSQL недоступен")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await _create_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
