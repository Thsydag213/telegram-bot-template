"""
handlers/admin.py — общие команды админов. Подключается всегда.

  /admin                — список доступных команд (с учётом модулей)
  /export, /exportcsv   — выгрузка участников и результатов
  /whois @ник           — карточка участника
  /queue                — что ждёт отправки в таблицу и админ-чат
  /resync_sheets        — перезалить таблицу из базы
  /restore_from_sheets  — поднять базу из таблицы
  /reset_sheet_cache    — после ручной правки таблицы
  /reset_game, /reset_registrations, /reset_all — сбросы с подтверждением
"""

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from config import (
    FEATURE_AGENDA,
    FEATURE_BINGO,
    FEATURE_SCREENING,
    SHEET_PROGRESS,
    SHEET_REGISTRATIONS,
    STATUS_LABELS,
    STATUS_PENDING,
    WIN_THRESHOLD,
)
from db.exports import export_csv, export_xlsx
from db.game import get_user_tasks
from db.resets import (
    count_everything,
    count_game,
    count_registrations,
    reset_everything,
    reset_game,
    reset_registrations,
)
from db.users import find_user
from services import admin_queue, antispam, recovery, screening, sheets
from tg.filters import IsAdmin
from tg.states import ResetConfirm

router = Router(name="admin")


# ── /admin ────────────────────────────────────────────────────────────────────

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message) -> None:
    lines = ["🛠 <b>Команды админа</b>", "",
             "<b>Участники</b>",
             "/whois @ник — карточка участника",
             "/export, /exportcsv — выгрузка в Excel / CSV"]
    if FEATURE_SCREENING:
        lines += ["", "<b>Сверка</b>",
                  "/pending — кто ждёт решения",
                  "/verify @ник — подтвердить вручную",
                  "/check @ник — что бот знает о человеке",
                  "/delegates — диагностика списка участников",
                  "/sync_delegates — перечитать список"]
    if FEATURE_BINGO:
        lines += ["", "<b>Бинго</b>",
                  "/list_tasks — задания",
                  "/upload_task — фото-образец и описание",
                  "/edit_desc — описание задания",
                  "/reseed_tasks — подтянуть задания из кода",
                  "/muted, /unmute @ник — антиспам"]
    if FEATURE_AGENDA:
        lines += ["", "<b>Программа</b>",
                  "/set_agenda — загрузить программу",
                  "/clear_agenda — вернуть заглушку"]
    if sheets.is_enabled():
        lines += ["", "<b>Google-таблица</b>",
                  "/queue — что ждёт отправки",
                  "/resync_sheets — перезалить таблицу из базы",
                  "/restore_from_sheets — поднять базу из таблицы",
                  "/reset_sheet_cache — после ручной правки таблицы"]
    lines += ["", "<b>Сбросы</b> (с подтверждением)"]
    if FEATURE_BINGO:
        lines.append("/reset_game — пруфы и прогресс")
    if FEATURE_SCREENING:
        lines.append("/reset_registrations — результаты сверки")
    lines.append("/reset_all — все данные участников")
    await message.answer("\n".join(lines))


# ── Выгрузки и карточка участника ────────────────────────────────────────────

@router.message(Command("export"), IsAdmin())
async def cmd_export(message: Message) -> None:
    await message.answer("⏳ Формирую Excel...")
    await message.answer_document(
        BufferedInputFile(await export_xlsx(), filename="participants.xlsx"),
        caption="📊 Участники" + (" и результаты бинго" if FEATURE_BINGO else ""))


@router.message(Command("exportcsv"), IsAdmin())
async def cmd_export_csv(message: Message) -> None:
    await message.answer_document(
        BufferedInputFile(await export_csv(), filename="participants.csv"),
        caption="📊 Участники (CSV)")


