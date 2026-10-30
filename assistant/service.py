"""главная логика: что делать с сообщением, уточняющие вопросы, проверки

разборщик (модель или правила) только определяет, что хочет пользователь.
а все проверки и запись в базу делаются тут. про Telegram этот модуль ничего не знает:
на вход текст, на выход Reply
"""
from __future__ import annotations

import logging
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import dates, schedule
from .dates import human_date, human_datetime, period_title, week_start
from .domain import Browse, Command, Entry, Intent, Item, ItemType, Reply, Status
from .nlu import ParseContext, Parser
from .search import Searcher, key_words_match
from .storage import Storage
from .texts import HELP, TYPE_ACCUSATIVE, TYPE_GENITIVE_PLURAL, TYPE_PLURAL, plural

log = logging.getLogger(__name__)

HISTORY_SIZE = 6  # сколько последних реплик передаем модели как контекст
RECENT_LIMIT = 20  # сколько последних показанных записей помним для "удали вторую"
MAX_PERIOD_DAYS = 92  # список больше чем на 3 месяца не строим
TIME_OPTIONS = (time(9), time(10), time(12), time(15), time(18))  # варианты времени на кнопках, когда спрашиваем время встречи

# какой уточняющий вопрос бот задал и ждет ответа. хранится в базе (dialog_state), поэтому переживает перезапуск
ASK_TIME = "time"  # у встречи нет времени: "на какое время поставить?"
ASK_WHEN = "when"  # у напоминания нет даты или время уже прошло: "когда напомнить?"
ASK_RESCHEDULE = "reschedule"  # "на когда перенести?"
ASK_CHOICE = "choose"  # нашлось несколько записей: "какую из них?"
ASK_DELETE = "delete"  # "удалить ...?" да/нет

GONE = "Эта запись уже удалена."

# подтверждение удаления - только короткий ответ из таких слов ("да", "да, удаляй", "удали ее").
# "удали встречу с командой" - это уже новая команда, а не "да" на прошлый вопрос
_YES = re.compile(
    r"^(?:да|ага|угу|удаляй|удали|подтверждаю|конечно|давай)"
    r"(?:[\s,]+(?:да|удаляй|удали|ее|его|конечно|давай|пожалуйста))*[\s.!]*$"
)
_NO = re.compile(r"^(?:нет|не\s+надо|не\s+нужно|отмена|отмени|стоп|забудь|не\s+удаляй)\b")
_CHOICE = re.compile(r"\b(\d{1,2})\b|\b(перв|втор|трет|четверт|пят)(?:ую|ой|ая|ый|ое|ым|ий|ье|ью)\b")
_CHOICE_ONLY = re.compile(
    r"^(?:(?:номер|№)\s*)?(?:(\d{1,2})|(перв|втор|трет|четверт|пят)(?:ую|ой|ая|ый|ое|ым|ий|ье|ью)?)"
    r"(?:\s+(?:запись|задачу|встречу|напоминание))?$"
)
_ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5}


@dataclass
class DialogState:
    """что бот помнит о диалоге с конкретным пользователем"""
    recent: list[int] = field(default_factory=list)  # id записей, на которые можно сослаться номером
    pending: dict | None = None  # заданный уточняющий вопрос, на который еще не ответили

    @classmethod
    def from_dict(cls, data: dict) -> DialogState:
        return cls(recent=list(data.get("recent", [])), pending=data.get("pending"))

    def to_dict(self) -> dict:
        return {"recent": self.recent[:RECENT_LIMIT], "pending": self.pending}


