"""отправка в Telegram того, что готовит фоновый цикл: напоминания и утренняя сводка"""
from __future__ import annotations

import logging
from datetime import date, datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..domain import Entry, Item, ItemType
from .keyboards import ItemCb
from .render import agenda

log = logging.getLogger(__name__)


class TelegramNotifier:
    """напоминание с кнопками "Готово" и "+15 мин", и сводка на день"""

    def __init__(self, bot: Bot, clock):
        self._bot = bot
        self._clock = clock

    async def send_reminder(self, item: Item, occurrence: datetime, late: bool) -> None:
        title = escape(item.title)
        if item.type is ItemType.MEETING and item.has_time:
            minutes = max(0, round((occurrence - self._clock()).total_seconds() / 60))
            soon = f"через {minutes} мин" if minutes else "сейчас"
            text = f"⏰ <b>{title}</b>\n{soon}, в {occurrence:%H:%M}"
        elif item.has_time:
            text = f"⏰ Напоминаю: <b>{title}</b>"
        else:
            text = f"📌 Сегодня срок: <b>{title}</b>"
        if late:
            # бот был выключен, когда надо было напомнить
            text = f"Пропущенное напоминание ({occurrence:%d.%m %H:%M})\n" + text

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data=ItemCb(action="done", item_id=item.id).pack()),
            InlineKeyboardButton(text="💤 +15 мин", callback_data=ItemCb(action="snooze", item_id=item.id).pack()),
        ]])
        await self._send(item.user_id, text, keyboard)

    async def send_digest(self, user_id: int, entries: list[Entry], today: date) -> None:
        await self._send(user_id, "☀️ <b>План на сегодня</b>\n\n" + agenda(entries, today))

    async def _send(self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=keyboard)
        except TelegramForbiddenError:
            # пользователь заблокировал бота, повторять отправку нет смысла
            log.info("Chat %s is not reachable, skipping", chat_id)
