"""Состояния диалогов в PostgreSQL переживают перезапуск."""

from aiogram.fsm.storage.base import StorageKey

from db.fsm_storage import PostgresStorage
from tg.states import ProofUpload

KEY = StorageKey(bot_id=1, chat_id=10, user_id=10)


async def test_state_and_data_survive_new_storage(db):
    await PostgresStorage().set_state(KEY, ProofUpload.waiting_for_proof)
    await PostgresStorage().update_data(KEY, {"task_id": 3})

    # Новый экземпляр — как после перезапуска бота
    storage = PostgresStorage()
    assert await storage.get_state(KEY) == ProofUpload.waiting_for_proof.state
    assert await storage.get_data(KEY) == {"task_id": 3}


async def test_clear_removes_row(db):
    storage = PostgresStorage()
    await storage.set_state(KEY, "some:state")
    await storage.set_data(KEY, {"a": 1})
    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM fsm_states") == 0
    assert await storage.get_state(KEY) is None and await storage.get_data(KEY) == {}


async def test_keys_are_isolated(db):
    storage = PostgresStorage()
    other = StorageKey(bot_id=1, chat_id=11, user_id=11)
    await storage.set_data(KEY, {"x": "мой"})
    assert await storage.get_data(other) == {}
