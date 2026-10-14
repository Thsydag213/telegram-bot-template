"""
services/sheets.py — синхронизация с Google Sheets (необязательная).

Не настроены GOOGLE_SPREADSHEET_ID и ключ — модуль молча выключен,
бот работает только с базой. Три листа в одной таблице:
  • «Делегаты»     — список прошедших скрининг (заполняют организаторы).
                     Бот только читает его и дописывает служебные колонки
                     «Статус в боте», «TG user_id», «Время входа».
  • «Регистрации»  — дописывается при каждом /start: user_id, username,
                     телефон, статус сверки, дата первой регистрации
                     (берётся из БД и не перезаписывается) и последний вход.
  • «Прогресс»     — обновляется при каждом одобрении пруфа админом:
                     ✓/✗ по каждому заданию, счётчик, время победы.

Два правила, на которых держится модуль:

1. Ни одна ошибка Google не должна ронять бота. Все публичные функции
   ловят исключения и только логируют их.

2. Экономия запросов. У Google 60 чтений и 60 записей в минуту, а на входе
   ивента люди сканируют QR одновременно. Поэтому:
     • объекты листов кэшируются — без повторных fetch_sheet_metadata;
     • номера строк держатся в памяти — без перечитывания колонки A;
     • значения и цвет строки пишутся ОДНИМ запросом updateCells;
     • дата первой регистрации берётся из БД, а не вычитывается из таблицы;
     • записи копятся в памяти и уходят одним batch_update раз в несколько
       секунд — расход квоты не зависит от числа участников.
   Итог: ~12 запросов в минуту при любой толпе.
"""

import asyncio
import json
import logging

from config import (
    FEATURE_BINGO,
    FEATURE_SCREENING,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_SPREADSHEET_ID,
    SHEET_DELEGATES,
    SHEET_REGISTRATIONS,
    SHEET_PROGRESS,
    SHEETS_FLUSH_SECONDS,
    SHEETS_MAX_BATCH,
    TASKS,
    STATUS_CONFIRMED,
    STATUS_MANUAL,
    STATUS_PENDING,
    STATUS_NOT_FOUND,
    STATUS_LABELS,
)

from services.timefmt import local_to_iso, now_local, to_local

logger = logging.getLogger(__name__)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _LIB_OK = True
except ImportError:  # библиотеки не установлены — просто работаем без Sheets
    _LIB_OK = False
    logger.warning(
        "gspread/google-auth не установлены — синхронизация с Google Sheets отключена"
    )

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Цвет строки в зависимости от статуса сверки
_STATUS_COLORS = {
    STATUS_CONFIRMED: {"red": 0.85, "green": 0.94, "blue": 0.83},   # зелёный
    STATUS_MANUAL:    {"red": 0.82, "green": 0.89, "blue": 0.96},   # синий
    STATUS_PENDING:   {"red": 1.00, "green": 0.90, "blue": 0.60},   # жёлтый
    STATUS_NOT_FOUND: {"red": 0.96, "green": 0.80, "blue": 0.80},   # красный
}

# Служебные колонки, которые бот дописывает в лист «Делегаты»
_DELEGATE_BOT_COLUMNS = ["Статус в боте", "TG user_id", "Время входа"]

_DELEGATE_HEADERS = ["ФИО", "Telegram username", "Телефон", "Email"] + _DELEGATE_BOT_COLUMNS

REGISTRATION_HEADERS = [
    "user_id", "username", "Имя в Telegram", "Телефон",
    "Статус", "Комментарий", "Первая регистрация", "Последний вход",
]

# Сколько строк добавлять к листу, когда упёрлись в его высоту
_ROWS_CHUNK = 500


def _progress_headers() -> list[str]:
    headers = ["user_id", "username", "Статус"]
    for i in TASKS:
        headers.append(f"{i}. {TASKS[i]}")
    headers += ["Всего", "Время победы"]
    return headers


# ── Внутреннее состояние ──────────────────────────────────────────────────────

_client = None
_spreadsheet = None
_lock = asyncio.Lock()

# Кэши, которые и дают экономию запросов. Все обращения к ним идут под _lock.
_ws_cache: dict[str, object] = {}              # название листа → Worksheet
_row_cache: dict[str, dict[int, int]] = {}     # название листа → {user_id: строка}
_next_row: dict[str, int] = {}                 # название листа → куда писать новую


