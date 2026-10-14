"""
Общая настройка тестов.

База для тестов:
  • TEST_DATABASE_URL — если задана (так в CI), используется она.
    ВНИМАНИЕ: схема public в ней пересоздаётся перед каждым тестом.
  • иначе — встроенный PostgreSQL из пакета pgserver (pip install pgserver);
  • нет ни того, ни другого — тесты с базой пропускаются.

Переменные окружения выставляются ДО импорта модулей бота: config.py
читает их при импорте.
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_server = None
_db_url = os.getenv("TEST_DATABASE_URL", "")
if not _db_url:
    try:
        import pgserver

        _server = pgserver.get_server(
            Path(tempfile.gettempdir()) / "event-bot-test-pg", cleanup_mode="stop")
        _db_url = _server.get_uri()
    except Exception:  # pgserver не установлен или не запустился
        _db_url = ""

os.environ.update({
    "DATABASE_URL": _db_url or "postgresql://unused@localhost/unused",
    "BOT_TOKEN": "123:test",
    "ADMIN_IDS": "1",
    "ADMIN_CHAT_ID": "-100",
    "EVENT_NAME": "Тестовый ивент",
    "CHANNEL_URL": "https://t.me/test_channel",
    "TIMEZONE_OFFSET": "5",
    "DB_CONNECT_ATTEMPTS": "1",
    # Тесты не должны ходить в Google
    "GOOGLE_SPREADSHEET_ID": "",
    "GOOGLE_CREDENTIALS_JSON": "",
    "GOOGLE_CREDENTIALS_FILE": "",
})

ADMIN_ID = 1
ADMIN_CHAT_ID = -100


def pytest_sessionfinish(session, exitstatus):
    if _server is not None:
        _server.cleanup()


@pytest.fixture
async def db():
    """Чистая база с актуальной схемой. Состояние модулей в памяти тоже сброшено."""
    if not _db_url:
        pytest.skip("нет PostgreSQL: задай TEST_DATABASE_URL или установи pgserver")

    from db.pool import close_pool, get_pool
    from db.schema import init_db
    from services import admin_queue, antispam, screening, sheets

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await init_db()

    admin_queue.drop_pending()
    antispam.reset_all()
    screening.reset_cache()
    sheets.drop_pending()
    yield pool
    await close_pool()


# ── Фейковый Telegram ────────────────────────────────────────────────────────

def fake_user(user_id: int, username: str = "", full_name: str = "Тест"):
    return SimpleNamespace(id=user_id, username=username, full_name=full_name)


def fake_message(user_id: int, text: str = "", username: str = "",
                 full_name: str = "Тест", chat_id: int | None = None):
    message = MagicMock()
    message.from_user = fake_user(user_id, username, full_name)
    message.chat = SimpleNamespace(id=chat_id or user_id, type="private")
    message.text = text
    message.html_text = text
    message.caption = None
    message.photo = None
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    return message


def fake_callback(user_id: int, data: str, full_name: str = "Админ",
                  card_text: str = "карточка", username: str = ""):
    callback = MagicMock()
    callback.from_user = fake_user(user_id, username, full_name)
    callback.data = data
    callback.message = fake_message(user_id, card_text, chat_id=ADMIN_CHAT_ID)
    callback.message.edit_text = AsyncMock()
    callback.message.edit_caption = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def answers(message) -> str:
    """Всё, что бот ответил в этот чат, одной строкой."""
    parts = []
    for call in message.answer.call_args_list + message.answer_photo.call_args_list:
        parts.append(str(call.args[0] if call.args else
                         call.kwargs.get("text") or call.kwargs.get("caption")))
    return " | ".join(parts)


@pytest.fixture
def bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    return bot


@pytest.fixture
def make_state():
    """FSMContext на хранилище в памяти — для логики хендлеров этого достаточно."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()

    def factory(user_id: int, chat_id: int | None = None) -> FSMContext:
        return FSMContext(storage=storage, key=StorageKey(
            bot_id=1, chat_id=chat_id or user_id, user_id=user_id))
    return factory
