"""
handlers/screening_admin.py — сверка со стороны организаторов
(модуль FEATURE_SCREENING).

  кнопки «Подтвердить / Отказать» под карточкой непрошедшего сверку
  /pending        — кто ждёт решения + массовое подтверждение несверенных
  /verify @ник    — подтвердить командой (когда карточки схлопнулись в сводку)
  /check @ник     — что бот думает про человека: есть ли в списке, пустят ли
  /delegates      — что бот вычитал из листа со списком, какие колонки узнал
  /sync_delegates — перечитать список прямо сейчас
"""

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import STATUS_LABELS, STATUS_MANUAL, STATUS_NOT_FOUND, STATUS_PENDING
from content import texts
from db.delegates import (
    count_delegates,
    count_unmatchable_delegates,
    find_delegate_by_phone,
    find_delegate_by_username,
    normalize_phone,
    sample_delegates,
)
from db.users import count_by_status, find_user, list_unverified, list_user_ids_by_status
from services import sheets
from services.screening import can_play, refresh_delegates, set_manual_status
from tg.cards import mark_card
from tg.filters import FromAdminChat, IsAdmin
from tg.keyboards import (
    bulk_verify_confirm_keyboard,
    bulk_verify_keyboard,
    main_menu_keyboard,
    retry_check_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="screening_admin")


async def _notify(bot: Bot, user_id: int, text: str, markup) -> bool:
    try:
        await bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
        return True
    except Exception as exc:
        logger.warning("Не удалось уведомить %s: %s", user_id, exc)
        return False


def _nick(row) -> str:
    return f"@{row['username']}" if row["username"] else f"ID {row['user_id']}"


# ── Кнопки под карточкой ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("verify:"), FromAdminChat())
async def verify_button(callback: CallbackQuery, bot: Bot) -> None:
    user_id = int(callback.data.split(":")[1])
    admin = callback.from_user.full_name
    info = await set_manual_status(user_id, STATUS_MANUAL, admin)
    await _notify(bot, user_id, texts.MANUALLY_VERIFIED, main_menu_keyboard())
    await mark_card(callback, f"\n\n✅ <b>Подтверждён</b> — {html.escape(admin)}")
    await callback.answer(f"✅ {('@' + info['username']) if info['username'] else user_id} подтверждён")


@router.callback_query(F.data.startswith("unverify:"), FromAdminChat())
async def unverify_button(callback: CallbackQuery, bot: Bot) -> None:
    user_id = int(callback.data.split(":")[1])
    admin = callback.from_user.full_name
    await set_manual_status(user_id, STATUS_NOT_FOUND, admin)
    await _notify(bot, user_id, texts.MANUALLY_REJECTED, retry_check_keyboard(with_retry=False))
    await mark_card(callback, f"\n\n🚫 <b>Отказано</b> — {html.escape(admin)}")
    await callback.answer("🚫 Участнику отказано")


# ── /pending и массовое подтверждение ────────────────────────────────────────

@router.message(Command("pending"), IsAdmin())
async def cmd_pending(message: Message) -> None:
    rows = await list_unverified(50)
    if not rows:
        await message.answer("✅ Все участники прошли сверку — ждущих нет.")
        return

    lines = [f"⚠️ <b>Ждут решения ({len(rows)}):</b>\n"]
    for row in rows:
        mark = "❌" if row["status"] == STATUS_NOT_FOUND else "⚠️"
        phone = f" | 📞 {html.escape(row['phone'])}" if row["phone"] else ""
        lines.append(f"{mark} {_nick(row)} — {html.escape(row['full_name'] or '—')}{phone}")
    lines += ["",
              "❌ — сверка проведена, в списке нет",
              "⚠️ — сверить не удалось (не поделился контактом)",
              "",
              "Подтвердить одного: <code>/verify @ник</code>"]

    # Массово — только ⚠️. ❌ точно нет в списке: подтвердить их скопом
    # значило бы отменить сверку целиком.
    unverified = await count_by_status(STATUS_PENDING)
    markup = None
    if unverified:
        markup = bulk_verify_keyboard(unverified)
        lines.append(f"Кнопка ниже подтвердит <b>{unverified}</b> участников со статусом ⚠️. "
                     f"Помеченных ❌ она не тронет.")
    await message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data == "bulkverify:ask", FromAdminChat())
