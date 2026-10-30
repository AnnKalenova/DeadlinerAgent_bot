"""отрисовка ответа ассистента для Telegram: html-текст и кнопки под ним, а также текст для озвучки"""
from __future__ import annotations

import re
from datetime import date
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..dates import day_header, human_datetime
from ..domain import Browse, Entry, Item, Reply
from ..texts import REPEAT_NAMES, TYPE_ICONS
from .keyboards import VIEWS, AnswerCb, BrowseCb, ItemCb

MAX_LINES = 60  # в списке больше 60 строк не показываем, дальше "...и еще N"
MAX_LENGTH = 4000  # Telegram не принимает сообщения длиннее 4096 символов
MAX_TITLE = 300  # длинное название в сообщении сокращаем, в базе оно хранится целиком
_TAG = re.compile(r"<(/?)(b|i|s|code)>")


def render(reply: Reply, today: date) -> tuple[str, InlineKeyboardMarkup | None]:
    """собирает текст и кнопки из всех частей ответа: карточка, список, варианты и т.д."""
    is_agenda = reply.entries is not None
    parts = [f"<b>{escape(reply.text)}</b>" if is_agenda and reply.entries else escape(reply.text)]
    keyboard = InlineKeyboardBuilder()

    if reply.entries:
        parts.append(agenda(reply.entries, today))
    if reply.card:
        parts.append(card(reply.card, today))
        keyboard.row(*_card_buttons(reply.card))
    if reply.candidates:
        parts.append("\n".join(
            f"{n}. {_item_line(item, today)}" for n, item in enumerate(reply.candidates, 1)
        ))
        if reply.question:
            # бот спросил "какую из них?": кнопка с номером продолжает исходную команду (удалить, перенести...)
            buttons = [_answer_button(str(n), reply.question, str(n)) for n in range(1, len(reply.candidates) + 1)]
        else:
            # обычный поиск: кнопка с номером просто открывает запись
            buttons = [InlineKeyboardButton(text=str(n), callback_data=ItemCb(action="open", item_id=item.id).pack())
                       for n, item in enumerate(reply.candidates, 1)]
        keyboard.row(*buttons)
    if reply.warning:
        parts.append(f"⚠️ {escape(reply.warning)}")
    # кнопки-ответы привязаны к метке вопроса: если вопрос уже другой, старая кнопка не сработает
    if reply.time_options and reply.question:
        keyboard.row(*(_answer_button(value, reply.question, value.replace(":", "")) for value in reply.time_options))
    if reply.confirm_delete and reply.question:
        keyboard.row(_answer_button("🗑 Да, удалить", reply.question, "yes"), _answer_button("Отмена", reply.question, "no"))
    if reply.browse:
        for row in _browse_rows(reply.browse):
            keyboard.row(*row)

    markup = keyboard.as_markup()
    return fit("\n\n".join(parts)), markup if markup.inline_keyboard else None


def fit(text: str, limit: int = MAX_LENGTH) -> str:
    """обрезает сообщение под лимит Telegram и не ломает html. нужна для любого сообщения бота,
    в том числе для утренней сводки: длинное сообщение Telegram просто не принимает"""
    if len(text) <= limit:
        return text
    cut = text[:limit - 20]  # запас на закрывающие теги и "…"
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]  # по концу строки: каждая строка сама по себе цельная
    else:
        cut = re.sub(r"<[^>]*$|&[a-z#0-9]*$", "", cut)  # одна длинная строка: убираем недописанный тег или &amp;
    opened: list[str] = []
    for closing, tag in _TAG.findall(cut):
        if not closing:
            opened.append(tag)
        elif opened and opened[-1] == tag:
            opened.pop()
    return cut + "".join(f"</{tag}>" for tag in reversed(opened)) + "\n…"


def short_title(title: str) -> str:
    """название для показа: очень длинное сокращаем, чтобы оно не вытеснило из сообщения все остальное"""
    return title if len(title) <= MAX_TITLE else title[:MAX_TITLE].rstrip() + "…"


def _answer_button(text: str, token: str, value: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AnswerCb(token=token, value=value).pack())


