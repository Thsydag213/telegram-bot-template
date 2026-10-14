"""
services/timefmt.py — перевод времени между базой и людьми.

В базе время хранится в UTC (ISO-строки или TIMESTAMPTZ). В таблице и
выгрузках — в часовом поясе ивента (TIMEZONE_OFFSET), формат
«ГГГГ-ММ-ДД ЧЧ:ММ:СС».
"""

from datetime import datetime, timedelta, timezone

from config import TIMEZONE_OFFSET

LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S"


def event_tz() -> timezone:
    return timezone(timedelta(hours=TIMEZONE_OFFSET))


def now_local() -> str:
    return datetime.now(event_tz()).strftime(LOCAL_FORMAT)


def to_local(value: str | datetime | None) -> str:
    """UTC из базы (ISO-строка или datetime) → локальное время. Пусто — ""."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(event_tz()).strftime(LOCAL_FORMAT)


def local_to_iso(local_ts: str | None) -> str:
    """
    Локальное время из таблицы → ISO в UTC для базы. Пусто или мусор — "".
    Обратное к to_local: восстановление из таблицы не сдвигает время.
    """
    if not local_ts:
        return ""
    try:
        dt = datetime.fromisoformat(local_ts.strip())
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=event_tz())
    return dt.astimezone(timezone.utc).isoformat()