def is_enabled() -> bool:
    """Настроена ли синхронизация вообще."""
    return bool(
        _LIB_OK
        and GOOGLE_SPREADSHEET_ID
        and (GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE)
    )


def _col_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ── Подключение ───────────────────────────────────────────────────────────────

def _connect_sync():
    """Создаёт клиент gspread и открывает таблицу. Кидает исключение при ошибке."""
    global _client, _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet

    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
        )

    _client = gspread.authorize(creds)
    _spreadsheet = _client.open_by_key(GOOGLE_SPREADSHEET_ID)
    return _spreadsheet


def _get_ws(title: str, headers: list[str]):
    """
    Возвращает лист по названию, создавая его с шапкой если его нет.

    Объект листа кэшируется: gspread на каждый worksheet(title) делает
    отдельный запрос метаданных, а нам он нужен один раз за жизнь процесса.
    """
    ws = _ws_cache.get(title)
    if ws is not None:
        return ws

    ss = _connect_sync()
    # Один запрос отдаёт сразу все листы — кэшируем их скопом
    for existing in ss.worksheets():
        _ws_cache.setdefault(existing.title, existing)

    ws = _ws_cache.get(title)
    if ws is not None:
        return ws

    ws = ss.add_worksheet(title=title, rows=2000, cols=max(len(headers), 12))
    ws.update([headers], "A1")
    ws.format(
        f"A1:{_col_letter(len(headers))}1",
        {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
        },
    )
    ws.freeze(rows=1)
    _ws_cache[title] = ws
    logger.info("Google Sheets: создан лист «%s»", title)
    return ws


# ── Запись одним батчем ───────────────────────────────────────────────────────

def _cell(value) -> dict:
    """Ячейка для updateCells: числа числами, остальное строкой."""
    if isinstance(value, bool):
        return {"userEnteredValue": {"stringValue": str(value)}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": "" if value is None else str(value)}}


def _update_cells_request(ws, row: int, values: list, start_col: int = 1,
                          color: dict | None = None) -> dict:
    """
    Готовит один updateCells: значения и (опционально) заливка строки
    уезжают в Google за один запрос вместо двух.
    """
    cells = [_cell(v) for v in values]
    fields = "userEnteredValue"
    if color is not None:
        for cell in cells:
            cell["userEnteredFormat"] = {"backgroundColor": color}
        fields += ",userEnteredFormat.backgroundColor"

    return {
        "updateCells": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": start_col - 1,
                "endColumnIndex": start_col - 1 + len(values),
            },
            "rows": [{"values": cells}],
            "fields": fields,
        }
    }


def _prime_index(ws) -> None:
    """
    Один раз за жизнь процесса читает колонку A, чтобы знать,
    в какой строке чей user_id и куда писать новых. Дальше — только память.
    """
    if ws.title in _next_row:
        return
    column = ws.col_values(1)
    index: dict[int, int] = {}
    for i, value in enumerate(column[1:], start=2):
        value = value.strip()
        if value.isdigit():
            index[int(value)] = i
    _row_cache[ws.title] = index
    _next_row[ws.title] = len(column) + 1 if column else 2


# ── Инициализация листов ──────────────────────────────────────────────────────

def _init_sync() -> None:
    _get_ws(SHEET_REGISTRATIONS, REGISTRATION_HEADERS)
    if FEATURE_BINGO:
        _get_ws(SHEET_PROGRESS, _progress_headers())
    if FEATURE_SCREENING:
        _get_ws(SHEET_DELEGATES, _DELEGATE_HEADERS)


async def init() -> bool:
    """
    Проверяет доступ к таблице и создаёт недостающие листы.
    Возвращает True если синхронизация готова к работе.
    """
    if not is_enabled():
        logger.info("Google Sheets: синхронизация не настроена — пропускаем")
        return False
    try:
        async with _lock:
            await asyncio.to_thread(_init_sync)
        logger.info("✅ Google Sheets: подключено к таблице %s", GOOGLE_SPREADSHEET_ID)
        return True
    except Exception as exc:
        logger.error("Google Sheets: не удалось подключиться — %s", exc)
        return False


