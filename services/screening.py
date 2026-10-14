"""
services/screening.py — сверка пришедшего в бота человека со списком
участников, прошедших отбор (лист «Делегаты» в Google-таблице).

Модуль включается флагом FEATURE_SCREENING. Выключен — в бота пускают
всех, а can_play() разрешает игру любому участнику.

Логика сверки комбинированная:
  1. Ищем по телеграм-нику.
  2. Не нашли — просим поделиться контактом и ищем по номеру телефона.
  3. Отказался делиться — статус «данные не сверены»: человек остаётся
     в боте и видит адженду с каналом, но помечается жёлтым в таблице,
     а админ-чат получает уведомление с кнопкой ручного подтверждения.

Если список делегатов не загружен вообще (Google недоступен и кэш пуст),
бот никого не блокирует — на живом ивенте лучше пропустить лишнего,
чем не пустить всех.
"""

import html
import logging

from config import (
    ADMIN_CHAT_ID,
    ALLOWED_TO_PLAY,
    FEATURE_SCREENING,
    SCREENING_STRICT,
    STATUS_CONFIRMED,
    STATUS_MANUAL,
    STATUS_NOT_FOUND,
    STATUS_LABELS,
)
from db.delegates import (
    count_delegates,
    find_delegate_by_phone,
    find_delegate_by_username,
    normalize_phone,
    normalize_username,
    replace_delegates,
)
from db.users import get_user, get_user_status, save_screening_result, set_manual_decision
from services import admin_queue, sheets
from services.timefmt import to_local
from tg.keyboards import admin_verify_keyboard

logger = logging.getLogger(__name__)

# Сколько делегатов в кэше БД. Список меняется раз в несколько минут,
# поэтому держим число в памяти и не гоняем COUNT(*) на каждую проверку.
_delegate_count: int | None = None


# ── Обновление списка делегатов из Google Sheets ─────────────────────────────

async def refresh_delegates() -> int | None:
    """
    Перечитывает лист «Делегаты» и обновляет локальный кэш в PostgreSQL.
    Возвращает количество записей либо None, если Google недоступен —
    в этом случае продолжает работать предыдущий кэш.
    """
    global _delegate_count

    delegates = await sheets.fetch_delegates()
    if delegates is None:
        logger.warning("Список делегатов не обновлён — используем кэш из БД")
        return None

    count = await replace_delegates(delegates)
    if count:
        _delegate_count = count
    return count


def reset_cache() -> None:
    """Забыть, сколько делегатов в базе — пересчитается при следующем запросе."""
    global _delegate_count
    _delegate_count = None


async def delegate_count() -> int:
    """Число делегатов в кэше БД. Читается из базы только один раз."""
    global _delegate_count
    if _delegate_count is None:
        _delegate_count = await count_delegates()
    return _delegate_count


# ── Сверка ────────────────────────────────────────────────────────────────────

async def match_delegate(username: str, phone: str = "") -> dict | None:
    """
    Сверяет человека со списком делегатов.

    Возвращает:
      None — сверить нечем (ник не найден, телефон ещё не получен) →
             вызывающий код должен запросить контакт;
      dict — {"status": ..., "comment": ..., "row": номер строки в таблице}.
    """
    if username:
        delegate = await find_delegate_by_username(username)
        if delegate:
            comment = ""
            if phone and delegate["phone"] and normalize_phone(phone) != delegate["phone"]:
                comment = "⚠️ телефон не совпадает с указанным в таблице"
            return {
                "status": STATUS_CONFIRMED,
                "comment": comment,
                "row": delegate["sheet_row"],
            }

    if phone:
        delegate = await find_delegate_by_phone(phone)
        if delegate:
            notes = ["сверено по телефону"]
            if not delegate["username"]:
                notes.append("в таблице не указан ник")
            elif username and normalize_username(username) != delegate["username"]:
                notes.append(f"⚠️ ник в таблице: @{delegate['username']}")
            return {
                "status": STATUS_CONFIRMED,
                "comment": "; ".join(notes),
                "row": delegate["sheet_row"],
            }
        return {
            "status": STATUS_NOT_FOUND,
            "comment": "не найден ни по нику, ни по телефону",
            "row": None,
        }

    return None


async def should_notify(user_id: int, status: str, comment: str) -> bool:
    """
    Уведомлять админ-чат только если результат сверки изменился —
    иначе повторные /start и «Проверить снова» завалят чат дублями.
    Вызывать ДО apply_result.
    """
    user = await get_user(user_id)
    if not user:
        return True
    return (user["status"], user["status_comment"] or "") != (status, comment)