async def bulk_ask(callback: CallbackQuery) -> None:
    count = await count_by_status(STATUS_PENDING)
    if not count:
        await callback.answer("Некого подтверждать", show_alert=True)
        return
    await callback.message.answer(
        f"⚠️ <b>Подтвердить {count} участников разом?</b>\n\n"
        f"Это те, кто не поделился контактом. Они получат доступ к бинго, "
        f"каждому уйдёт уведомление.\n\n"
        f"<b>Тех, кого нет в списке (❌), это не затронет.</b>\n"
        f"Отменять придётся по одному.",
        reply_markup=bulk_verify_confirm_keyboard(count))
    await callback.answer()


@router.callback_query(F.data == "bulkverify:cancel", FromAdminChat())
async def bulk_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("↩️ Отменено. Никто не подтверждён.")
    await callback.answer()


@router.callback_query(F.data == "bulkverify:go", FromAdminChat())
async def bulk_go(callback: CallbackQuery, bot: Bot) -> None:
    admin = callback.from_user.full_name
    user_ids = await list_user_ids_by_status(STATUS_PENDING)
    if not user_ids:
        await callback.answer("Некого подтверждать", show_alert=True)
        return
    await callback.answer(f"Подтверждаю {len(user_ids)}...")
    await callback.message.edit_text(f"⏳ Подтверждаю {len(user_ids)} участников...")

    confirmed = notified = 0
    for user_id in user_ids:
        try:
            await set_manual_status(user_id, STATUS_MANUAL, admin)
            confirmed += 1
        except Exception as exc:
            logger.error("Массовое подтверждение: %s — %s", user_id, exc)
            continue
        # Уведомления идут участникам в личку (лимит ~30 в секунду),
        # небольшая пауза держит нас ниже него
        await asyncio.sleep(0.05)
        notified += await _notify(bot, user_id, texts.MANUALLY_VERIFIED, main_menu_keyboard())

    await callback.message.answer(
        f"✅ Подтверждено: <b>{confirmed}</b>\n"
        f"📨 Уведомлено: <b>{notified}</b> (остальные могли заблокировать бота)\n\n"
        f"Проверить: /pending")


# ── /verify ───────────────────────────────────────────────────────────────────

@router.message(Command("verify"), IsAdmin())
async def cmd_verify(message: Message, bot: Bot) -> None:
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Использование: <code>/verify @ник</code> или "
                             "<code>/verify 123456789</code>")
        return
    user = await find_user(query)
    if not user:
        await message.answer("❌ Такой участник ещё не запускал бота.")
        return
    await set_manual_status(user["user_id"], STATUS_MANUAL, message.from_user.full_name)
    await _notify(bot, user["user_id"], texts.MANUALLY_VERIFIED, main_menu_keyboard())
    await message.answer(f"✅ {_nick(user)} подтверждён.")


# ── Диагностика ──────────────────────────────────────────────────────────────