# ── Лист «Делегаты» — определение колонок ────────────────────────────────────
#
# Раньше колонки искались подстрокой, и это тихо ломало сверку: «участНИК»
# содержит «ник», поэтому колонка с ФИО принималась за колонку с ником,
# а «ТЕЛеграм» содержит «тел» и принимался за телефон. В таблице с шапкой
# ["Участник", "Телеграм", "Телефон"] бот искал ники в ФИО, а телефоны —
# в никах, и не совпадал вообще никто.
#
# Теперь три защиты:
#   1. сравнение по СЛОВАМ, а не по подстроке;
#   2. проходы в порядке приоритета — сначала ник, потом телефон, и только
#      в конце ФИО (самое общее), причём занятые колонки не переиспользуются;
#   3. если по шапке колонку не нашли — смотрим на сами данные: колонка,
#      где значения похожи на @ники или на телефоны, определяется по
#      содержимому. Это спасает при нестандартных шапках вроде «Аккаунт».

# Порядок важен: чей приоритет выше, тот и забирает колонку
_COLUMN_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("username", (
        "username", "user", "telegram", "телеграм", "телеграмм", "телега",
        "тг", "ник", "никнейм", "nickname", "nick", "юзернейм", "юзер",
        "логин", "login", "handle", "аккаунт", "account", "профиль",
    )),
    ("phone", (
        "телефон", "тел", "phone", "номер", "number", "моб", "мобильный",
        "mobile", "cell", "контакт", "contact", "whatsapp", "вотсап",
        "сотовый", "msisdn",
    )),
    ("email", ("email", "mail", "почта", "емейл", "e")),
    ("full_name", (
        "фио", "имя", "фамилия", "name", "участник", "делегат",
        "студент", "гость", "person",
    )),
]

# Слова, которые ОДНОЗНАЧНО означают телеграм-ник. Колонка с таким словом
# никогда не будет считаться телефоном, даже если «телеграм» начинается
# на «тел».
_TELEGRAM_WORDS = frozenset({
    "telegram", "телеграм", "телеграмм", "телега", "тг", "username",
    "ник", "никнейм", "юзернейм",
})