class Assistant:
    """принимает текст пользователя или нажатие кнопки и возвращает Reply"""

    def __init__(self, storage: Storage, parser: Parser, searcher: Searcher, clock, *,
                 meeting_minutes: int = 60, reminder_lead_minutes: int = 15):
        self._storage = storage
        self._parser = parser
        self._searcher = searcher
        self._clock = clock
        self._meeting = timedelta(minutes=meeting_minutes)  # длина встречи, если конец не указан
        self._lead = reminder_lead_minutes  # за сколько минут напоминать о встрече

    # --- методы, которые вызывает бот ---

    async def handle(self, user_id: int, text: str) -> Reply:
        """обработка сообщения (голосовое сюда приходит уже распознанным текстом)"""
        now = self._clock()
        with self._session(user_id) as state:
            reply = None
            # 1. возможно, это ответ на уточняющий вопрос
            if state.pending:
                reply = await self._answer_pending(user_id, text, state, now)
            # 2. "2" или "вторую" сразу после списка - просто открываем запись, модель не нужна
            if reply is None and state.recent and (choice := _choice(text)):
                reply = self._pick(user_id, choice, state)
            # 3. иначе обычный запрос: разбираем и выполняем
            if reply is None:
                ctx = ParseContext(
                    now=now,
                    recent=self._recent_items(user_id, state),
                    history=self._storage.recent_messages(user_id, HISTORY_SIZE),
                )
                command = await self._parser.parse(text, ctx)
                log.info("user=%s intent=%s", user_id, command.intent.value)
                reply = await self._execute(user_id, command, state, now)
            _attach_question(reply, state)
        self._storage.add_message(user_id, "user", text)
        self._storage.add_message(user_id, "assistant", _summary(reply))
        return reply

    async def answer_button(self, user_id: int, token: str, text: str) -> Reply:
        """нажали кнопку под уточняющим вопросом (время, номер, да/нет). кнопка срабатывает,
        только если бот все еще ждет ответ именно на этот вопрос, иначе старая кнопка ответила бы на новый"""
        pending = DialogState.from_dict(self._storage.load_state(user_id)).pending
        if not pending or pending.get("token") != token:
            return Reply("Этот вопрос уже неактуален.")
        return await self.handle(user_id, text)

    def agenda(self, user_id: int, period: str) -> Reply:
        """быстрые списки для кнопок меню: "today", "tomorrow" или "week" """
        today = self._clock().date()
        if period == "week":
            first = week_start(today)
            bounds = (first, first + timedelta(days=6))
        else:
            day = today + timedelta(days=1 if period == "tomorrow" else 0)
            bounds = (day, day)
        with self._session(user_id) as state:
            return self._list(user_id, Command(Intent.LIST, period=bounds), state, self._clock())

    def browse(self, user_id: int, offset: int = 0, view: str = "all") -> Reply:
        """раздел "Все записи": неделя со сдвигом offset от текущей и фильтр view"""
        today = self._clock().date()
        with self._session(user_id) as state:
            if view == "undated":
                entries = [Entry(item, None) for item in self._storage.undated_items(user_id)]
                text = "Записи без срока" if entries else "Записей без срока нет."
            elif view == "done":
                done = sorted(self._storage.done_items(user_id), key=lambda i: i.start or datetime.max)
                entries = [Entry(item, item.start) for item in done]
                text = "Последние выполненные" if entries else "Выполненных записей пока нет."
            else:
                monday = week_start(today) + timedelta(weeks=offset)
                item_type = ItemType(view) if view in {t.value for t in ItemType} else None
                start = datetime.combine(monday, time())
                entries = self._entries(user_id, start, start + timedelta(days=7), item_type)
                title = period_title(monday, monday + timedelta(days=6), today)
                text = f"Записи на {title}" if entries else f"На {title} записей нет."
            state.recent = _ids(entries)
            return Reply(text, entries=entries, browse=Browse(offset, view))

    # дальше методы для кнопок под сообщениями. запись могли уже удалить, поэтому везде проверка на None

    def open_item(self, user_id: int, item_id: int) -> Reply:
        with self._session(user_id) as state:
            item = self._storage.get_item(user_id, item_id)
            if item is None:
                return Reply(GONE)
            state.recent = [item.id]
            return Reply("Запись:", card=item)

    def complete(self, user_id: int, item_id: int) -> Reply:
        with self._session(user_id) as state:
            item = self._storage.get_item(user_id, item_id)
            if item is None:
                return Reply(GONE)
            return self._complete_item(item, state, self._clock())

    def ask_delete(self, user_id: int, item_id: int) -> Reply:
        with self._session(user_id) as state:
            item = self._storage.get_item(user_id, item_id)
            if item is None:
                return Reply(GONE)
            return _attach_question(self._confirm_delete(item, state), state)

    def start_reschedule(self, user_id: int, item_id: int) -> Reply:
        with self._session(user_id) as state:
            item = self._storage.get_item(user_id, item_id)
            if item is None:
                return Reply(GONE)
            state.recent = [item.id]
            state.pending = _question(ASK_RESCHEDULE, item_id=item.id)
            reply = Reply(f"На когда перенести «{item.title}»? Например: «завтра в 15:00».")
            return _attach_question(reply, state)

    def snooze(self, user_id: int, item_id: int, minutes: int = 15) -> Reply:
        """кнопка "+15 мин" под напоминанием"""
        item = self._storage.get_item(user_id, item_id)
        if item is None:
            return Reply(GONE)
        item.remind_at = self._clock() + timedelta(minutes=minutes)
        item.notified = False
        self._storage.save_item(item)
        return Reply(f"Хорошо, напомню через {minutes} минут.")

    def cancel(self, user_id: int) -> Reply:
        with self._session(user_id) as state:
            state.pending = None
        return Reply("Хорошо, отменил.")

    def day_entries(self, user_id: int, day: date) -> list[Entry]:
        """невыполненные записи на день, для утренней сводки. состояние диалога не трогает"""
        start = datetime.combine(day, time())
        return [e for e in self._entries(user_id, start, start + timedelta(days=1)) if not e.item.is_done]

    def clear(self, user_id: int) -> int:
        self._storage.clear_dialog(user_id)
        return self._storage.delete_user_items(user_id)

    # --- состояние диалога ---

    @contextmanager
    def _session(self, user_id: int):
        """загружает состояние диалога из базы, а после обработки сохраняет обратно"""
        state = DialogState.from_dict(self._storage.load_state(user_id))
        yield state
        self._storage.save_state(user_id, state.to_dict())

    def _recent_items(self, user_id: int, state: DialogState) -> list[Item]:
        items = (self._storage.get_item(user_id, item_id) for item_id in state.recent[:10])
        return [item for item in items if item is not None]

    async def _answer_pending(self, user_id: int, text: str, state: DialogState, now: datetime) -> Reply | None:
        """разбирает ответ на уточняющий вопрос. None - это не ответ, дальше сообщение разбирается как обычный запрос"""
        pending, state.pending = state.pending, None
        low = dates.normalize(text.strip())
        # короткую непонятную фразу считаем неудачным ответом и переспрашиваем,
        # а длинную - новым запросом (пользователь передумал и спросил о другом)
        short = len(low.split()) <= 4
        kind = pending["kind"]

        if kind == ASK_DELETE:
            if _YES.search(low):
                return self._delete_confirmed(user_id, pending["item_id"], state)
            if _NO.search(low):
                return Reply("Хорошо, не удаляю.")
            return None
        if _NO.search(low):
            return Reply("Хорошо, отменил.")

        if kind == ASK_CHOICE:
            if not (m := _CHOICE.search(low)):
                return None
            command = Command.from_dict(pending["command"])
            command.ref = int(m.group(1)) if m.group(1) else _ORDINALS[m.group(2)]
            return await self._execute(user_id, command, state, now)

        when = dates.parse_when(text, now)
        if kind == ASK_TIME:
            if when.at is None:
                return self._ask_again(state, pending, "Не понял время. Напишите, например, «в 15:00».") if short else None
            command = Command.from_dict(pending["command"])
            command.at, command.end_at = when.at, when.end_at or command.end_at
            command.day = when.day or command.day
            return self._create(user_id, command, state, now)
        if kind == ASK_WHEN:
            if not when.found:
                return self._ask_again(state, pending, "Не понял дату. Напишите, например, «завтра в 10».") if short else None
            command = Command.from_dict(pending["command"])
            command.day, command.at = when.day, when.at or command.at
            return self._create(user_id, command, state, now)
        if kind == ASK_RESCHEDULE:
            if not when.found:
                return self._ask_again(state, pending, "Не понял, на когда. Например: «в пятницу в 12:00».") if short else None
            item = self._storage.get_item(user_id, pending["item_id"])
            if item is None:
                return Reply(GONE)
            return self._reschedule(item, when.day, when.at, state, now)
        return None

    def _pick(self, user_id: int, number: int, state: DialogState) -> Reply:
        """"вторую" или "2" после списка - открываем эту запись"""
        if not 1 <= number <= len(state.recent):
            return Reply(f"В списке нет записи под номером {number}.")
        item = self._storage.get_item(user_id, state.recent[number - 1])
        if item is None:
            return Reply(GONE)
        state.recent = [item.id]
        return Reply("Запись:", card=item)

    @staticmethod
    def _ask_again(state: DialogState, pending: dict, text: str) -> Reply:
        state.pending = pending  # вопрос остается открытым
        return Reply(text)

    async def _execute(self, user_id: int, cmd: Command, state: DialogState, now: datetime) -> Reply:
        if cmd.intent is Intent.CREATE:
            return self._create(user_id, cmd, state, now)
        if cmd.intent is Intent.LIST:
            return self._list(user_id, cmd, state, now)
        if cmd.intent is Intent.SEARCH:
            return await self._search(user_id, cmd, state, now)
        if cmd.intent in (Intent.UPDATE, Intent.DELETE, Intent.COMPLETE):
            # сначала находим, о какой записи речь, потом выполняем действие
            item, problem = await self._target(user_id, cmd, state, now)
            if problem:
                return problem
            if cmd.intent is Intent.DELETE:
                return self._confirm_delete(item, state)
            if cmd.intent is Intent.COMPLETE:
                return self._complete_item(item, state, now)
            return self._update(item, cmd, state, now)
        if cmd.intent is Intent.HELP:
            return Reply(HELP)
        if cmd.intent is Intent.CHAT and cmd.text:
            return Reply(cmd.text)
        return Reply(
            "Не совсем понял. Можно сказать, например: «напомни завтра в 10 позвонить Ивану» "
            "или «что у меня на этой неделе?»."
        )

    # --- команды ---

    def _create(self, user_id: int, cmd: Command, state: DialogState, now: datetime) -> Reply:
        title = (cmd.title or "").strip()
        if not title:
            return Reply("Не понял, что записать. Например: «Напомни завтра в 10 позвонить Ивану».")
        item_type = cmd.item_type or ItemType.TASK
        today = now.date()

        # встрече нужно время, напоминанию хотя бы дата или время. если нет - спрашиваем и пока ничего не сохраняем
        if item_type is ItemType.MEETING and cmd.at is None:
            state.pending = _question(ASK_TIME, command=cmd.to_dict())
            day = f" {human_date(cmd.day, today)}" if cmd.day else ""
            return Reply(f"На какое время поставить «{title}»{day}?", time_options=self._time_options(cmd.day, now))
        if item_type is ItemType.REMINDER and cmd.day is None and cmd.at is None:
            state.pending = _question(ASK_WHEN, command=cmd.to_dict())
            return Reply(f"Когда напомнить «{title}»?")

        day = cmd.day or (today if cmd.repeat and cmd.at is None else None)
        start, has_time = dates.combine(day, cmd.at, now)
        if start and _in_past(start, has_time, now) and not cmd.repeat:
            state.pending = _question(ASK_WHEN, command=cmd.to_dict())
            return Reply(f"{human_datetime(start, has_time, today).capitalize()} уже прошло. На когда записать «{title}»?")
        # напоминание на сегодня без времени: о таких бот напоминает в 9:00, а это время уже прошло,
        # и напоминание бы просто не пришло. поэтому спрашиваем, во сколько напомнить
        if (item_type is ItemType.REMINDER and start and not has_time and not cmd.repeat
                and start.date() == today and now.time() >= schedule.UNTIMED_REMINDER_AT):
            state.pending = _question(ASK_TIME, command=cmd.to_dict())
            return Reply(f"Во сколько напомнить «{title}» сегодня?", time_options=self._time_options(today, now))

        end = None
        if item_type is ItemType.MEETING and start:
            # конец встречи: если указано "с 15 до 16:30", берем его, иначе начало + MEETING_MINUTES (по умолчанию час)
            end = datetime.combine(start.date(), cmd.end_at) if cmd.end_at and cmd.end_at > start.time() \
                else start + self._meeting
        item = Item(
            user_id=user_id, type=item_type, title=title, start=start, end=end, has_time=has_time,
            repeat=cmd.repeat if start else None,
        )
        item.remind_at = schedule.reminder_time(item, now, self._lead)
        conflict = self._conflict(item)
        item = self._storage.add_item(item)
        state.recent = [item.id]
        return Reply(f"Записал {TYPE_ACCUSATIVE[item_type]}.", card=item, warning=conflict)

    def _list(self, user_id: int, cmd: Command, state: DialogState, now: datetime) -> Reply:
        today = now.date()
        first, last = cmd.period or (today, today)
        last = min(last, first + timedelta(days=MAX_PERIOD_DAYS))
        start = datetime.combine(first, time())
        entries = self._entries(user_id, start, datetime.combine(last + timedelta(days=1), time()), cmd.item_type)
        state.recent = _ids(entries)

        title = period_title(first, last, today)
        if entries:
            text = f"{TYPE_PLURAL[cmd.item_type]} на {title}" if cmd.item_type else f"План на {title}"
        elif cmd.item_type:
            text = f"На {title} {TYPE_GENITIVE_PLURAL[cmd.item_type]} нет."
        else:
            text = f"На {title} ничего не запланировано."
        return Reply(text, entries=entries)

    async def _search(self, user_id: int, cmd: Command, state: DialogState, now: datetime) -> Reply:
        query = (cmd.query or "").strip()
        if not query:
            return Reply("Что найти? Например: «найди задачу про договор».")
        hits = await self._searcher.search(user_id, query, now, item_type=cmd.item_type, scope=cmd.scope)
        # с фильтрами ничего не нашлось - ищем еще раз без них, на случай если тип или время определены неверно
        if not hits and (cmd.item_type or cmd.scope):
            hits = await self._searcher.search(user_id, query, now)
        if not hits:
            return Reply(f"Ничего не нашёл по запросу «{query}».")
        items = [hit.item for hit in hits]
        state.recent = [item.id for item in items]
        if len(items) == 1:
            return Reply("Нашёл:", card=items[0])
        return Reply(f"Нашёл {len(items)} {plural(len(items), 'запись', 'записи', 'записей')}:", candidates=items)

    async def _target(self, user_id: int, cmd: Command, state: DialogState,
                      now: datetime) -> tuple[Item | None, Reply | None]:
        """находит запись, к которой относится команда. если не получилось, возвращает ответ с объяснением"""
        # по номеру из последнего списка: "удали вторую"
        if cmd.ref is not None:
            if 1 <= cmd.ref <= len(state.recent):
                item = self._storage.get_item(user_id, state.recent[cmd.ref - 1])
                if item:
                    return item, None
            return None, Reply(f"Не нашёл запись под номером {cmd.ref}.")

        # по описанию: "перенеси встречу с Иваном"
        if cmd.query:
            hits = await self._searcher.search(user_id, cmd.query, now, item_type=cmd.item_type)
            # для изменения записи похожести мало: все значимые слова запроса должны быть в названии.
            # иначе "перенеси встречу с Петром" перенесла бы встречу с Иваном, потому что совпало "встреча"
            hits = [hit for hit in hits if key_words_match(cmd.query, hit.item.title, now)]
            if not hits:
                return None, Reply(f"Не нашёл запись «{cmd.query}».")
            if len(hits) == 1:
                return hits[0].item, None
            # подходят несколько - не угадываем, а спрашиваем. ответить можно номером или кнопкой
            state.recent = [hit.item.id for hit in hits]
            state.pending = _question(ASK_CHOICE, command=cmd.to_dict())
            return None, Reply("Нашёл несколько подходящих записей. Какую из них?",
                               candidates=[hit.item for hit in hits])

        # "перенеси ее": если последней показана одна запись, речь о ней
        if len(state.recent) == 1 and (item := self._storage.get_item(user_id, state.recent[0])):
            return item, None
        if state.recent:
            return None, Reply("Уточните, какую запись: назовите её номер из списка или название.")
        return None, Reply("Не понял, о какой записи речь. Например: «перенеси встречу с Иваном на пятницу».")

    def _update(self, item: Item, cmd: Command, state: DialogState, now: datetime) -> Reply:
        state.recent = [item.id]
        if cmd.title:
            item.title = cmd.title
        if cmd.day is None and cmd.at is None:
            if cmd.title:
                return Reply("Переименовал.", card=self._storage.save_item(item))
            state.pending = _question(ASK_RESCHEDULE, item_id=item.id)
            return Reply(f"На когда перенести «{item.title}»?")
        return self._reschedule(item, cmd.day, cmd.at, state, now)

    def _reschedule(self, item: Item, day: date | None, at: time | None, state: DialogState, now: datetime) -> Reply:
        # меняем только то, что названо: "на пятницу" оставляет время, "на 15:00" оставляет день
        duration = self._duration(item) if item.end else None
        if day is None and at is not None and item.start:
            day = item.start.date()
        if at is None and day is not None and item.start and item.has_time:
            at = item.start.time()
        start, has_time = dates.combine(day, at, now)
        if start and _in_past(start, has_time, now) and not item.repeat:
            state.pending = _question(ASK_RESCHEDULE, item_id=item.id)
            return Reply(f"{human_datetime(start, has_time, now.date()).capitalize()} уже прошло. На когда перенести?")

        item.start, item.has_time = start, has_time
        if item.type is ItemType.MEETING and has_time:
            item.end = start + (duration or self._meeting)  # длина встречи сохраняется
        else:
            item.end = None
        item.remind_at = schedule.reminder_time(item, now, self._lead)
        item.notified = False
        conflict = self._conflict(item)
        item = self._storage.save_item(item)
        state.recent = [item.id]
        return Reply(f"Перенёс на {human_datetime(start, has_time, now.date())}.", card=item, warning=conflict)

    def _confirm_delete(self, item: Item, state: DialogState) -> Reply:
        # без подтверждения не удаляем
        state.recent = [item.id]
        state.pending = _question(ASK_DELETE, item_id=item.id)
        series = " (все повторы)" if item.repeat else ""
        return Reply(f"Удалить {TYPE_ACCUSATIVE[item.type]} «{item.title}»{series}?", confirm_delete=item)

    def _delete_confirmed(self, user_id: int, item_id: int, state: DialogState) -> Reply:
        item = self._storage.get_item(user_id, item_id)
        if item is None:
            return Reply(GONE)
        self._storage.delete_item(user_id, item_id)
        state.recent = [i for i in state.recent if i != item_id]
        return Reply(f"Удалил «{item.title}».")

    def _complete_item(self, item: Item, state: DialogState, now: datetime) -> Reply:
        state.recent = [item.id]
        if item.is_done:
            return Reply("Эта запись уже выполнена.", card=item)
        # повторяющуюся запись не закрываем, а переносим на следующий раз
        if item.repeat and item.start:
            following = schedule.next_occurrence(item, max(now, item.start))
            if item.end:
                item.end = following + (item.end - item.start)
            item.start = following
            item.remind_at = schedule.reminder_time(item, now, self._lead)
            item.notified = False
            item = self._storage.save_item(item)
            return Reply(f"Отметил. Следующий раз — {human_datetime(following, item.has_time, now.date())}.", card=item)
        item.status = Status.DONE
        item.remind_at = None
        item = self._storage.save_item(item)
        return Reply(f"Отметил «{item.title}» выполненной.", card=item)

    # --- вспомогательное ---

    def _entries(self, user_id: int, start: datetime, end: datetime, item_type: ItemType | None = None) -> list[Entry]:
        """записи за период. повторяющиеся раскладываются на отдельные даты"""
        entries = [
            Entry(item, moment)
            for item in self._storage.items_between(user_id, start, end)
            if item_type is None or item.type is item_type
            for moment in schedule.occurrences(item, start, end)
        ]
        # внутри дня сначала записи без времени, потом по времени
        entries.sort(key=lambda e: (e.start.date(), e.item.has_time, e.start))
        return entries

    def _duration(self, item: Item) -> timedelta:
        if item.start and item.end and item.end > item.start:
            return item.end - item.start
        return self._meeting

    def _conflict(self, item: Item) -> str | None:
        """текст предупреждения, если встречи пересекаются. запись все равно сохраняется, решает пользователь"""
        if item.type is not ItemType.MEETING or not item.start or not item.has_time:
            return None
        end = item.start + self._duration(item)
        day = datetime.combine(item.start.date(), time())
        for entry in self._entries(item.user_id, day, day + timedelta(days=1), ItemType.MEETING):
            other = entry.item
            if other.id == item.id or not other.has_time or other.is_done:
                continue
            if entry.start < end and item.start < entry.start + self._duration(other):
                return f"В это время уже есть «{other.title}» ({entry.start:%H:%M}). Записал, но проверьте расписание."
        return None

    @staticmethod
    def _time_options(day: date | None, now: datetime) -> list[str]:
        # на сегодня не предлагаем время, которое уже прошло
        options = TIME_OPTIONS
        if day is None or day == now.date():
            options = [t for t in TIME_OPTIONS if t > now.time()] or list(TIME_OPTIONS)
        return [t.strftime("%H:%M") for t in options]