@router.message(Command("whois"), IsAdmin())
async def cmd_whois(message: Message) -> None:
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Использование: <code>/whois @ник</code> или "
                             "<code>/whois 123456789</code>")
        return
    user = await find_user(query)
    if not user:
        await message.answer("❌ Такой участник ещё не запускал бота.")
        return
    status = user["status"] or STATUS_PENDING
    lines = [f"👤 <b>{html.escape(user['full_name'] or '—')}</b>",
             f"Ник: {('@' + user['username']) if user['username'] else '—'}",
             f"ID: <code>{user['user_id']}</code>",
             f"📞 {html.escape(user['phone'] or '—')}"]
    if FEATURE_SCREENING:
        lines += [f"📋 Статус: <b>{STATUS_LABELS.get(status, status)}</b>"
                  + (" 🔒 решение админа" if user["status_locked"] else ""),
                  f"💬 {html.escape(user['status_comment'] or '—')}"]
    if FEATURE_BINGO:
        done = await get_user_tasks(user["user_id"])
        lines.append(f"🎯 Заданий: {sum(done.values())}/{WIN_THRESHOLD}"
                     + (" 🏆" if user["notified_win"] else ""))
    await message.answer("\n".join(lines))


# ── Google-таблица ────────────────────────────────────────────────────────────

@router.message(Command("queue"), IsAdmin())
async def cmd_queue(message: Message) -> None:
    sheets_pending = sheets.pending_count()
    admin_pending = admin_queue.pending_count()
    dropped = admin_queue.dropped_count()
    lines = ([f"📥 <b>В очереди</b>\n\n"
              f"• Google-таблица: <b>{sheets_pending}</b> строк\n"
              f"• Админ-чат: <b>{admin_pending}</b> сообщений\n\n"
              f"<i>Между сбросами это нормально. Если число растёт и не падает — "
              f"смотри логи. Данные при этом в безопасности: они в базе, таблицу "
              f"можно пересобрать командой /resync_sheets.</i>"]
             if sheets_pending or admin_pending
             else ["✅ Очереди пусты — всё записано и отправлено."])
    if dropped:
        lines.append(f"\n⚠️ Не доставлено в админ-чат за время работы: <b>{dropped}</b> "
                     f"(подробности в логах)")
    await message.answer("\n".join(lines))


@router.message(Command("resync_sheets"), IsAdmin())
async def cmd_resync(message: Message) -> None:
    if not sheets.is_enabled():
        await message.answer("❌ Google-таблица не подключена.")
        return
    await message.answer("⏳ Ставлю всех участников в очередь на запись...")
    queued = await recovery.resync_to_sheets()
    written = await sheets.flush()
    await message.answer(f"✅ Готово.\n• Участников: <b>{queued}</b>\n"
                         f"• Строк записано: <b>{written}</b>\n"
                         f"• Осталось в очереди: {sheets.pending_count()}")


@router.message(Command("restore_from_sheets"), IsAdmin())
async def cmd_restore(message: Message) -> None:
    """Поднимает участников из таблицы, если база потерялась. Существующих не трогает."""
    if not sheets.is_enabled():
        await message.answer("❌ Google-таблица не подключена.")
        return
    await message.answer("⏳ Читаю таблицу и восстанавливаю участников...")
    users, progress = await recovery.restore_from_sheets()
    if not users and not progress:
        await message.answer("Ничего не восстановлено: в таблице пусто или все уже в базе.")
        return
    await message.answer(f"✅ Восстановлено из таблицы.\n• Участников: <b>{users}</b>\n"
                         f"• С прогрессом: <b>{progress}</b>\n\n"
                         f"<i>Существовавшие записи не тронуты.</i>")


@router.message(Command("reset_sheet_cache"), IsAdmin())
async def cmd_reset_sheet_cache(message: Message) -> None:
    """Бот помнит номера строк; после ручной вставки или удаления строк они устаревают."""
    sheets.reset_caches()
    await message.answer("✅ Кэш строк сброшен, номера перечитаются при следующей записи.\n"
                         "Если строки в таблице сдвинулись — запусти ещё /resync_sheets.")


