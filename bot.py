"""
bot.py — точка входа. Запуск: python bot.py

Порядок старта:
  проверка конфига → PostgreSQL (схема создаётся и мигрирует сама) →
  Google-таблица и список участников → антиспам-ограничения →
  восстановление после сбоя → роутеры включённых модулей →
  фоновые задачи → polling.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from config import (
    ADMIN_CHAT_ID,
    ADMIN_IDS,
    ALBUM_LATENCY,
    BOT_TOKEN,
    DATABASE_URL,
    DELEGATES_REFRESH_MINUTES,
    FEATURE_AGENDA,
    FEATURE_BINGO,
    FEATURE_SCREENING,
)
from db.delegates import count_delegates
from db.fsm_storage import PostgresStorage
from db.mutes import clear_mute, load_active_mutes, save_mute
from db.pool import close_pool, describe_dsn
from db.schema import init_db
from handlers import admin, agenda, bingo, fallback, review, screening_admin, start, tasks_admin
from services import admin_queue, antispam, recovery, sheets
from services.screening import refresh_delegates
from tg.album import AlbumMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _check_config() -> None:
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN не задан — получи токен у @BotFather")
    if not DATABASE_URL:
        problems.append("DATABASE_URL не задан — нужна база PostgreSQL")
    if FEATURE_BINGO and not ADMIN_CHAT_ID:
        problems.append("ADMIN_CHAT_ID не задан — пруфам бинго некуда приходить")
    if problems:
        raise SystemExit("Бот не запущен:\n  • " + "\n  • ".join(problems)
                         + "\nСм. .env.example")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не заданы — админские команды недоступны никому")
    logger.info("Модули: сверка=%s, программа=%s, бинго=%s",
                FEATURE_SCREENING, FEATURE_AGENDA, FEATURE_BINGO)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=PostgresStorage())
    dp.message.middleware(AlbumMiddleware(latency=ALBUM_LATENCY))

    # Порядок важен: админские роутеры раньше пользовательских, чтобы
    # их команды и диалоги не перехватывались; fallback — последним
    dp.include_router(admin.router)
    if FEATURE_SCREENING:
        dp.include_router(screening_admin.router)
    if FEATURE_BINGO:
        dp.include_router(review.router)
        dp.include_router(tasks_admin.router)
    if FEATURE_AGENDA:
        dp.include_router(agenda.router)
    dp.include_router(start.router)
    if FEATURE_BINGO:
        dp.include_router(bingo.router)
    dp.include_router(fallback.router)
    return dp


async def _set_commands(bot: Bot) -> None:
    """Меню команд в интерфейсе Telegram: участникам своё, админам ещё /admin."""
    user_commands = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="menu", description="🏠 Меню ивента"),
    ]
    if FEATURE_AGENDA:
        user_commands.append(BotCommand(command="agenda", description="📅 Программа"))
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        logger.warning("Не удалось установить меню команд: %s", exc)
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                user_commands + [BotCommand(command="admin", description="🛠 Команды админа")],
                scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            # Обычно значит, что админ ещё ни разу не писал боту
            logger.warning("Меню админа для %s не установлено: %s", admin_id, exc)


async def _delegates_refresh_loop() -> None:
    """Фоново перечитывает список участников из таблицы."""
    while True:
        await asyncio.sleep(max(1, DELEGATES_REFRESH_MINUTES) * 60)
        try:
            await refresh_delegates()
        except Exception as exc:
            logger.error("Фоновое обновление списка участников упало: %s", exc)


async def main() -> None:
    _check_config()
    logger.info("PostgreSQL: подключаюсь к %s", describe_dsn())
    await init_db()

    await sheets.init()
    if FEATURE_SCREENING and await refresh_delegates() is None:
        logger.warning(
            "Список участников не загружен из таблицы, в кэше базы: %s записей. "
            "Если 0 — сверка не работает, см. /delegates.", await count_delegates())

    antispam.bind_storage(save_mute, clear_mute)
    restored = await antispam.load_from_db(load_active_mutes)
    if restored:
        logger.info("Антиспам: восстановлено %s ограничений", restored)

    await recovery.run_on_startup()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    await _set_commands(bot)

    # Хендлеры не ходят ни в Google, ни в админ-чат напрямую: кладут задания
    # в очереди, а эти задачи разгребают их в своём темпе
    background = [
        asyncio.create_task(sheets.flush_worker()),
        asyncio.create_task(admin_queue.worker(bot)),
    ]
    if FEATURE_SCREENING:
        background.append(asyncio.create_task(_delegates_refresh_loop()))

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        try:
            written = await sheets.flush()
            if written:
                logger.info("Google Sheets: дозаписано %s строк перед остановкой", written)
        except Exception as exc:
            logger.error("Не удалось дозаписать очередь: %s", exc)
        for task in background:
            task.cancel()
        await bot.session.close()
        await close_pool()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
