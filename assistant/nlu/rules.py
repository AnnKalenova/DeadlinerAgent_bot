"""разбор на правилах (регулярные выражения), запасной вариант

понимает типичные фразы из задания и в сеть не ходит. включается когда модель
не настроена (нет ключей яндекса) или не ответила
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..dates import find_period, normalize, parse_when, strip_spans
from ..domain import Command, Intent, ItemType, Repeat
from .base import ParseContext

# намерение проверяется по порядку: справка, удаление, выполнение, перенос, поиск, список, создание.
# порядок важен: в "удали встречу на завтра" есть и "удали", и дата, но это удаление, а не новая запись
_HELP = re.compile(
    r"^(?:/?help|помощь|справка|помоги|что ты умеешь|что умеешь|как (?:тобой )?пользоваться"
    r"|привет|здравствуй\w*|добрый (?:день|вечер)|доброе утро|hi|hello)\b"
)
_DELETE = re.compile(r"\b(?:удали\w*|убери|сотри|отмени)\b")
_COMPLETE = re.compile(
    r"\b(?:отметь|пометь)\b.*\b(?:выполн\w*|сделан\w*|готов\w*)"
    r"|\b(?:выполнил\w*|сделал\w*|закрой|заверши)\b|^готово\b"
)
# для "отметь ... выполненной" из фразы убираем только эти слова, а то, что между ними, - описание записи.
# если вырезать весь кусок от "отметь" до "выполненной", описание пропадет и отметится не та запись
_COMPLETE_WORDS = re.compile(r"\b(?:отметь|пометь|выполн\w*|сделан\w*|сделал\w*|готов\w*|закрой|заверши)\b")
_UPDATE =re.compile(r"\b(?:перенеси|перенести|сдвинь|передвинь|перемести|поменяй|измени)\b")
_SEARCH = re.compile(
    r"\b(?:найди|найти|поищи|ищи)\b|^(?:а\s+)?когда\b|\bгде\s+(?:моя|мой|мое)\b|\bкогда-то\b"
)
_LIST = re.compile(
    r"\bчто\s+(?:у\s+меня|по\s+плану|запланировано|намечено|было|будет)"
    r"|^что\s+(?:на|в|во|сегодня|завтра|послезавтра)\b"
    r"|\bкакие\s+(?:у\s+меня\s+)?(?:планы|задачи|встречи|дела|напоминания)"
    r"|\bпокажи\b|\bесть\s+ли\b|\bмои\s+(?:встречи|задачи|дела|напоминания|планы)"
    r"|\bплан\w*\s+на\b|\bрасписание\b|\bсводк\w*|\bчем\s+(?:я\s+)?занят"
    r"|\bсвобод(?:ен|на|ны|ное)\b|\bчто-(?:нибудь|то)\s+(?:есть|запланировано)"
)
# признаки новой записи. последняя часть находит любой глагол в инфинитиве, поэтому "купить молоко" тоже задача
_CREATE_HINT = re.compile(
    r"\b(?:напомни\w*|поставь|добавь|создай|запланируй|запиши|внеси|назначь|нужно|надо|необходимо"
    r"|не\s+забыть|не\s+забудь)\b|\b\w{3,}(?:ть|ться|ти)\b"
)

_REMINDER_WORDS = re.compile(r"\b(?:напомни\w*|напоминани\w*|не\s+забыть|не\s+забудь)\b")
# только именительный и винительный падеж: "подготовиться к встрече" это задача, а не встреча
_MEETING_WORDS = re.compile(
    r"\b(?:встреча|встречу|созвон|созвонимся|совещание|планерка|планерку|собеседование"
    r"|переговоры|звонок|конференция|конференцию|митинг|стендап)\b"
)
_ANY_MEETING = re.compile(r"\b(?:встреч\w*|созвон\w*|совещани\w*|планерк\w*|собеседовани\w*|переговор\w*)")
_ANY_TASK = re.compile(r"\bзадач\w*")
_ANY_REMINDER = re.compile(r"\bнапомина\w*")
# для поиска: искать в прошлом ("когда была встреча") или в будущем ("когда будет")
_PAST = re.compile(r"\b(?:был\w*|ставил\w*|раньше|прошл\w*|когда-то|давно|вчера)\b")
_FUTURE = re.compile(r"\b(?:будет|предстоящ\w*|следующ\w*|ближайш\w*)\b")

_REPEATS = (
    (re.compile(r"\b(?:каждый\s+день|ежедневно)\b"), Repeat.DAILY),
    (re.compile(r"\b(?:каждую\s+неделю|еженедельно)\b"), Repeat.WEEKLY),
    (re.compile(r"\b(?:каждый\s+месяц|ежемесячно)\b"), Repeat.MONTHLY),
)
_REPEAT_WEEKDAY = re.compile(
    r"\b(?:(?:каждый|каждую|каждое)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)"
    r"|по\s+(понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям))\b"
)
_WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен")

# ссылка на запись из списка: "вторую", "номер 3", "#2"
_ORDINAL = re.compile(r"\b(перв|втор|трет|четверт|пят|шест)(?:ую|ой|ая|ый|ое|ым|ий|ье|ью)\b")
_ORDINAL_VALUES = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6}
_NUMBER_REF = re.compile(r"(?:№|#|номер\s*)(\d{1,2})\b")

# служебные слова: удаляются из начала и конца названия и из поисковых запросов
_LEADING_FILLER = {
    "а", "ну", "и", "пожалуйста", "напомни", "напомнить", "напоминание", "мне", "поставь", "добавь",
    "создай", "запланируй", "запиши", "внеси", "назначь", "задачу", "задача", "нужно", "надо",
    "необходимо", "не", "забыть", "забудь", "что", "чтобы", "о", "том", "про", "новую", "еще",
}
_TRAILING_FILLER = {"в", "во", "на", "к", "ко", "до", "с", "и", "а", "по", "мне", "пожалуйста"}
_QUERY_FILLER = {
    "а", "ну", "и", "пожалуйста", "мне", "меня", "у", "я", "мою", "мой", "моя", "мое", "задачу", "задача",
    "задачи", "запись", "записи", "напоминание", "про", "о", "об", "по", "как", "это", "эту", "ее", "его",
    "ту", "тот", "последнюю", "когда", "где", "когда-то", "ставил", "ставила", "была",
    "был", "было", "будет", "найди", "найти", "поищи", "ищи", "все", "всё", "мои", "выполненной",
    "выполненным", "выполнена", "сделанной", "готово", "на", "в",
}
# "поставь встречу с Иваном" -> название "Встреча с Иваном", а не "Встречу с Иваном"
_NOMINATIVE = {"встречу": "встреча", "планерку": "планерка", "конференцию": "конференция"}


def _clean_token(token: str) -> str:
    return normalize(token).strip(",.!?;:«»\"'()")


def _trim(tokens: list[str], leading: set[str], trailing: set[str]) -> list[str]:
    while tokens and _clean_token(tokens[0]) in leading:
        tokens = tokens[1:]
    while tokens and _clean_token(tokens[-1]) in trailing:
        tokens = tokens[:-1]
    return tokens


def make_title(text: str) -> str:
    """оставшуюся часть фразы (без дат и служебных слов) превращает в короткое название с большой буквы"""
    text = re.sub(r"(?i)\bу\s+меня\b", " ", text)
    tokens = _trim(text.split(), _LEADING_FILLER, _TRAILING_FILLER)
    if not tokens:
        return ""
    first = _clean_token(tokens[0])
    if first in _NOMINATIVE:
        tokens[0] = _NOMINATIVE[first]
    title = " ".join(tokens).strip(" ,.!?;:-")
    return title[:1].upper() + title[1:]


def make_query(text: str) -> str | None:
    """что искать: фраза без служебных слов"""
    words = [w for w in text.split() if _clean_token(w) and _clean_token(w) not in _QUERY_FILLER]
    query = " ".join(words).strip(" ,.!?;:")
    return query or None


def _repeat(low: str, today: date) -> tuple[Repeat | None, date | None, tuple[int, int] | None]:
    for pattern, repeat in _REPEATS:
        if m := pattern.search(low):
            return repeat, None, m.span()
    if m := _REPEAT_WEEKDAY.search(low):
        word = m.group(1) or m.group(2)
        weekday = next(i for i, stem in enumerate(_WEEKDAY_STEMS) if word.startswith(stem))
        return Repeat.WEEKLY, today + timedelta(days=(weekday - today.weekday()) % 7), m.span()
    return None, None, None


def _reference(low: str) -> int | None:
    if m := _NUMBER_REF.search(low):
        return int(m.group(1))
    if m := _ORDINAL.search(low):
        return _ORDINAL_VALUES[m.group(1)]
    return None


def _filter_type(low: str) -> ItemType | None:
    if _ANY_MEETING.search(low):
        return ItemType.MEETING
    if _ANY_REMINDER.search(low):
        return ItemType.REMINDER
    if _ANY_TASK.search(low):
        return ItemType.TASK
    return None


class RuleParser:
    """разборщик на регулярных выражениях"""

    async def parse(self, text: str, ctx: ParseContext) -> Command:
        return self.parse_sync(text, ctx)

    def parse_sync(self, text: str, ctx: ParseContext) -> Command:
        text = text.strip()
        low = normalize(text)
        if not low:
            return Command(Intent.UNKNOWN)
        if _HELP.search(low):
            return Command(Intent.HELP)
        if _DELETE.search(low):
            return self._targeted(Intent.DELETE, text, _DELETE)
        if _COMPLETE.search(low):
            return self._targeted(Intent.COMPLETE, text, _COMPLETE_WORDS)
        if _UPDATE.search(low):
            return self._update(text, ctx)
        if _SEARCH.search(low):
            return self._search(text, low)
        if _LIST.search(low):
            return self._list(low, ctx)
        if _CREATE_HINT.search(low) or _MEETING_WORDS.search(low) or parse_when(text, ctx.now).found:
            return self._create(text, ctx)
        return Command(Intent.UNKNOWN)

    def _create(self, text: str, ctx: ParseContext) -> Command:
        low = normalize(text)
        # сначала убираем повтор ("каждую пятницу"), иначе "пятницу" распознается как дата
        repeat, repeat_day, repeat_span = _repeat(low, ctx.now.date())
        if repeat_span:
            text = strip_spans(text, [repeat_span])
            low = normalize(text)
        when = parse_when(text, ctx.now)

        if _REMINDER_WORDS.search(low):
            item_type = ItemType.REMINDER
        elif _MEETING_WORDS.search(low):
            item_type = ItemType.MEETING
        else:
            item_type = ItemType.TASK

        title = make_title(strip_spans(text, when.spans))  # название = фраза без даты и времени
        if not title:
            return Command(Intent.UNKNOWN)
        return Command(
            Intent.CREATE, item_type=item_type, title=title, day=when.day or repeat_day,
            at=when.at, end_at=when.end_at, repeat=repeat,
        )

    def _list(self, low: str, ctx: ParseContext) -> Command:
        today = ctx.now.date()
        found = find_period(low, today, prefer_future=not _PAST.search(low))
        period = (found[0], found[1]) if found else (today, today)
        return Command(Intent.LIST, period=period, item_type=_filter_type(low))

    def _search(self, text: str, low: str) -> Command:
        scope = "past" if _PAST.search(low) else "future" if _FUTURE.search(low) else None
        item_type = ItemType.MEETING if _ANY_MEETING.search(low) else None
        return Command(Intent.SEARCH, query=make_query(text), item_type=item_type, scope=scope)

    def _targeted(self, intent: Intent, text: str, verb: re.Pattern) -> Command:
        # удалить или выполнить: запись берем по номеру ("вторую"), а если номера нет - по описанию
        low = normalize(text)
        ref = _reference(low)
        rest = verb.sub(" ", low)
        rest = _ORDINAL.sub(" ", _NUMBER_REF.sub(" ", rest))
        item_type = ItemType.MEETING if _ANY_MEETING.search(low) else None
        return Command(intent, ref=ref, query=None if ref else make_query(rest), item_type=item_type)

    def _update(self, text: str, ctx: ParseContext) -> Command:
        # перенос: новая дата и время берутся из фразы, а остаток - описание записи, которую переносим
        low = normalize(text)
        when = parse_when(text, ctx.now)
        rest = normalize(strip_spans(text, when.spans))
        rest = _ORDINAL.sub(" ", _NUMBER_REF.sub(" ", _UPDATE.sub(" ", rest)))
        ref = _reference(low)
        return Command(
            Intent.UPDATE, day=when.day, at=when.at, end_at=when.end_at,
            ref=ref, query=None if ref else make_query(rest),
        )
