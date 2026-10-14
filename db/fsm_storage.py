"""
db/fsm_storage.py — хранилище состояний диалогов (FSM) в PostgreSQL.

С хранилищем в памяти перезапуск бота сбрасывал всех, кто был посреди
диалога: участник нажимал задание, бот перезапускался, и фото уходило
в никуда. Здесь состояние лежит в таблице fsm_states и переживает
перезапуск. Отдельный Redis для этого не нужен.

В данных состояния должны лежать только JSON-совместимые значения
(числа, строки, списки, словари) — так и есть во всех хендлерах бота.
"""

import json
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey

from db.pool import get_pool


class PostgresStorage(BaseStorage):
    def __init__(self) -> None:
        self._keys = DefaultKeyBuilder(with_destiny=True)

    def _key(self, key: StorageKey) -> str:
        return self._keys.build(key)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fsm_states (key, state) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
                """,
                self._key(key), value)
            # Пустая запись (ни состояния, ни данных) не нужна
            await conn.execute(
                "DELETE FROM fsm_states WHERE key = $1 AND state IS NULL AND data = '{}'::jsonb",
                self._key(key))

    async def get_state(self, key: StorageKey) -> str | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT state FROM fsm_states WHERE key = $1", self._key(key))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(data), ensure_ascii=False)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fsm_states (key, data) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                """,
                self._key(key), payload)
            await conn.execute(
                "DELETE FROM fsm_states WHERE key = $1 AND state IS NULL AND data = '{}'::jsonb",
                self._key(key))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT data FROM fsm_states WHERE key = $1", self._key(key))
        if raw is None:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    async def close(self) -> None:
        # Пулом владеет бот целиком (db.pool.close_pool), здесь закрывать нечего
        pass
