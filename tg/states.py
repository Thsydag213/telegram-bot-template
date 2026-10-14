"""
tg/states.py — состояния диалогов (FSM). Хранятся в PostgreSQL
(db/fsm_storage.py), поэтому переживают перезапуск бота.
"""

from aiogram.fsm.state import State, StatesGroup


class Screening(StatesGroup):
    """Участник: бот ждёт контакт для сверки по телефону."""
    waiting_for_contact = State()


class ProofUpload(StatesGroup):
    """Участник: выбрал задание, бот ждёт пруф. В данных — task_id."""
    waiting_for_proof = State()


class AgendaEdit(StatesGroup):
    """Админ: бот ждёт новую программу (текст или фото с подписью)."""
    waiting_for_content = State()


class TaskUpload(StatesGroup):
    """Админ, /upload_task: номер → фото-образец → описание."""
    waiting_for_task_number = State()
    waiting_for_photo = State()
    waiting_for_description = State()


class TaskDescEdit(StatesGroup):
    """Админ, /edit_desc: номер → новое описание."""
    waiting_for_task_number = State()
    waiting_for_description = State()


class ResetConfirm(StatesGroup):
    """Админ: любой из трёх сбросов ждёт кодовое слово. Какой — в данных."""
    waiting_for_word = State()
