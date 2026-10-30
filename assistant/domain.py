"""типы данных, которыми обмениваются разборщики, ассистент и бот"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum


class ItemType(str, Enum):
    TASK = "task"
    REMINDER = "reminder"
    MEETING = "meeting"


class Status(str, Enum):
    OPEN = "open"
    DONE = "done"


class Repeat(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class Item:
    """одна запись пользователя: задача, напоминание или встреча (строка в таблице items)"""

    user_id: int
    type: ItemType
    title: str
    start: datetime | None = None  # None - запись без срока
    end: datetime | None = None  # конец, бывает только у встреч
    has_time: bool = False  # False - указан только день, без времени
    status: Status = Status.OPEN
    repeat: Repeat | None = None
    remind_at: datetime | None = None  # когда отправить следующее напоминание
    notified: bool = False  # напоминание уже отправлено
    origin: str = "chat"  # chat или demo (загружено из csv по /demo)
    source_text: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_done(self) -> bool:
        return self.status is Status.DONE


@dataclass(frozen=True)
class Entry:
    """одно появление записи в календаре. у повторяющейся записи их много"""
    item: Item
    start: datetime | None


class Intent(str, Enum):
    CREATE = "create"
    LIST = "list"
    SEARCH = "search"
    UPDATE = "update"
    DELETE = "delete"
    COMPLETE = "complete"
    HELP = "help"
    CHAT = "chat"
    UNKNOWN = "unknown"


@dataclass
class Command:
    """что хочет пользователь: действие + параметры, извлеченные из сообщения

    для UPDATE поля day/at/title это новые значения, а какую запись менять
    понимаем по ref (номер в последнем списке) или по query (описание словами)
    """
    intent: Intent
    item_type: ItemType | None = None
    title: str | None = None
    day: date | None = None
    at: time | None = None
    end_at: time | None = None
    repeat: Repeat | None = None
    period: tuple[date, date] | None = None  # с какого по какое число, оба дня включительно
    query: str | None = None
    ref: int | None = None
    scope: str | None = None  # где искать: "past" в прошлом, "future" в будущем
    text: str | None = None  # текстовый ответ модели (для CHAT)

    # в базу сохраняем только то, что нужно, чтобы выполнить команду после уточняющего вопроса
    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "item_type": self.item_type.value if self.item_type else None,
            "title": self.title,
            "day": self.day.isoformat() if self.day else None,
            "at": self.at.strftime("%H:%M") if self.at else None,
            "end_at": self.end_at.strftime("%H:%M") if self.end_at else None,
            "repeat": self.repeat.value if self.repeat else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Command:
        return cls(
            intent=Intent(data["intent"]),
            item_type=ItemType(data["item_type"]) if data.get("item_type") else None,
            title=data.get("title"),
            day=date.fromisoformat(data["day"]) if data.get("day") else None,
            at=time.fromisoformat(data["at"]) if data.get("at") else None,
            end_at=time.fromisoformat(data["end_at"]) if data.get("end_at") else None,
            repeat=Repeat(data["repeat"]) if data.get("repeat") else None,
        )


@dataclass(frozen=True)
class Browse:
    """положение в разделе "Все записи": сдвиг в неделях от текущей и выбранный фильтр"""
    offset: int = 0
    view: str = "all"  # all | task | meeting | reminder | undated | done


@dataclass
class Reply:
    """ответ ассистента. как его показать (текст, кнопки, голос) решает бот, а не ассистент"""
    text: str
    warning: str | None = None  # например что встречи пересекаются
    card: Item | None = None  # одна запись карточкой
    entries: list[Entry] | None = None  # список за период. пустой список != None: значит "на этот день ничего"
    candidates: list[Item] = field(default_factory=list)  # нашлось несколько записей, надо выбрать
    time_options: list[str] = field(default_factory=list)  # кнопки со временем: "09:00", "10:00"...
    confirm_delete: Item | None = None  # кнопки "Да, удалить" / "Отмена"
    browse: Browse | None = None  # кнопки листания по неделям и фильтры
