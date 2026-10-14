"""
services/admin_queue.py — очередь всех сообщений в админ-чат.

Зачем: Telegram разрешает боту около 20 сообщений в минуту в одну группу.
Через админ-чат идёт три потока — пруфы на проверку, уведомления о не
прошедших сверку и служебные сообщения. Пруфов больше всего: 200 делегатов
по 9 заданий это 1800 сообщений за ивент. Без очереди бот получал бы 429,
и часть пруфов просто не дошла бы до организаторов — молча.

Как решено:
  • хэндлер кладёт в очередь готовую функцию отправки и не ждёт ничего;
  • воркер вызывает их не чаще, чем раз в ADMIN_NOTIFY_INTERVAL секунд;
  • на 429 ждёт столько, сколько просит Telegram, и повторяет — ничего
    не теряется;
  • уведомления о непрошедших сверку помечены как «схлопываемые»: если их
    накопилось больше ADMIN_NOTIFY_DIGEST_AT, они сливаются в одну сводку.
    Пруфы не схлопываются — у них фото и кнопки решения.

Порядок сохраняется: пруфы уходят в том же порядке, в каком пришли.
"""

import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramRetryAfter

from config import ADMIN_CHAT_ID, ADMIN_NOTIFY_DIGEST_AT, ADMIN_NOTIFY_INTERVAL

logger = logging.getLogger(__name__)

# Элемент: {"send": фабрика корутины, "digest_line": str, "coalescable": bool,
#           "tag": str}
# tag — к какому потоку относится сообщение: "proof" (пруфы на проверку),
# "screening" (не прошедшие сверку) или "" (служебное). Нужен, чтобы
# сброс игры выкидывал из очереди только пруфы, а сброс регистраций —
# только карточки сверки.
TAG_PROOF = "proof"
TAG_SCREENING = "screening"
_queue: list[dict] = []
_lock = asyncio.Lock()
_has_items = asyncio.Event()
_dropped = 0

# Больше этого в очереди — начинаем ругаться в лог, чтобы было видно затор
_BACKLOG_WARN = 50

# ── Адаптивный темп ───────────────────────────────────────────────────────────
# Задача: использовать столько полосы, сколько Telegram реально даёт, и при
# этом никогда не терять сообщения.
#
# Фиксированная пауза плоха: документированные «20 сообщений в минуту в
# группу» — ориентир, и держать себя заведомо ниже значит искусственно
# затормозить разбор пруфов.
#
# Экспоненциальный backoff тоже плох, и это выяснилось на нагрузочном тесте:
# после ОДНОГО отказа пауза улетала до 30 секунд и восстанавливалась
# десятками успешных отправок. Доставка падала до 2 сообщений в минуту там,
# где разрешено 20 — то есть лечение оказалось хуже болезни.
#
# Поэтому: Telegram в ответе 429 сам присылает retry_after. Ждём ровно
# столько, а темп после отказа ставим на «безопасный» уровень чуть ниже
# документированного лимита и оттуда плавно разгоняемся обратно.
_MIN_INTERVAL = max(0.3, ADMIN_NOTIFY_INTERVAL)   # быстрее не разгоняемся
_SAFE_INTERVAL = 3.0     # ~20 сообщений в минуту — документированный лимит
_MAX_INTERVAL = 8.0      # предел, если лимит бьёт даже по безопасному темпу
_SPEEDUP_AFTER = 5       # успешных подряд — пробуем чуть быстрее

_interval = _MIN_INTERVAL
_ok_streak = 0


def _slow_down() -> None:
    """
    Отказ от Telegram. Первый раз опускаемся сразу до безопасного темпа,
    дальше добавляем по секунде — без разгона в бесконечность.
    """
    global _interval, _ok_streak
    if _interval < _SAFE_INTERVAL:
        _interval = _SAFE_INTERVAL
    else:
        _interval = min(_interval + 1.0, _MAX_INTERVAL)
    _ok_streak = 0
    logger.warning("Админ-чат: сбавляю темп до %.1f c между сообщениями", _interval)


def _speed_up() -> None:
    """Серия успешных отправок — осторожно возвращаем скорость."""
    global _interval, _ok_streak
    _ok_streak += 1
    if _ok_streak >= _SPEEDUP_AFTER and _interval > _MIN_INTERVAL:
        _interval = max(_interval - 0.5, _MIN_INTERVAL)
        _ok_streak = 0