def _choice(text: str) -> int | None:
    """номер в списке, если сообщение состоит только из него: "2", "вторую", "номер 3" """
    low = dates.normalize(text.strip()).strip(" .!?")
    m = _CHOICE_ONLY.match(low)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else _ORDINALS[m.group(2)]


def _question(kind: str, **data) -> dict:
    """уточняющий вопрос для состояния диалога. token - метка вопроса, к ней привязаны кнопки ответа"""
    return {"kind": kind, "token": secrets.token_hex(3), **data}


def _attach_question(reply: Reply, state: DialogState) -> Reply:
    """если бот задал вопрос, помечаем ответ меткой этого вопроса, чтобы кнопки под ним относились именно к нему"""
    reply.question = state.pending.get("token") if state.pending else None
    return reply


def _in_past(start: datetime, has_time: bool, now: datetime) -> bool:
    # если время не указано, прошедшим считаем только вчерашний день и раньше
    return start < now if has_time else start.date() < now.date()


def _ids(entries: list[Entry]) -> list[int]:
    return list(dict.fromkeys(entry.item.id for entry in entries))[:RECENT_LIMIT]


def _summary(reply: Reply) -> str:
    """краткий текст ответа бота для истории диалога, которую видит модель"""
    lines = [reply.text]
    if reply.card:
        when = reply.card.start.isoformat(sep=" ", timespec="minutes") if reply.card.start else "без срока"
        lines.append(f"{reply.card.title} ({when})")
    lines += [f"{n}. {item.title}" for n, item in enumerate(reply.candidates, 1)]
    if reply.entries:
        lines += [f"- {entry.item.title}" for entry in reply.entries[:10]]
    return "\n".join(lines)
