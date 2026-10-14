"""
config.py — все настройки бота. Значения берутся из переменных окружения
(локально — из файла .env, см. .env.example).

Тексты сообщений и задания здесь не живут: они в content/texts.py
и content/tasks.py.
"""

import os

from dotenv import load_dotenv

from content.tasks import TASKS_DATA

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


def _int_list(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
# Кто может пользоваться админскими командами (Telegram ID через запятую)
ADMIN_IDS: list[int] = _int_list("ADMIN_IDS")
# Группа организаторов: сюда приходят пруфы и уведомления о сверке.
# Кнопки под ними может нажимать любой участник этой группы.
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)

# ── Модули ────────────────────────────────────────────────────────────────────
# Каждый модуль можно выключить. Выключенный модуль не регистрирует свои
# команды и не показывается в меню.
#   SCREENING — сверка пришедших со списком участников из Google-таблицы.
#               Выключен: в бота пускают всех.
#   AGENDA    — программа ивента, которую админы меняют прямо из Telegram.
#   BINGO     — игра: задания, пруфы, проверка в админ-чате, победители.
FEATURE_SCREENING: bool = _flag("FEATURE_SCREENING", True)
FEATURE_AGENDA: bool = _flag("FEATURE_AGENDA", True)
FEATURE_BINGO: bool = _flag("FEATURE_BINGO", True)

# ── Ивент ─────────────────────────────────────────────────────────────────────
# Название подставляется в тексты. CHANNEL_TITLE — старое имя переменной.
EVENT_NAME: str = os.getenv("EVENT_NAME") or os.getenv("CHANNEL_TITLE") or "Ивент"
# Канал с анонсами. Пусто — бот не упоминает канал и не показывает кнопку.
CHANNEL_URL: str = os.getenv("CHANNEL_URL", "")
# Часовой пояс ивента относительно UTC — для времени в таблице и выгрузках
TIMEZONE_OFFSET: int = int(os.getenv("TIMEZONE_OFFSET", "0"))

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
# Запасной адрес: бот переключится на него, если основной не отвечает.
# Пример — Railway: DATABASE_URL ведёт во внутреннюю сеть, которая иногда
# поднимается с опозданием, а публичный адрес работает всегда.
# DATABASE_PUBLIC_URL — старое имя переменной.
DATABASE_FALLBACK_URL: str = (
    os.getenv("DATABASE_FALLBACK_URL") or os.getenv("DATABASE_PUBLIC_URL") or ""
)
# Сколько раз пробовать подключиться при старте: в облаках сеть контейнера
# часто поднимается на несколько секунд позже самого процесса
DB_CONNECT_ATTEMPTS: int = int(os.getenv("DB_CONNECT_ATTEMPTS", "12"))
DB_CONNECT_DELAY: float = float(os.getenv("DB_CONNECT_DELAY", "2"))

# ── Задания бинго ─────────────────────────────────────────────────────────────
# Источник правды — content/tasks.py. Здесь производные словари.
TASKS: dict[int, str] = {
    t["task_number"]: f'{t["emoji"]} {t["name"]}'.strip() for t in TASKS_DATA
}
TASK_DESCRIPTIONS: dict[int, str] = {
    t["task_number"]: f'{t["emoji"]} <b>{t["name"]}</b>\n\n{t["description"]}'.strip()
    for t in TASKS_DATA
}
TASK_SUBMISSION_TYPES: dict[int, str] = {
    t["task_number"]: t["submission_type"] for t in TASKS_DATA
}
TASK_COUNT: int = len(TASKS)
# Сколько заданий нужно для победы. По умолчанию — все.
WIN_THRESHOLD: int = int(os.getenv("WIN_THRESHOLD") or TASK_COUNT)

# ── Статусы участника ─────────────────────────────────────────────────────────
# confirmed — найден в списке участников (по нику или телефону)
# manual    — подтверждён вручную админом
# pending   — сверить не удалось (отказался делиться контактом и т.п.)
# not_found — сверка проведена, в списке его нет
STATUS_CONFIRMED: str = "confirmed"
STATUS_MANUAL: str = "manual"
STATUS_PENDING: str = "pending"
STATUS_NOT_FOUND: str = "not_found"