# ── Сбросы ───────────────────────────────────────────────────────────────────
# Каждый показывает, что затронет, и ждёт кодовое слово. /cancel — отмена.

RESETS = {
    "game": {
        "word": "СБРОСИТЬ ИГРУ",
        "title": "СБРОС ИГРЫ",
        "count": count_game,
        "sheets": f"«{SHEET_PROGRESS}» — все строки ниже шапки",
        "kept": ["участники и их статусы сверки", "задания, фото-образцы и программа"],
    },
    "registrations": {
        "word": "СБРОСИТЬ РЕГИСТРАЦИИ",
        "title": "СБРОС РЕГИСТРАЦИЙ",
        "count": count_registrations,
        "sheets": (f"«{SHEET_REGISTRATIONS}» — все строки ниже шапки\n"
                   f"  • лист со списком — только служебные колонки бота и заливка"),
        "kept": ["прогресс в игре (играть можно будет после новой сверки)",
                 "задания, фото-образцы и программа",
                 "данные организаторов в листе со списком"],
    },
    "all": {
        "word": "СБРОСИТЬ ВСЁ",
        "title": "ПОЛНЫЙ СБРОС",
        "count": count_everything,
        "sheets": ("все строки ниже шапки в листах бота\n"
                   "  • лист со списком — только служебные колонки бота и заливка"),
        "kept": ["данные организаторов в листе со списком",
                 "задания, фото-образцы и программа"],
    },
}


async def _ask_reset(message: Message, state: FSMContext, kind: str) -> None:
    spec = RESETS[kind]
    counts = await spec["count"]()
    lines = [f"⚠️ <b>{spec['title']}</b>", "", "<b>Будет затронуто:</b>"]
    lines += [f"  • {name}: <b>{value}</b>" for name, value in counts.items()]
    if sheets.is_enabled():
        lines += ["", "<b>В Google-таблице очистится:</b>", f"  • {spec['sheets']}"]
    lines += ["", "<b>Не будет тронуто:</b>"]
    lines += [f"  ✅ {item}" for item in spec["kept"]]
    lines += ["", "<b>Действие необратимо.</b>",
              f"Для подтверждения отправь: <code>{spec['word']}</code>",
              "Отмена: /cancel"]
    await state.set_state(ResetConfirm.waiting_for_word)
    await state.update_data(reset_kind=kind)
    await message.answer("\n".join(lines))


@router.message(Command("reset_game"), IsAdmin())
async def cmd_reset_game(message: Message, state: FSMContext) -> None:
    if not FEATURE_BINGO:
        await message.answer("Модуль бинго выключен — сбрасывать нечего.")
        return
    await _ask_reset(message, state, "game")


@router.message(Command("reset_registrations"), IsAdmin())
async def cmd_reset_registrations(message: Message, state: FSMContext) -> None:
    if not FEATURE_SCREENING:
        await message.answer("Сверка выключена — сбрасывать нечего.")
        return
    await _ask_reset(message, state, "registrations")


@router.message(Command("reset_all"), IsAdmin())
async def cmd_reset_all(message: Message, state: FSMContext) -> None:
    await _ask_reset(message, state, "all")


@router.message(ResetConfirm.waiting_for_word, Command("cancel"))
async def cmd_reset_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено. Ничего не удалено.")


@router.message(ResetConfirm.waiting_for_word)
async def cmd_reset_confirm(message: Message, state: FSMContext) -> None:
    kind = (await state.get_data()).get("reset_kind")
    spec = RESETS.get(kind)
    if spec is None:
        await state.clear()
        return
    if (message.text or "").strip() != spec["word"]:
        await message.answer(f"❌ Не совпало. Отправь ровно <code>{spec['word']}</code> "
                             f"или /cancel.")
        return

    await state.clear()
    await message.answer("⏳ Сбрасываю...")
    if kind == "game":
        lines = await _do_reset_game()
    elif kind == "registrations":
        lines = await _do_reset_registrations()
    else:
        lines = await _do_reset_all()
    await message.answer("\n".join(lines))