def _tokens(header: str) -> list[str]:
    """«Моб. телефон» → ['моб', 'телефон']. Пунктуация и @ отбрасываются."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in header.lower())
    return [t for t in cleaned.split() if t]


def _header_matches(header: str, keywords: tuple[str, ...]) -> bool:
    """
    Совпадение по словам: слово шапки должно НАЧИНАТЬСЯ с ключа
    (чтобы ловить «телефона», «никнейм»), а не содержать его внутри
    (чтобы «участник» не считался ником).
    """
    for token in _tokens(header):
        for key in keywords:
            if token == key or token.startswith(key):
                return True
    return False


def _looks_like_username(value: str) -> bool:
    v = value.strip().lstrip("@")
    if not (4 <= len(v) <= 32):
        return False
    return all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in v)


def _looks_like_phone(value: str) -> bool:
    digits = [ch for ch in str(value) if ch.isdigit()]
    return 10 <= len(digits) <= 15


def _sniff_column(rows: list[list[str]], taken: set[int], width: int,
                  predicate) -> int | None:
    """
    Определяет колонку по содержимому: та, где больше половины непустых
    значений проходят проверку. Нужна, когда шапка нестандартная.
    """
    best, best_score = None, 0.0
    for col in range(width):
        if col in taken:
            continue
        values = [r[col].strip() for r in rows if col < len(r) and r[col].strip()]
        if len(values) < 3:
            continue
        score = sum(1 for v in values if predicate(v)) / len(values)
        if score > 0.5 and score > best_score:
            best, best_score = col, score
    return best


def _delegate_column_indexes(headers: list[str],
                             rows: list[list[str]] | None = None) -> dict[str, int | None]:
    """
    Возвращает {'username': i, 'phone': i, 'email': i, 'full_name': i}.
    None — колонка не найдена.
    """
    found: dict[str, int | None] = {
        "username": None, "phone": None, "email": None, "full_name": None
    }
    taken: set[int] = set()

    # Колонку с телеграм-ником определяем первой и жёстко резервируем,
    # иначе её перехватит поиск телефона по слову «тел»
    telegram_cols = {
        i for i, h in enumerate(headers)
        if h not in _DELEGATE_BOT_COLUMNS
        and any(t in _TELEGRAM_WORDS for t in _tokens(h))
    }

    for field, keywords in _COLUMN_KEYWORDS:
        for i, header in enumerate(headers):
            if i in taken or header in _DELEGATE_BOT_COLUMNS:
                continue
            if field != "username" and i in telegram_cols:
                continue          # это колонка ника, чужим её не отдаём
            if _header_matches(header, keywords):
                found[field] = i
                taken.add(i)
                break

    # Шапка нестандартная — пробуем узнать колонки по данным
    if rows:
        width = max((len(r) for r in rows), default=0)
        width = max(width, len(headers))
        if found["username"] is None:
            guess = _sniff_column(rows, taken, width, _looks_like_username)
            if guess is not None:
                found["username"] = guess
                taken.add(guess)
                logger.warning(
                    "Лист «%s»: колонка с ником не опознана по шапке, "
                    "определена по содержимому — колонка %s",
                    SHEET_DELEGATES, _col_letter(guess + 1),
                )
        if found["phone"] is None:
            guess = _sniff_column(rows, taken, width, _looks_like_phone)
            if guess is not None:
                found["phone"] = guess
                taken.add(guess)
                logger.warning(
                    "Лист «%s»: колонка с телефоном не опознана по шапке, "
                    "определена по содержимому — колонка %s",
                    SHEET_DELEGATES, _col_letter(guess + 1),
                )

    return found


# Что бот в итоге распознал — показывается админам командой /delegates
LAST_DETECTION: dict[str, str] = {}


def _fetch_delegates_sync() -> list[dict]:
    """
    Читает лист «Делегаты» одним запросом. Колонки определяются по шапке,
    а при нестандартной шапке — по содержимому.

    Значения берём в UNFORMATTED виде: телефон, вписанный в таблицу как
    число (77001234567 без плюса), Google в форматированном виде отдаёт
    как «7,7E+10» — цифры терялись и сверка по телефону не срабатывала.
    """
    ws = _get_ws(SHEET_DELEGATES, _DELEGATE_HEADERS)
    try:
        raw = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    except TypeError:
        # Старые версии gspread не принимают этот аргумент
        raw = ws.get_all_values()

    # UNFORMATTED возвращает числа числами — приводим всё к строкам.
    # float 77001234567.0 превратился бы в «77001234567.0», поэтому
    # целые числа печатаем без дробной части.
    def as_text(cell_value) -> str:
        if cell_value is None:
            return ""
        if isinstance(cell_value, float) and cell_value.is_integer():
            return str(int(cell_value))
        return str(cell_value)

    values = [[as_text(c) for c in row] for row in raw]
    if not values:
        LAST_DETECTION.clear()
        LAST_DETECTION["ошибка"] = "лист пуст"
        return []

    headers = values[0]
    data_rows = values[1:]
    # Строки передаём, чтобы при нестандартной шапке колонки можно было
    # определить по содержимому
    idx = _delegate_column_indexes(headers, data_rows)

    # Запоминаем, что распозналось — админы смотрят это через /delegates
    LAST_DETECTION.clear()
    LAST_DETECTION["шапка"] = " | ".join(headers) or "(пусто)"
    for field, title in (("username", "ник"), ("phone", "телефон"),
                         ("full_name", "ФИО"), ("email", "email")):
        i = idx[field]
        LAST_DETECTION[title] = (
            f"колонка {_col_letter(i + 1)} «{headers[i]}»"
            if i is not None and i < len(headers) else "НЕ НАЙДЕНА"
        )

    if idx["username"] is None and idx["phone"] is None:
        logger.error(
            "Лист «%s»: не найдены ни колонка с ником, ни колонка с телефоном. "
            "Сверять участников не с чем! Шапка: %s",
            SHEET_DELEGATES, headers,
        )

    # Служебные колонки бота: если их нет — дописываем в шапку
    missing = [c for c in _DELEGATE_BOT_COLUMNS if c not in headers]
    if missing:
        headers = headers + missing
        ws.update([headers], f"A1:{_col_letter(len(headers))}1")

    # Позиции служебных колонок пригодятся при отметке прихода делегата
    _delegate_bot_cols_cache.clear()
    for name in _DELEGATE_BOT_COLUMNS:
        if name in headers:
            _delegate_bot_cols_cache[name] = headers.index(name) + 1
    _delegate_width_cache["width"] = len(headers)

    def cell(row: list[str], key: str) -> str:
        i = idx[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    delegates: list[dict] = []
    for row_num, row in enumerate(data_rows, start=2):
        record = {
            "row": row_num,
            "full_name": cell(row, "full_name"),
            "username": cell(row, "username"),
            "phone": cell(row, "phone"),
            "email": cell(row, "email"),
        }
        if any(record[k] for k in ("full_name", "username", "phone", "email")):
            delegates.append(record)

    matchable = sum(1 for d in delegates if d["username"] or d["phone"])
    LAST_DETECTION["строк"] = str(len(delegates))
    LAST_DETECTION["из них сверяемых"] = str(matchable)
    logger.info(
        "Лист «%s»: %s строк, из них %s с ником или телефоном",
        SHEET_DELEGATES, len(delegates), matchable,
    )
    if delegates and not matchable:
        logger.error(
            "Ни у одной строки нет ни ника, ни телефона — сверка не сработает. "
            "Проверь /delegates"
        )
    return delegates


# Позиции служебных колонок листа «Делегаты», заполняются при чтении списка
_delegate_bot_cols_cache: dict[str, int] = {}
_delegate_width_cache: dict[str, int] = {}


async def fetch_delegates() -> list[dict] | None:
    """
    Возвращает список делегатов из таблицы, либо None если Google недоступен
    (в этом случае вызывающий код должен использовать кэш из БД).
    """
    if not is_enabled():
        return None
    try:
        async with _lock:
            return await asyncio.to_thread(_fetch_delegates_sync)
    except Exception as exc:
        logger.error(
            "Google Sheets: не удалось прочитать лист «%s» — %s", SHEET_DELEGATES, exc
        )
        return None


# ── Очередь записи ────────────────────────────────────────────────────────────
# Хэндлеры не ходят в Google. Они кладут строку в память, а фоновый воркер
# раз в SHEETS_FLUSH_SECONDS отправляет всё накопленное ОДНИМ batch_update.
#
# Почему это снимает потолок: один batch_update несёт сотни updateCells и
# считается Google за ОДИН запрос. Значит расход квоты зависит только от
# частоты сброса, а не от числа участников. При сбросе раз в 5 секунд это
# 12 запросов в минуту — хоть 100 делегатов, хоть 1000.
#
# Ключ очереди — (лист, user_id), поэтому повторные /start одного человека
# схлопываются в одну запись: в таблицу уедет только последнее состояние.

# (название листа, user_id) → {"values": [...], "color": {...} | None}
_pending_rows: dict[tuple[str, int], dict] = {}
# номер строки в «Делегатах» → данные отметки
_pending_delegates: dict[int, dict] = {}
_queue_lock = asyncio.Lock()
_flush_now = asyncio.Event()
_consecutive_failures = 0


def pending_count() -> int:
    """Сколько записей ждёт отправки — для админской диагностики."""
    return len(_pending_rows) + len(_pending_delegates)


def _enqueue_row(sheet_title: str, user_id: int, values: list,
                 color: dict | None) -> None:
    _pending_rows[(sheet_title, user_id)] = {"values": values, "color": color}
    if pending_count() >= SHEETS_MAX_BATCH:
        _flush_now.set()


def queue_registration(user_id: int, username: str, full_name: str, phone: str,
                       status: str, comment: str = "", first_seen: str = "",
                       last_seen: str | None = None) -> None:
    """
    Ставит строку листа «Регистрации» в очередь. Мгновенно, без сети.

    last_seen — время последнего входа (уже в локальном формате). None
    значит «участник только что пришёл» и подставляется текущее время.
    Вызовы, которые НЕ являются визитом участника (решение админа,
    дозалив при старте), обязаны передать время из БД — иначе каждый
    перезапуск бота перезаписывал бы «Последний вход» всем участникам.
    """
    if not is_enabled():
        return
    _enqueue_row(SHEET_REGISTRATIONS, user_id, [
        str(user_id),
        f"@{username}" if username else "",
        full_name or "",
        phone or "",
        STATUS_LABELS.get(status, status),
        comment or "",
        first_seen or now_local(),
        now_local() if last_seen is None else last_seen,
    ], _STATUS_COLORS.get(status))


def queue_progress(user_id: int, username: str, status: str,
                   done: dict[int, bool], total: int,
                   ts_win: str | None = None) -> None:
    """Ставит строку листа «Прогресс» в очередь."""
    if not is_enabled() or not FEATURE_BINGO:
        return
    values = [
        str(user_id),
        f"@{username}" if username else "",
        STATUS_LABELS.get(status, status),
    ]
    for i in TASKS:
        values.append("✓" if done.get(i) else "✗")
    values += [total, to_local(ts_win)]
    _enqueue_row(SHEET_PROGRESS, user_id, values, None)


def queue_delegate_mark(row: int, status: str, user_id: int, username: str) -> None:
    """Ставит отметку прихода делегата в листе «Делегаты» в очередь."""
    if not is_enabled() or not row:
        return
    _pending_delegates[row] = {
        "status": status,
        "user_id": user_id,
        "username": username,
        "at": now_local(),
    }
    if pending_count() >= SHEETS_MAX_BATCH:
        _flush_now.set()


# ── Сборка и отправка батча ───────────────────────────────────────────────────

def _delegate_requests(ws, row: int, data: dict) -> list[dict]:
    """Запросы для одной отметки в листе «Делегаты»."""
    cols = _delegate_bot_cols_cache
    if not cols:
        headers = ws.row_values(1)
        for name in _DELEGATE_BOT_COLUMNS:
            if name in headers:
                cols[name] = headers.index(name) + 1
        _delegate_width_cache["width"] = len(headers)

    color = _STATUS_COLORS.get(data["status"])
    nick = data["username"]
    values = {
        "Статус в боте": STATUS_LABELS.get(data["status"], data["status"]),
        "TG user_id": str(data["user_id"]) + (f" (@{nick})" if nick else ""),
        "Время входа": data["at"],
    }

    requests = []
    for name, value in values.items():
        col = cols.get(name)
        if col:
            requests.append(
                _update_cells_request(ws, row, [value], start_col=col, color=color)
            )
    width = _delegate_width_cache.get("width")
    if color and width:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                    "startColumnIndex": 0, "endColumnIndex": width,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
    return requests


def _build_and_send(rows: dict, delegates: dict) -> int:
    """
    Собирает все накопленные записи в один batch_update и отправляет.
    Возвращает число отправленных строк. Выполняется в отдельном потоке.
    """
    headers_by_sheet = {
        SHEET_REGISTRATIONS: REGISTRATION_HEADERS,
        SHEET_PROGRESS: _progress_headers(),
    }
    requests: list[dict] = []
    grown: set[str] = set()

    for (title, user_id), item in rows.items():
        ws = _get_ws(title, headers_by_sheet.get(title, REGISTRATION_HEADERS))
        _prime_index(ws)
        index = _row_cache.setdefault(title, {})

        row = index.get(user_id)
        if row is None:
            row = _next_row[title]
            index[user_id] = row
            _next_row[title] = row + 1

        # Высоту листа увеличиваем один раз на батч, а не на каждую строку
        if row > ws.row_count and title not in grown:
            need = max(_ROWS_CHUNK, _next_row[title] + _ROWS_CHUNK - ws.row_count)
            requests.append({
                "appendDimension": {
                    "sheetId": ws.id, "dimension": "ROWS", "length": need,
                }
            })
            ws._properties["gridProperties"]["rowCount"] = ws.row_count + need
            grown.add(title)

        requests.append(
            _update_cells_request(ws, row, item["values"], color=item["color"])
        )

    if delegates:
        ws = _get_ws(SHEET_DELEGATES, _DELEGATE_HEADERS)
        for row, data in delegates.items():
            requests.extend(_delegate_requests(ws, row, data))

    if requests:
        _connect_sync().batch_update({"requests": requests})
    return len(rows) + len(delegates)


async def flush() -> int:
    """
    Отправляет всё накопленное. Возвращает число записанных строк.

    При ошибке записи возвращает строки в очередь: данные не теряются,
    следующий сброс попробует снова. Уже поставленные новые записи по тому
    же участнику имеют приоритет — вернувшаяся старая их не затрёт.
    """
    global _consecutive_failures

    async with _queue_lock:
        rows = dict(_pending_rows)
        delegates = dict(_pending_delegates)
        _pending_rows.clear()
        _pending_delegates.clear()

    if not rows and not delegates:
        return 0

    try:
        async with _lock:
            sent = await asyncio.to_thread(_build_and_send, rows, delegates)
        if _consecutive_failures:
            logger.info("Google Sheets: запись восстановилась после %s ошибок подряд",
                        _consecutive_failures)
        _consecutive_failures = 0
        return sent
    except Exception as exc:
        _consecutive_failures += 1
        logger.error(
            "Google Sheets: батч из %s строк не записан (ошибка №%s) — %s",
            len(rows) + len(delegates), _consecutive_failures, exc,
        )
        async with _queue_lock:
            for key, item in rows.items():
                _pending_rows.setdefault(key, item)
            for row, item in delegates.items():
                _pending_delegates.setdefault(row, item)
        return 0


async def flush_worker() -> None:
    """
    Фоновый воркер: сбрасывает очередь по таймеру или досрочно,
    когда накопилось SHEETS_MAX_BATCH записей.

    При повторяющихся ошибках увеличивает паузу, чтобы не долбить Google
    и не сжигать квоту впустую.
    """
    while True:
        delay = SHEETS_FLUSH_SECONDS * min(2 ** _consecutive_failures, 12)
        try:
            await asyncio.wait_for(_flush_now.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        _flush_now.clear()
        try:
            await flush()
        except Exception as exc:
            logger.error("Google Sheets: воркер очереди упал — %s", exc)


# ── Очистка листов бота ──────────────────────────────────────────────────────

def _clear_sheet_sync(title: str, headers: list[str]) -> int:
    """
    Стирает все строки листа НИЖЕ шапки. Сама шапка остаётся.
    Возвращает, сколько строк было удалено.
    """
    ws = _get_ws(title, headers)
    used = len(ws.col_values(1))
    if used <= 1:
        return 0

    _connect_sync().batch_update({"requests": [{
        "updateCells": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": 1,          # шапку не трогаем
                "endRowIndex": used,
            },
            "fields": "userEnteredValue,userEnteredFormat",
        }
    }]})
    return used - 1


async def clear_bot_sheets(titles: list[str] | None = None) -> dict[str, int]:
    """
    Очищает листы, которые ведёт БОТ: «Регистрации» и/или «Прогресс».
    titles=None — оба листа.

    Лист «Делегаты» здесь не трогается ни при каких условиях — его
    заполняют организаторы вручную. Отметки бота в нём стирает
    clear_delegate_marks().
    """
    if not is_enabled():
        return {}

    headers_by_title = {SHEET_REGISTRATIONS: REGISTRATION_HEADERS}
    if FEATURE_BINGO:
        headers_by_title[SHEET_PROGRESS] = _progress_headers()
    result: dict[str, int] = {}
    for title in titles or list(headers_by_title):
        if title not in headers_by_title:
            continue
        headers = headers_by_title[title]
        try:
            async with _lock:
                result[title] = await asyncio.to_thread(
                    _clear_sheet_sync, title, headers
                )
        except Exception as exc:
            logger.error("Не удалось очистить лист «%s» — %s", title, exc)
            result[title] = -1

    reset_caches()
    return result


def _clear_delegate_marks_sync(rows: list[int]) -> int:
    """
    Стирает служебные колонки бота в листе «Делегаты» по всей высоте листа
    и снимает заливку со строк, которые бот красил. Данные организаторов
    (ФИО, ник, телефон и прочие их колонки) не трогаются.
    """
    ws = _get_ws(SHEET_DELEGATES, _DELEGATE_HEADERS)
    headers = ws.row_values(1)
    requests = []
    for name in _DELEGATE_BOT_COLUMNS:
        if name not in headers:
            continue
        col = headers.index(name)
        requests.append({"updateCells": {
            "range": {"sheetId": ws.id, "startRowIndex": 1,
                      "endRowIndex": ws.row_count,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "fields": "userEnteredValue,userEnteredFormat.backgroundColor",
        }})
    for row in sorted(set(r for r in rows if r and r > 1)):
        requests.append({"updateCells": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": 0,
                      "endColumnIndex": len(headers)},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    if requests:
        _connect_sync().batch_update({"requests": requests})
    return len(set(rows))


async def clear_delegate_marks(rows: list[int]) -> int | None:
    """
    Убирает из листа «Делегаты» отметки прихода, которые ставил бот.
    Возвращает число очищенных строк или None при ошибке Google.
    """
    if not is_enabled() or not FEATURE_SCREENING:
        return 0
    try:
        async with _lock:
            return await asyncio.to_thread(_clear_delegate_marks_sync, rows)
    except Exception as exc:
        logger.error("Не удалось очистить отметки в листе «%s» — %s",
                     SHEET_DELEGATES, exc)
        return None


def drop_pending(titles: list[str] | None = None,
                 delegate_marks: bool = True) -> int:
    """
    Выбрасывает то, что ждёт записи в таблицу. Возвращает сколько.
    titles=None — строки всех листов; delegate_marks — отметки в «Делегатах».
    """
    dropped = 0
    for key in list(_pending_rows):
        if titles is None or key[0] in titles:
            del _pending_rows[key]
            dropped += 1
    if delegate_marks:
        dropped += len(_pending_delegates)
        _pending_delegates.clear()
    return dropped


# ── Восстановление из таблицы в базу ─────────────────────────────────────────
# Обратное направление синхронизации. Нужно на случай, если PostgreSQL
# потеряется или пересоздастся во время ивента: без этого делегатам пришлось
# бы проходить сверку заново, а очередь на стойке регистрации выросла бы
# второй раз. Google-таблица переживает падение базы, поэтому статусы
# восстанавливаются из неё.

def _read_sheet_sync(title: str, headers: list[str]) -> list[dict]:
    """Читает лист целиком и отдаёт список словарей по шапке. Один запрос."""
    ws = _get_ws(title, headers)
    values = ws.get_all_values()
    if len(values) < 2:
        return []

    head = values[0]
    rows = []
    for raw in values[1:]:
        if not raw or not raw[0].strip().isdigit():
            continue
        rows.append({
            head[i]: (raw[i].strip() if i < len(raw) else "")
            for i in range(len(head))
        })
    return rows


def _label_to_status(label: str) -> str:
    """«✅ подтверждён вручную» → manual. Обратное к STATUS_LABELS."""
    for status, text in STATUS_LABELS.items():
        if label.strip() == text:
            return status
    return STATUS_PENDING


async def read_registrations() -> list[dict] | None:
    """
    Читает лист «Регистрации» для восстановления базы.
    None — если Google недоступен.
    """
    if not is_enabled():
        return None
    try:
        async with _lock:
            raw = await asyncio.to_thread(
                _read_sheet_sync, SHEET_REGISTRATIONS, REGISTRATION_HEADERS
            )
    except Exception as exc:
        logger.error("Google Sheets: не удалось прочитать «Регистрации» — %s", exc)
        return None

    result = []
    for row in raw:
        result.append({
            "user_id": int(row.get("user_id", 0) or 0),
            "username": (row.get("username", "") or "").lstrip("@"),
            "full_name": row.get("Имя в Telegram", ""),
            "phone": row.get("Телефон", ""),
            "status": _label_to_status(row.get("Статус", "")),
            "status_comment": row.get("Комментарий", ""),
            # В таблице время локальное, в БД — ISO в UTC
            "first_seen": local_to_iso(row.get("Первая регистрация", "")),
            "last_seen": local_to_iso(row.get("Последний вход", "")),
        })
    return [r for r in result if r["user_id"]]


async def read_progress() -> list[dict] | None:
    """Читает лист «Прогресс» — какие задания у кого зачтены."""
    if not is_enabled() or not FEATURE_BINGO:
        return None
    try:
        async with _lock:
            raw = await asyncio.to_thread(
                _read_sheet_sync, SHEET_PROGRESS, _progress_headers()
            )
    except Exception as exc:
        logger.error("Google Sheets: не удалось прочитать «Прогресс» — %s", exc)
        return None

    result = []
    for row in raw:
        # Колонка ищется по имени, а не по позиции: номера заданий
        # не обязаны идти подряд с единицы
        done = {i: row.get(f"{i}. {TASKS[i]}", "") == "✓" for i in TASKS}
        result.append({
            "user_id": int(row.get("user_id", 0) or 0),
            "done": done,
            "ts_win": local_to_iso(row.get("Время победы", "")),
        })
    return [r for r in result if r["user_id"]]


def reset_caches() -> None:
    """
    Сбрасывает кэши номеров строк. Нужно после ручной правки таблицы
    организаторами — иначе бот будет писать по устаревшим номерам строк.
    """
    _row_cache.clear()
    _next_row.clear()
    _delegate_bot_cols_cache.clear()
    _delegate_width_cache.clear()
    logger.info("Google Sheets: кэши строк сброшены, номера перечитаются заново")