def pending_count() -> int:
    """Сколько сообщений ждёт отправки — для /queue."""
    return len(_queue)


def dropped_count() -> int:
    """Сколько сообщений потеряно (только при переполнении)."""
    return _dropped


def enqueue_send(send: Callable[[], Awaitable], *, digest_line: str = "",
                 coalescable: bool = False, tag: str = "") -> None:
    """
    Ставит отправку в очередь. Мгновенно, без сети.

    send         — функция без аргументов, возвращающая корутину отправки.
                   Именно функция, а не корутина: при 429 её нужно вызвать
                   заново, а корутину повторно не запустишь.
    digest_line  — строка для сводки, если очередь схлопнётся.
    coalescable  — можно ли схлопнуть в сводку. У пруфов нельзя: там фото
                   и кнопки «Принять / Отклонить».
    """
    if not ADMIN_CHAT_ID:
        return
    _queue.append({
        "send": send,
        "digest_line": digest_line,
        "coalescable": coalescable,
        "tag": tag,
    })
    _has_items.set()

    if len(_queue) == _BACKLOG_WARN:
        logger.warning(
            "Админ-чат: в очереди %s сообщений — организаторы увидят их с задержкой",
            _BACKLOG_WARN,
        )


def enqueue(bot, text: str, reply_markup=None, digest_line: str = "",
            coalescable: bool = True, tag: str = "") -> None:
    """Удобная обёртка для обычного текстового уведомления."""
    def send():
        return bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text,
            reply_markup=reply_markup, parse_mode="HTML",
        )
    enqueue_send(send, digest_line=digest_line, coalescable=coalescable, tag=tag)


async def _send_digest(bot, items: list[dict]) -> None:
    """Схлопывает пачку схлопываемых уведомлений в одно сообщение."""
    lines = [
        f"⚠️ <b>Не прошли сверку: {len(items)} чел.</b>",
        "",
        "Пришли одновременно, поэтому одной сводкой вместо карточек:",
        "",
    ]
    lines += [item["digest_line"] for item in items if item["digest_line"]]
    lines += [
        "",
        "Подтвердить: <code>/verify @ник</code> или <code>/verify ID</code>",
        "Весь список с деталями: /pending",
    ]
    await bot.send_message(
        chat_id=ADMIN_CHAT_ID, text="\n".join(lines), parse_mode="HTML"
    )


async def worker(bot) -> None:
    """
    Фоновый воркер. Держит темп ниже лимита Telegram, схлопывает уведомления
    о сверке при наплыве и никогда не теряет пруфы.
    """
    while True:
        await _has_items.wait()

        async with _lock:
            if not _queue:
                _has_items.clear()
                continue

            coalescable = [i for i in _queue if i["coalescable"]]
            if len(coalescable) >= ADMIN_NOTIFY_DIGEST_AT:
                # Забираем ТОЛЬКО схлопываемые, пруфы остаются в очереди
                for item in coalescable:
                    _queue.remove(item)
                job = ("digest", coalescable)
            else:
                job = ("single", _queue.pop(0))

            if not _queue:
                _has_items.clear()

        kind, payload = job
        try:
            if kind == "digest":
                await _send_digest(bot, payload)
                logger.info("Админ-чат: отправлена сводка на %s чел.", len(payload))
            else:
                await payload["send"]()
            _speed_up()
        except TelegramRetryAfter as exc:
            # Упёрлись в реальный лимит — ждём сколько просят, возвращаем
            # сообщение в очередь и снижаем темп на будущее
            logger.warning("Админ-чат: лимит Telegram, пауза %s с", exc.retry_after)
            _slow_down()
            async with _lock:
                if kind == "digest":
                    _queue[:0] = payload
                else:
                    _queue.insert(0, payload)
                _has_items.set()
            await asyncio.sleep(exc.retry_after)
            continue
        except Exception as exc:
            global _dropped
            _dropped += 1
            logger.error("Админ-чат: сообщение не отправлено — %s", exc)

        await asyncio.sleep(_interval)


def drop_pending(tag: str | None = None) -> int:
    """
    Выбрасывает неотправленные уведомления. tag=None — все, иначе только
    с этой меткой. Возвращает сколько выброшено.
    """
    before = len(_queue)
    _queue[:] = [i for i in _queue if tag is not None and i["tag"] != tag]
    if not _queue:
        _has_items.clear()
    return before - len(_queue)
