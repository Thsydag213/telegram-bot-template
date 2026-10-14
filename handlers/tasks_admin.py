"""
handlers/tasks_admin.py — управление заданиями бинго (модуль FEATURE_BINGO).

  /upload_task   — фото-образец и описание задания
  /edit_desc     — только описание
  /list_tasks    — все задания: тип сдачи, есть ли фото-образец
  /reseed_tasks  — подтянуть названия и описания из content/tasks.py
                   (ручные правки описаний сохраняются; force — перезаписать)

Во всех диалогах /cancel выходит без изменений.
"""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config import TASKS
from db.game import (
    get_all_tasks,
    get_task_by_number,
    reseed_tasks,
    update_task_description,
    update_task_reference_photo,
)
from tg.filters import IsAdmin
from tg.states import TaskDescEdit, TaskUpload

router = Router(name="tasks_admin")

_CANCEL_HINT = "Для отмены: /cancel"


def _numbers_hint() -> str:
    return ", ".join(str(n) for n in TASKS)


async def _read_task_number(message: Message):
    """Номер задания из сообщения или None (с подсказкой админу)."""
    text = (message.text or "").strip()
    if text.isdigit() and int(text) in TASKS:
        task = await get_task_by_number(int(text))
        if task:
            return task
    await message.answer(f"❌ Нет такого задания. Номера: {_numbers_hint()}\n{_CANCEL_HINT}")
    return None


# ── Отмена любого из диалогов ────────────────────────────────────────────────

@router.message(TaskUpload.waiting_for_task_number, Command("cancel"))
@router.message(TaskUpload.waiting_for_photo, Command("cancel"))
@router.message(TaskUpload.waiting_for_description, Command("cancel"))
@router.message(TaskDescEdit.waiting_for_task_number, Command("cancel"))
@router.message(TaskDescEdit.waiting_for_description, Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("↩️ Отменено. Задание не изменено.")


# ── /upload_task ──────────────────────────────────────────────────────────────

@router.message(Command("upload_task"), IsAdmin())
async def upload_start(message: Message, state: FSMContext) -> None:
    await state.set_state(TaskUpload.waiting_for_task_number)
    await message.answer(f"📝 Номер задания ({_numbers_hint()}):\n\n{_CANCEL_HINT}")


@router.message(TaskUpload.waiting_for_task_number)
async def upload_number(message: Message, state: FSMContext) -> None:
    task = await _read_task_number(message)
    if not task:
        return
    await state.update_data(task_number=task["task_number"])
    await state.set_state(TaskUpload.waiting_for_photo)
    await message.answer(
        f"📸 Пришли <b>фото-образец</b> для задания #{task['task_number']} "
        f"«{html.escape(task['name'])}».\n\n"
        f"Оставить текущее фото: /skip\n{_CANCEL_HINT}")


@router.message(TaskUpload.waiting_for_photo, F.photo)
async def upload_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await state.set_state(TaskUpload.waiting_for_description)
    await message.answer(f"✅ Фото принято. Теперь пришли <b>описание</b> задания.\n\n"
                         f"Оставить текущее описание: /skip\n{_CANCEL_HINT}")


@router.message(TaskUpload.waiting_for_photo, Command("skip"))
async def upload_skip_photo(message: Message, state: FSMContext) -> None:
    await state.set_state(TaskUpload.waiting_for_description)
    await message.answer(f"Фото не меняем. Пришли <b>описание</b> задания.\n\n"
                         f"Оставить текущее описание: /skip\n{_CANCEL_HINT}")


@router.message(TaskUpload.waiting_for_photo)
async def upload_photo_wrong(message: Message) -> None:
    await message.answer(f"⚠️ Нужно именно <b>фото</b>. Оставить текущее: /skip\n{_CANCEL_HINT}")


@router.message(TaskUpload.waiting_for_description, Command("skip"))
@router.message(TaskUpload.waiting_for_description, F.text)
async def upload_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    number, photo = data["task_number"], data.get("photo_file_id")
    skip = (message.text or "").strip() == "/skip"

    if photo:
        await update_task_reference_photo(number, photo)
    if not skip:
        await update_task_description(number, message.html_text)
    await message.answer(
        f"✅ Задание #{number} обновлено.\n"
        f"• Фото: {'новое' if photo else 'без изменений'}\n"
        f"• Описание: {'без изменений' if skip else 'новое'}")


@router.message(TaskUpload.waiting_for_description)
async def upload_description_wrong(message: Message) -> None:
    await message.answer(f"⚠️ Пришли описание текстом. Оставить текущее: /skip\n{_CANCEL_HINT}")


# ── /edit_desc ────────────────────────────────────────────────────────────────

@router.message(Command("edit_desc"), IsAdmin())
async def edit_start(message: Message, state: FSMContext) -> None:
    await state.set_state(TaskDescEdit.waiting_for_task_number)
    await message.answer(f"✏️ Номер задания ({_numbers_hint()}):\n\n{_CANCEL_HINT}")


@router.message(TaskDescEdit.waiting_for_task_number)
async def edit_number(message: Message, state: FSMContext) -> None:
    task = await _read_task_number(message)
    if not task:
        return
    await state.update_data(task_number=task["task_number"])
    await state.set_state(TaskDescEdit.waiting_for_description)
    await message.answer(
        f"Текущее описание #{task['task_number']} «{html.escape(task['name'])}»:\n\n"
        f"{task['description']}\n\nПришли новое.\n{_CANCEL_HINT}")


@router.message(TaskDescEdit.waiting_for_description, F.text)
async def edit_text(message: Message, state: FSMContext) -> None:
    number = (await state.get_data())["task_number"]
    await state.clear()
    await update_task_description(number, message.html_text)
    await message.answer(f"✅ Описание задания #{number} обновлено.")


@router.message(TaskDescEdit.waiting_for_description)
async def edit_text_wrong(message: Message) -> None:
    await message.answer(f"⚠️ Пришли описание текстом.\n{_CANCEL_HINT}")


# ── /list_tasks, /reseed_tasks ───────────────────────────────────────────────

@router.message(Command("list_tasks"), IsAdmin())
async def list_tasks(message: Message) -> None:
    tasks = await get_all_tasks()
    if not tasks:
        await message.answer("📋 В базе нет заданий. Запусти /reseed_tasks.")
        return
    lines = ["📋 <b>Задания:</b>\n"]
    for t in tasks:
        lines.append(
            f"<b>#{t['task_number']}</b> {html.escape(t['name'])}\n"
            f"  тип: <code>{t['submission_type']}</code> | "
            f"фото-образец: {'✅' if t['reference_photo_url'] else '—'} | "
            f"описание: {'✏️ изменено вручную' if t['description_custom'] else 'из кода'}")
    await message.answer("\n".join(lines))


@router.message(Command("reseed_tasks"), IsAdmin())
async def cmd_reseed(message: Message) -> None:
    force = (message.text or "").split()[1:2] == ["force"]
    updated, inserted, kept = await reseed_tasks(force=force)
    lines = ["✅ Задания синхронизированы с content/tasks.py.",
             f"• Обновлено: <b>{updated}</b>",
             f"• Добавлено: <b>{inserted}</b>",
             "• Фото-образцы сохранены"]
    if kept:
        lines += ["",
                  f"✏️ Описания {', '.join(f'#{n}' for n in kept)} меняли вручную — "
                  f"они оставлены как есть.",
                  "Перезаписать их версией из кода: <code>/reseed_tasks force</code>"]
    lines += ["", "Проверь: /list_tasks"]
    await message.answer("\n".join(lines))
