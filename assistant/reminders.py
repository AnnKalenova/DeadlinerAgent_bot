"""фоновый цикл: отправляет напоминания, когда подошло время, и утреннюю сводку

все хранится в базе (remind_at, notified), поэтому после перезапуска ничего не теряется:
то, что пропустили пока бот лежал, уйдет на первом же проходе
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Protocol

from . import schedule
from .domain import Entry, Item
from .service import Assistant
from .storage import Storage

log = logging.getLogger(__name__)

LATE_AFTER = timedelta(minutes=10)  # если опоздали больше чем на 10 мин, пишем "пропущенное напоминание"
DIGEST_WINDOW = timedelta(hours=3)  # сводка уходит с DIGEST_TIME и еще 3 часа, если бот был выключен


class Notifier(Protocol):
    """куда отправлять. в боте это телега (bot/notifier.py)"""

    async def send_reminder(self, item: Item, occurrence: datetime, late: bool) -> None: ...

    async def send_digest(self, user_id: int, entries: list[Entry], today: date) -> None: ...


class ReminderService:
    """раз в interval секунд смотрит в базу и отправляет то, что пора"""

    def __init__(self, storage: Storage, assistant: Assistant, notifier: Notifier, clock, *,
                 meeting_lead: int, digest_time: time, interval: float = 30):
        self._storage = storage
        self._assistant = assistant
        self._notifier = notifier
        self._clock = clock
        self._lead = meeting_lead
        self._digest_time = digest_time
        self._interval = interval

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # что бы ни сломалось, цикл не падает, просто пишем в лог и идем на следующий круг
                log.exception("Reminder tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        """один проход: все напоминания, у которых наступило время, и утренние сводки"""
        now = self._clock()
        for item in self._storage.due_reminders(now):
            occurrence = schedule.occurrence_for_alert(item, item.remind_at, self._lead)
            try:
                await self._notifier.send_reminder(item, occurrence, late=now - item.remind_at > LATE_AFTER)
            except Exception:
                log.exception("Could not send reminder for item %s, will retry", item.id)
                continue  # notified не ставим, попробуем на следующем круге
            # у повторяющейся записи сразу ставим следующее напоминание
            if item.repeat:
                item.remind_at = schedule.reminder_time(item, now, self._lead)
                item.notified = item.remind_at is None
            else:
                item.notified = True
            self._storage.save_item(item)
        await self._send_digests(now)

    async def _send_digests(self, now: datetime) -> None:
        today = now.date()
        start = datetime.combine(today, self._digest_time)
        if not start <= now < start + DIGEST_WINDOW:
            return
        for user in self._storage.digest_users():
            if user.last_digest == today:
                continue  # сегодня уже отправляли
            entries = self._assistant.day_entries(user.user_id, today)
            try:
                if entries:  # если на сегодня пусто, ничего не шлем
                    await self._notifier.send_digest(user.user_id, entries, today)
            except Exception:
                log.exception("Could not send digest to %s", user.user_id)
                continue
            self._storage.update_user(user.user_id, last_digest=today)