async def apply_result(user_id: int, username: str, full_name: str,
                       result: dict, phone: str = "") -> None:
    """
    Записывает результат сверки в БД и ставит строки в очередь на запись
    в Google Sheets.

    Одно обращение к базе вместо трёх и ноль сетевых вызовов: строки уедут
    в таблицу фоновым воркером пачкой. Хэндлер отвечает участнику сразу.
    """
    user = await save_screening_result(
        user_id, result["status"], result.get("comment", ""),
        result.get("row"), phone,
    )

    sheets.queue_registration(
        user_id=user_id,
        username=username,
        full_name=full_name,
        phone=phone or ((user["phone"] or "") if user else ""),
        status=result["status"],
        comment=result.get("comment", ""),
        first_seen=to_local(user["first_seen"] if user else None),
    )

    if result.get("row"):
        sheets.queue_delegate_mark(
            row=result["row"],
            status=result["status"],
            user_id=user_id,
            username=username,
        )


# ── Доступ к бинго ────────────────────────────────────────────────────────────

async def screening_possible() -> bool:
    """
    Есть ли с чем сверять. Пустой список делегатов = сверка невозможна.
    Число берётся из кэша в памяти, обновляемого вместе со списком.
    """
    return await delegate_count() > 0


async def can_play(user_id: int) -> bool:
    """
    Допущен ли участник к бинго.

    Правила по убыванию силы:
      1. confirmed / manual — пускаем всегда;
      2. not_found — НЕ пускаем НИКОГДА. Сверка была проведена, человека
         в списке нет, бот ему об этом написал. Раньше здесь срабатывал
         общий fail-open, и участник с явным вердиктом «нет в базе» всё
         равно попадал в бинго — это и был баг «пропускает незарегистрированных»;
      3. pending — сверку провести не удалось. Пускаем только если сверять
         действительно не с чем (список делегатов не загрузился), иначе
         поломанная интеграция остановила бы весь ивент.
    """
    if not FEATURE_SCREENING:
        return True

    status = await get_user_status(user_id)

    if status in ALLOWED_TO_PLAY:
        return True

    # Вердикт «нет в базе» вынесен по данным — fail-open его не отменяет
    if status == STATUS_NOT_FOUND:
        return False

    if not await screening_possible():
        if SCREENING_STRICT:
            logger.error(
                "Список делегатов пуст, SCREENING_STRICT=true — "
                "блокируем участника %s", user_id
            )
            return False
        logger.warning(
            "Список делегатов пуст — пропускаем участника %s без сверки", user_id
        )
        return True

    return False


# ── Уведомление админ-чата ────────────────────────────────────────────────────

def _username_display(username: str, user_id: int) -> str:
    return f"@{username}" if username else f"ID {user_id}"


def notify_admins(bot, user_id: int, username: str, full_name: str,
                  status: str, comment: str = "", phone: str = "") -> None:
    """
    Сообщает в админ-чат о человеке, который запустил бота, но не прошёл
    автоматическую сверку. К сообщению прикладывается кнопка ручного
    подтверждения.
    """
    if not ADMIN_CHAT_ID:
        return

    header = (
        "🚨 <b>Незарегистрированный участник</b>"
        if status == STATUS_NOT_FOUND
        else "⚠️ <b>Участник без сверки данных</b>"
    )

    lines = [
        header,
        "",
        f"👤 Ник: {_username_display(username, user_id)}",
        f"📝 Имя в Telegram: {html.escape(full_name or '—')}",
        f"🆔 ID: <code>{user_id}</code>",
    ]
    if phone:
        lines.append(f"📞 Телефон: <code>{phone}</code>")
    lines.append(f"📋 Статус: {STATUS_LABELS.get(status, status)}")
    if comment:
        lines.append(f"💬 {html.escape(comment)}")
    lines.append("")
    lines.append(
        "Его нет в списке прошедших делегатов. Если участие подтвердят "
        "на стойке регистрации — нажми кнопку ниже."
    )

    digest_line = (
        f"• {_username_display(username, user_id)} — {html.escape(full_name or '—')}"
        + (f" | 📞 {phone}" if phone else "")
    )
    admin_queue.enqueue(
        bot,
        text="\n".join(lines),
        reply_markup=admin_verify_keyboard(user_id),
        digest_line=digest_line,
        coalescable=True,
        tag=admin_queue.TAG_SCREENING,
    )


async def set_manual_status(user_id: int, status: str, admin_name: str) -> dict:
    """
    Ручное решение админа по участнику. Возвращает данные пользователя
    для последующего уведомления.

    Решение блокирует статус: «Проверить снова» его не перебьёт. Снять
    блокировку может только другое ручное решение или /reset_registrations.
    """
    comment = (
        f"подтверждён вручную ({admin_name})"
        if status == STATUS_MANUAL
        else f"отклонён вручную ({admin_name})"
    )
    await set_manual_decision(user_id, status, comment)

    user = await get_user(user_id)
    username = user["username"] if user else ""
    full_name = (user["full_name"] if user else "") or ""
    phone = (user["phone"] if user else "") or ""

    sheets.queue_registration(
        user_id=user_id,
        username=username,
        full_name=full_name,
        phone=phone,
        status=status,
        comment=comment,
        first_seen=to_local(user["first_seen"] if user else None),
        # Решение админа — не визит участника
        last_seen=to_local(user["last_seen"] if user else None),
    )

    return {"username": username, "full_name": full_name, "phone": phone}
