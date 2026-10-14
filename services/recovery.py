"""
services/recovery.py — восстановление после сбоев. Работает, только если
подключена Google-таблица.

Два направления, потому что упасть может любая из сторон:

  база → таблица   («догнать» таблицу после перезапуска)
    Очередь записи живёт в памяти. Если процесс упал, незаписанные строки
    пропадут из таблицы, хотя в базе они есть. На старте бот дозаливает
    всех участников.

  таблица → база   (поднять базу, если она потерялась)
    Если база пересоздастся, исчезнут все статусы сверки, и участникам
    пришлось бы проходить регистрацию заново. Поэтому при пустой базе бот
    поднимает участников из листа «Регистрации».

Восстановление из таблицы срабатывает ТОЛЬКО на пустой базе, поэтому
живые данные не перетираются.
"""

import logging

from config import STATUS_PENDING, TASKS
from db.game import all_completions, restore_progress
from db.users import all_users, count_users, restore_users
from services import sheets
from services.timefmt import to_local

logger = logging.getLogger(__name__)

# Больше этого числа участников не заливаем автоматически на старте:
# заметный расход квоты, при необходимости — вручную через /resync_sheets
_AUTO_RESYNC_LIMIT = 1500


async def restore_from_sheets() -> tuple[int, int]:
    """Поднимает участников и прогресс из таблицы. Возвращает (участников, с прогрессом)."""
    registrations = await sheets.read_registrations()
    if not registrations:
        return 0, 0
    users = await restore_users(registrations)
    progress = await restore_progress(await sheets.read_progress() or [])
    return users, progress


async def resync_to_sheets(registrations: bool = True, progress: bool = True) -> int:
    """Ставит участников из базы в очередь на запись в таблицу."""
    users = await all_users()
    completions = await all_completions() if progress else {}
    for row in users:
        username = row["username"] or ""
        status = row["status"] or STATUS_PENDING
        if registrations:
            sheets.queue_registration(
                user_id=row["user_id"],
                username=username,
                full_name=row["full_name"] or "",
                phone=row["phone"] or "",
                status=status,
                comment=row["status_comment"] or "",
                first_seen=to_local(row["first_seen"]),
                # Дозалив — не визит участника: время входа берём из базы
                last_seen=to_local(row["last_seen"]),
            )
        if progress:
            done = completions.get(row["user_id"], {})
            sheets.queue_progress(
                user_id=row["user_id"],
                username=username,
                status=status,
                done={n: n in done for n in TASKS},
                total=len(done),
                ts_win=row["ts_win"],
            )
    return len(users)


async def run_on_startup() -> None:
    """Вызывается при запуске. Сам решает, что чинить. Никогда не падает."""
    if not sheets.is_enabled():
        return
    try:
        users_in_db = await count_users()
        if users_in_db == 0:
            restored, progress = await restore_from_sheets()
            if restored:
                logger.warning(
                    "База была пуста — восстановлено из таблицы: %s участников, "
                    "прогресс у %s. Повторная регистрация не нужна.", restored, progress)
            else:
                logger.info("База пуста и в таблице никого — старт с чистого листа")
            return

        if users_in_db > _AUTO_RESYNC_LIMIT:
            logger.info("Участников в базе %s — автодозалив в таблицу пропущен, "
                        "при необходимости запусти /resync_sheets", users_in_db)
            return

        queued = await resync_to_sheets()
        written = await sheets.flush()
        logger.info("Таблица синхронизирована при старте: %s участников, записано %s строк",
                    queued, written)
    except Exception as exc:
        logger.error("Восстановление при старте не выполнено: %s", exc)