@router.message(Command("delegates"), IsAdmin())
async def cmd_delegates(message: Message) -> None:
    """Первое, что стоит запустить, если бот «не узнаёт» участников."""
    total = await count_delegates()
    lines = [f"📋 <b>Список участников: {total} записей</b>", ""]

    if not sheets.is_enabled():
        lines += ["❌ Google Sheets не подключён — список брать неоткуда.",
                  "Проверь GOOGLE_SPREADSHEET_ID и ключ service account."]
        await message.answer("\n".join(lines))
        return

    if sheets.LAST_DETECTION:
        lines.append("<b>Что распознано в шапке:</b>")
        for key, value in sheets.LAST_DETECTION.items():
            mark = "❌" if value == "НЕ НАЙДЕНА" else "•"
            lines.append(f"{mark} {key}: <code>{html.escape(value)}</code>")
    else:
        lines.append("<i>Лист ещё не читался. Запусти /sync_delegates.</i>")

    if total == 0:
        lines += ["",
                  "🔴 <b>Список пуст — сверка не работает.</b>",
                  "Обычно причина в том, что лист называется иначе, пуст, "
                  "или в шапке нет колонки с ником и телефоном."]
    else:
        sample = await sample_delegates(3)
        if sample:
            lines += ["", "<b>Примеры записей:</b>"]
            for row in sample:
                nick = f"@{row['username']}" if row["username"] else "—"
                lines.append(f"• {html.escape(row['full_name'] or '—')} | ник {nick} | "
                             f"тел {row['phone'] or '—'}")
        broken = await count_unmatchable_delegates()
        if broken:
            lines += ["", f"⚠️ У <b>{broken}</b> записей нет ни ника, ни телефона — "
                          f"этих людей бот сверить не сможет."]
    lines += ["", "Проверить человека: <code>/check @ник</code>"]
    await message.answer("\n".join(lines))


@router.message(Command("check"), IsAdmin())
async def cmd_check(message: Message) -> None:
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Использование:\n<code>/check @ник</code>\n"
                             "<code>/check +77001234567</code>\n<code>/check 123456789</code>")
        return

    lines = [f"🔍 <b>Проверка: {html.escape(query)}</b>", ""]
    if any(ch.isdigit() for ch in query) and len(normalize_phone(query)) >= 10:
        delegate = await find_delegate_by_phone(query)
        lines.append("Искали в списке по телефону.")
    else:
        delegate = await find_delegate_by_username(query)
        lines.append("Искали в списке по нику.")

    if delegate:
        lines += [f"✅ В списке: строка {delegate['sheet_row']}, "
                  f"{html.escape(delegate['full_name'] or '—')}",
                  f"   ник: <code>{delegate['username'] or '—'}</code>, "
                  f"телефон: <code>{delegate['phone'] or '—'}</code>"]
    else:
        lines.append("❌ В списке НЕ найден.")
        if await count_delegates() == 0:
            lines.append("   (список вообще пуст — смотри /delegates)")

    user = await find_user(query)
    lines.append("")
    if user:
        status = user["status"] or STATUS_PENDING
        lines += [f"👤 В боте: ID <code>{user['user_id']}</code>, "
                  f"{html.escape(user['full_name'] or '—')}",
                  f"📋 Статус: <b>{STATUS_LABELS.get(status, status)}</b>"
                  + (" 🔒 решение админа" if user["status_locked"] else ""),
                  f"💬 {html.escape(user['status_comment'] or '—')}",
                  f"📞 Телефон в боте: <code>{html.escape(user['phone'] or '—')}</code>",
                  f"🎯 Пустят ли в бинго: <b>{'ДА' if await can_play(user['user_id']) else 'НЕТ'}</b>"]
    else:
        lines.append("👤 В боте: ещё ни разу не запускал /start")
    await message.answer("\n".join(lines))


@router.message(Command("sync_delegates"), IsAdmin())
async def cmd_sync_delegates(message: Message) -> None:
    if not sheets.is_enabled():
        await message.answer("❌ Google Sheets не подключён. Проверь "
                             "<code>GOOGLE_SPREADSHEET_ID</code> и ключ service account.")
        return
    await message.answer("⏳ Читаю список участников...")
    count = await refresh_delegates()
    if count is None:
        cached = await count_delegates()
        await message.answer(f"⚠️ Google недоступен. Работаем на кэше: {cached} записей.\n"
                             "Подробности — в логах бота.")
        return
    await message.answer(f"✅ Список обновлён: <b>{count}</b> записей.")