# Кого пускать в бинго. Чтобы пускать и тех, кто отказался делиться
# контактом, добавь STATUS_PENDING.
ALLOWED_TO_PLAY: set[str] = {STATUS_CONFIRMED, STATUS_MANUAL}

STATUS_LABELS: dict[str, str] = {
    STATUS_CONFIRMED: "✅ подтверждён",
    STATUS_MANUAL: "✅ подтверждён вручную",
    STATUS_PENDING: "⚠️ данные не сверены",
    STATUS_NOT_FOUND: "❌ нет в базе",
}

# Что делать, если список участников не загрузился и сверять не с чем:
#   false (по умолчанию) — пускать всех: поломка интеграции не остановит ивент,
#                          но и отсева в этот момент нет;
#   true                 — не пускать никого: отсев гарантирован, но при
#                          поломке свои участники тоже получат отказ.
SCREENING_STRICT: bool = _flag("SCREENING_STRICT", False)

# ── Google Sheets (необязательно) ─────────────────────────────────────────────
# Ключ service account: содержимое JSON одной строкой ИЛИ путь к файлу.
GOOGLE_CREDENTIALS_JSON: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "")
# ID таблицы — часть URL между /d/ и /edit
GOOGLE_SPREADSHEET_ID: str = os.getenv("GOOGLE_SPREADSHEET_ID", "")

SHEET_DELEGATES: str = os.getenv("SHEET_DELEGATES", "Делегаты")
SHEET_REGISTRATIONS: str = os.getenv("SHEET_REGISTRATIONS", "Регистрации")
SHEET_PROGRESS: str = os.getenv("SHEET_PROGRESS", "Прогресс")
# Как часто перечитывать список участников из таблицы, минут
DELEGATES_REFRESH_MINUTES: int = int(os.getenv("DELEGATES_REFRESH_MINUTES", "5"))

# Записи в таблицу копятся в памяти и уходят одним запросом раз в
# SHEETS_FLUSH_SECONDS: расход квоты Google не зависит от числа участников.
SHEETS_FLUSH_SECONDS: float = float(os.getenv("SHEETS_FLUSH_SECONDS", "5"))
SHEETS_MAX_BATCH: int = int(os.getenv("SHEETS_MAX_BATCH", "400"))

# ── Антиспам ──────────────────────────────────────────────────────────────────
# Больше SPAM_MAX_SUBMISSIONS пруфов за SPAM_WINDOW_SECONDS — приём заявок
# от участника приостанавливается на SPAM_MUTE_SECONDS.
SPAM_WINDOW_SECONDS: int = int(os.getenv("SPAM_WINDOW_SECONDS", "300"))
SPAM_MAX_SUBMISSIONS: int = int(os.getenv("SPAM_MAX_SUBMISSIONS", "6"))
SPAM_MUTE_SECONDS: int = int(os.getenv("SPAM_MUTE_SECONDS", "900"))

# ── Админ-чат ─────────────────────────────────────────────────────────────────
# Минимальная пауза между сообщениями в админ-чат; дальше бот сам
# подстраивает темп под лимиты Telegram
ADMIN_NOTIFY_INTERVAL: float = float(os.getenv("ADMIN_NOTIFY_INTERVAL", "0.5"))
# Если уведомлений о сверке накопилось больше — шлём одну сводку
ADMIN_NOTIFY_DIGEST_AT: int = int(os.getenv("ADMIN_NOTIFY_DIGEST_AT", "8"))

# ── Прочее ────────────────────────────────────────────────────────────────────
# Сколько ждать остальные фото альбома, прежде чем принять его как один пруф
ALBUM_LATENCY: float = 0.7
# Ключи в таблице settings
AGENDA_TEXT_KEY: str = "agenda_text"
AGENDA_PHOTO_KEY: str = "agenda_photo"
