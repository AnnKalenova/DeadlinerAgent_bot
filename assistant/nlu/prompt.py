"""что отправляем модели: системный промпт и список инструментов (действий), из которых она выбирает"""
from __future__ import annotations

from datetime import timedelta

from ..dates import WEEKDAYS
from ..domain import Item
from .base import ParseContext

SYSTEM_PROMPT = """Ты ведёшь задачи, напоминания и встречи пользователя в Telegram.
На каждое сообщение вызывай ровно один инструмент. Обычным текстом отвечай, только если сообщение не про задачи и встречи (приветствие, вопрос о твоих возможностях, благодарность): тогда ответь одной-двумя фразами.

Сейчас {now:%Y-%m-%d %H:%M}, {weekday}.
Ближайшие дни:
{calendar}

Правила:
- Дата — YYYY-MM-DD, время — HH:MM в 24-часовом формате. Бери даты из списка ближайших дней, не высчитывай дни недели сам.
- Время передавай, только если пользователь его назвал. «Утром» — 09:00, «днём» — 14:00, «вечером» — 19:00. «В 3», «в 5» без уточнения — это 15:00, 17:00.
- «В пятницу» — ближайшая пятница после сегодняшнего дня. «На следующей неделе» — понедельник следующей недели. «До конца недели» — воскресенье этой недели. «Через два часа» — текущее время плюс два часа.
- Тип: meeting — встреча, созвон, совещание, планёрка, собеседование; reminder — когда просят напомнить; task — всё остальное. «Подготовиться к встрече» — это task.
- Название — коротко, с заглавной буквы, без даты, времени и служебных слов («напомни», «нужно», «поставь задачу»). Падежи сохраняй: «Позвонить Ивану», «Встреча с командой».
- Период для list_items — границы включительно: «сегодня» — одна и та же дата в обоих полях, «эта неделя» — с понедельника по воскресенье.
- «Найди…», «когда у меня была/будет…» — это search_items. Для «была» укажи when=past.
- Если пользователь ссылается на запись из списка ниже («её», «эту», «вторую»), передай её номер в item_number. Если запись описана словами и её нет в списке, передай описание в query.
- Если для действия не хватает данных, всё равно вызывай инструмент с тем, что известно: недостающее уточнит программа.

{recent}"""

_TYPE = {"type": "string", "enum": ["task", "reminder", "meeting"]}
_DATE = {"type": "string", "description": "Дата в формате YYYY-MM-DD"}
_TIME = {"type": "string", "description": "Время в формате HH:MM"}
_ITEM_NUMBER = {"type": "integer", "description": "Номер записи из списка последних записей"}
_QUERY = {"type": "string", "description": "Описание записи своими словами, если номера нет"}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


# инструменты. на каждое сообщение модель выбирает ровно один и заполняет параметры
TOOLS = [
    _tool("create_item", "Создать задачу, напоминание или встречу.", {
        "type": _TYPE,
        "title": {"type": "string", "description": "Короткое название записи"},
        "date": _DATE,
        "time": {**_TIME, "description": "Время начала HH:MM, только если названо"},
        "end_time": {**_TIME, "description": "Время окончания встречи HH:MM, если названо"},
        "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"],
                   "description": "Повтор: «каждый день», «каждую пятницу», «каждый месяц»"},
    }, ["type", "title"]),
    _tool("list_items", "Показать записи за период: план на день, неделю и т. п.", {
        "date_from": _DATE,
        "date_to": _DATE,
        "type": {**_TYPE, "description": "Только записи этого типа, если пользователь об этом просит"},
    }, ["date_from", "date_to"]),
    _tool("search_items", "Найти записи по описанию, в том числе прошлые.", {
        "query": {"type": "string", "description": "Что искать: ключевые слова из запроса"},
        "type": _TYPE,
        "when": {"type": "string", "enum": ["past", "future"], "description": "Искать только в прошлом или будущем"},
    }, ["query"]),
    _tool("update_item", "Перенести запись на другую дату или время или переименовать её.", {
        "item_number": _ITEM_NUMBER,
        "query": _QUERY,
        "date": {**_DATE, "description": "Новая дата YYYY-MM-DD"},
        "time": {**_TIME, "description": "Новое время HH:MM"},
        "title": {"type": "string", "description": "Новое название, если просят переименовать"},
    }),
    _tool("delete_item", "Удалить запись.", {"item_number": _ITEM_NUMBER, "query": _QUERY}),
    _tool("complete_item", "Отметить задачу выполненной.", {"item_number": _ITEM_NUMBER, "query": _QUERY}),
]


def _calendar(ctx: ParseContext, days: int = 14) -> str:
    """даты с днями недели на 2 недели вперед. модели часто ошибаются, вычисляя дату "в пятницу",
    а так модель берет готовую дату из списка"""
    today = ctx.now.date()
    marks = {0: " (сегодня)", 1: " (завтра)", 2: " (послезавтра)"}
    lines = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        lines.append(f"{day.isoformat()} {WEEKDAYS[day.weekday()]}{marks.get(offset, '')}")
    return "\n".join(lines)


def _describe(item: Item) -> str:
    when = "без срока"
    if item.start:
        when = item.start.strftime("%Y-%m-%d %H:%M" if item.has_time else "%Y-%m-%d")
    status = ", выполнена" if item.is_done else ""
    return f"{item.title} ({item.type.value}, {when}{status})"


def build_system_prompt(ctx: ParseContext) -> str:
    """промпт: текущее время, календарь и пронумерованные последние записи (для "удали вторую")"""
    if ctx.recent:
        lines = "\n".join(f"{n}. {_describe(item)}" for n, item in enumerate(ctx.recent, 1))
        recent = f"Последние записи, которые видел пользователь:\n{lines}"
    else:
        recent = "Последних записей в диалоге нет."
    return SYSTEM_PROMPT.format(
        now=ctx.now, weekday=WEEKDAYS[ctx.now.weekday()], calendar=_calendar(ctx), recent=recent,
    )