def agenda(entries: list[Entry], today: date) -> str:
    """записи по дням с заголовком на каждый день. без даты идут в конце"""
    lines: list[str] = []
    current: date | None | str = "start"  # "start" - чтобы первый же день точно получил заголовок
    for entry in entries[:MAX_LINES]:
        day = entry.start.date() if entry.start else None
        if day != current:
            if lines:
                lines.append("")
            lines.append(f"<b>{day_header(day, today)}</b>" if day else "<b>Без срока</b>")
            current = day
        lines.append(_entry_line(entry))
    if len(entries) > MAX_LINES:
        lines.append(f"\n…и ещё {len(entries) - MAX_LINES}")
    return "\n".join(lines)


def card(item: Item, today: date) -> str:
    """карточка одной записи: тип, название, когда, повтор, выполнена или нет"""
    when = human_datetime(item.start, item.has_time, today)
    if item.end and item.has_time and item.start and item.end.date() == item.start.date():
        when += f"–{item.end:%H:%M}"
    lines = [f"{TYPE_ICONS[item.type]} <b>{escape(short_title(item.title))}</b>", f"📅 {when}"]
    if item.repeat:
        lines.append(f"🔁 {REPEAT_NAMES[item.repeat]}")
    if item.is_done:
        lines.append("✅ выполнено")
    return "\n".join(lines)


def speech_text(reply: Reply, today: date) -> str:
    """текст для озвучки: без разметки и эмодзи, только главное, и не больше 6 пунктов списка"""
    parts = [reply.text.rstrip(".:")]
    if reply.card:
        parts.append(f"{reply.card.title}, {human_datetime(reply.card.start, reply.card.has_time, today)}")
    if reply.warning:
        parts.append(reply.warning)
    for n, item in enumerate(reply.candidates[:3], 1):
        parts.append(f"{n}: {item.title}, {human_datetime(item.start, item.has_time, today)}")
    if reply.entries:
        for entry in reply.entries[:6]:
            at = f"в {entry.start:%H:%M} " if entry.start and entry.item.has_time else ""
            parts.append(f"{at}{entry.item.title}")
        if len(reply.entries) > 6:
            parts.append(f"и ещё {len(reply.entries) - 6}")
    return ". ".join(parts) + "."


def _entry_line(entry: Entry) -> str:
    item = entry.item
    at = f"{entry.start:%H:%M}" if entry.start and item.has_time else "  —  "
    title = escape(short_title(item.title))
    if item.is_done:
        title = f"<s>{title}</s>"  # выполненные записи зачеркнуты
    repeat = " 🔁" if item.repeat else ""
    return f"<code>{at}</code> {TYPE_ICONS[item.type]} {title}{repeat}"


def _item_line(item: Item, today: date) -> str:
    when = human_datetime(item.start, item.has_time, today)
    done = " ✅" if item.is_done else ""
    return f"{TYPE_ICONS[item.type]} {escape(short_title(item.title))} — {when}{done}"


def _card_buttons(item: Item) -> list[InlineKeyboardButton]:
    def button(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=ItemCb(action=action, item_id=item.id).pack())

    if item.is_done:
        return [button("🗑 Удалить", "delete")]
    return [button("✅ Выполнено", "done"), button("🕐 Перенести", "move"), button("🗑 Удалить", "delete")]


def _browse_rows(state: Browse) -> list[list[InlineKeyboardButton]]:
    rows = []
    # листать по неделям имеет смысл только в календаре, "без срока" и "выполненные" - обычные списки
    if state.view not in ("undated", "done"):
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=BrowseCb(offset=state.offset - 1, view=state.view).pack()),
            InlineKeyboardButton(text="Эта неделя", callback_data=BrowseCb(offset=0, view=state.view).pack()),
            InlineKeyboardButton(text="▶️", callback_data=BrowseCb(offset=state.offset + 1, view=state.view).pack()),
        ])
    filters = [
        InlineKeyboardButton(
            text=f"• {label}" if view == state.view else label,
            callback_data=BrowseCb(offset=state.offset if view not in ("undated", "done") else 0, view=view).pack(),
        )
        for view, label in VIEWS
    ]
    rows += [filters[:3], filters[3:]]
    return rows