def _report(title: str, removed: dict, cleared: dict, extra: list[str]) -> list[str]:
    lines = [f"✅ <b>{title}</b>", ""]
    lines += [f"  • {name}: {value}" for name, value in removed.items()]
    lines += extra
    if cleared:
        lines += ["", "<b>Листы очищены:</b>"]
        lines += [f"  • {name}: {'❌ ошибка' if rows < 0 else f'{rows} строк'}"
                  for name, rows in cleared.items()]
    return lines


def _marks_line(marks: int | None) -> list[str]:
    if not (sheets.is_enabled() and FEATURE_SCREENING):
        return []
    return ["  • отметки в листе со списком: "
            + ("❌ ошибка" if marks is None else f"очищено {marks} строк")]


async def _do_reset_game() -> list[str]:
    # Сначала очереди в памяти, чтобы ничего не дописалось после сброса
    dropped_rows = sheets.drop_pending([SHEET_PROGRESS], delegate_marks=False)
    dropped_proofs = admin_queue.drop_pending(admin_queue.TAG_PROOF)
    dropped_mutes = antispam.reset_all()
    removed = await reset_game()
    cleared = await sheets.clear_bot_sheets([SHEET_PROGRESS])
    return _report("Игра сброшена", removed, cleared, [
        f"  • строк в очереди на запись: {dropped_rows}",
        f"  • неотправленных пруфов: {dropped_proofs}",
        f"  • снято ограничений: {dropped_mutes}",
        "",
        "Старые карточки пруфов в админ-чате больше ничего не засчитают.",
    ])


async def _do_reset_registrations() -> list[str]:
    dropped_rows = sheets.drop_pending([SHEET_REGISTRATIONS])
    dropped_cards = admin_queue.drop_pending(admin_queue.TAG_SCREENING)
    removed, marked_rows = await reset_registrations()
    cleared = await sheets.clear_bot_sheets([SHEET_REGISTRATIONS])
    marks = await sheets.clear_delegate_marks(marked_rows)
    # В листе «Прогресс» есть колонка статуса — обновляем её
    await recovery.resync_to_sheets(registrations=False, progress=True)
    return _report("Регистрации сброшены", removed, cleared, [
        f"  • строк в очереди на запись: {dropped_rows}",
        f"  • неотправленных карточек сверки: {dropped_cards}",
        *_marks_line(marks),
        "",
        "Все участники пройдут сверку заново при следующем /start.",
    ])


async def _do_reset_all() -> list[str]:
    dropped_sheets = sheets.drop_pending()
    dropped_admin = admin_queue.drop_pending()
    dropped_mutes = antispam.reset_all()
    removed, marked_rows = await reset_everything()
    screening.reset_cache()
    sheets.reset_caches()
    cleared = await sheets.clear_bot_sheets()
    marks = await sheets.clear_delegate_marks(marked_rows)
    lines = _report("Сброшено всё", removed, cleared, [
        f"  • очередь записи: {dropped_sheets}",
        f"  • очередь уведомлений: {dropped_admin}",
        f"  • снято ограничений: {dropped_mutes}",
        *_marks_line(marks),
    ])

    if FEATURE_SCREENING:
        # Сразу перечитываем список, чтобы бот не остался с пустым
        # списком, при котором сверка не работает
        count = await screening.refresh_delegates()
        lines += ["", "<b>Список участников перечитан:</b>"]
        if count is None:
            lines += ["  ⚠️ Google недоступен — список ПУСТ, сверка не работает!",
                      "  Запусти /sync_delegates, когда связь появится."]
        elif count == 0:
            lines.append("  🔴 В листе со списком нет записей!")
        else:
            lines.append(f"  ✅ {count} записей")
    return lines
