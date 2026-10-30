"""прослойка перед всеми обработчиками: регистрирует пользователя и пускает его сообщения строго по одному"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from aiogram import BaseMiddleware

from ..storage import Storage


class UserMiddleware(BaseMiddleware):
    """кладет в обработчики объект user и держит очередь сообщений на каждого пользователя"""

    def __init__(self, db: Storage):
        self._db = db
        # состояние диалога читается и пишется на каждом сообщении. если два сообщения одного
        # человека обработать одновременно, одно затрет другое, поэтому по очереди
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __call__(self, handler, event, data):
        sender = data.get("event_from_user")
        if sender is None:
            return await handler(event, data)
        data["user"] = self._db.upsert_user(sender.id, sender.full_name)
        async with self._locks[sender.id]:
            return await handler(event, data)
