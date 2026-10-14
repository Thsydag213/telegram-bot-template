"""Чистая логика без базы и Telegram."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from db.delegates import normalize_phone, normalize_username
from services import admin_queue, sheets
from services.timefmt import local_to_iso, to_local

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("raw", ["@Ivan_K", "ivan_k", "https://t.me/Ivan_K", " t.me/ivan_k/ "])
def test_normalize_username(raw):
    assert normalize_username(raw) == "ivan_k"


@pytest.mark.parametrize("raw", ["+7 (700) 123-45-67", "87001234567", "77001234567", 77001234567])
def test_normalize_phone(raw):
    assert normalize_phone(raw) == "7001234567"


def test_time_roundtrip():
    assert to_local(local_to_iso("2026-08-31 12:00:00")) == "2026-08-31 12:00:00"
    assert local_to_iso("мусор") == "" and to_local(None) == ""


def test_delegate_columns_by_words_not_substrings():
    """«Участник» содержит «ник», «Телеграм» начинается на «тел» — не путаем."""
    idx = sheets._delegate_column_indexes(["Участник", "Телеграм", "Телефон", "Почта"])
    assert idx == {"full_name": 0, "username": 1, "phone": 2, "email": 3}


def test_delegate_columns_sniffed_from_data():
    rows = [["Иван", "ivan_k", "87001112233"], ["Анна", "anna_a", "87002223344"],
            ["Пётр", "petr_p", "87003334455"]]
    idx = sheets._delegate_column_indexes(["ФИО", "Аккаунт?", "Связь"], rows)
    assert idx["username"] == 1 and idx["phone"] == 2


def test_selective_queue_drops():
    sheets._pending_rows.update({("A", 1): {}, ("B", 1): {}, ("B", 2): {}})
    sheets._pending_delegates[5] = {}
    assert sheets.drop_pending(["B"], delegate_marks=False) == 2
    assert sheets.drop_pending() == 2 and not sheets._pending_delegates

    send = lambda: None  # noqa: E731
    admin_queue.enqueue_send(send, tag=admin_queue.TAG_PROOF)
    admin_queue.enqueue_send(send, tag=admin_queue.TAG_SCREENING)
    admin_queue.enqueue_send(send)
    assert admin_queue.drop_pending(admin_queue.TAG_PROOF) == 1
    assert [i["tag"] for i in admin_queue._queue] == [admin_queue.TAG_SCREENING, ""]
    assert admin_queue.drop_pending() == 2


def _bot_layout(**flags) -> dict:
    """Собирает бота в отдельном процессе с заданными флагами (config читается при импорте)."""
    code = (
        "import json, bot; from tg.keyboards import main_menu_keyboard as m; k = m();"
        "print(json.dumps({'routers': [r.name for r in bot.build_dispatcher().sub_routers],"
        "'menu': [b.text for row in (k.inline_keyboard if k else []) for b in row]}))"
    )
    env = {**os.environ, **{k: v for k, v in flags.items()}}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_all_modules_enabled():
    layout = _bot_layout(FEATURE_SCREENING="1", FEATURE_AGENDA="1", FEATURE_BINGO="1")
    assert layout["routers"] == ["admin", "screening_admin", "review", "tasks_admin",
                                 "agenda", "start", "bingo", "fallback"]


def test_disabled_modules_not_registered():
    layout = _bot_layout(FEATURE_SCREENING="0", FEATURE_AGENDA="1", FEATURE_BINGO="0",
                         CHANNEL_URL="")
    assert layout["routers"] == ["admin", "agenda", "start", "fallback"]
    assert layout["menu"] == ["📅 Программа"]
