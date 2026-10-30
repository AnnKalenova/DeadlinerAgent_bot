"""разбор русских дат и времени из текста, и обратно - красиво написать дату для пользователя

функции разбора отдают еще и позиции найденных кусков, чтобы потом вырезать
их из названия ("позвонить Ивану завтра в 10" -> "позвонить Ивану")
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# дни недели во всех падежах, которые реально говорят: "в среду", "к пятнице", "до вторника"
_WEEKDAY_FORMS = {
    0: ("понедельник", "понедельника", "понедельнику"),
    1: ("вторник", "вторника", "вторнику"),
    2: ("среда", "среду", "среды", "среде"),
    3: ("четверг", "четверга", "четвергу"),
    4: ("пятница", "пятницу", "пятницы", "пятнице"),
    5: ("суббота", "субботу", "субботы", "субботе"),
    6: ("воскресенье", "воскресенья", "воскресенью"),
}
WEEKDAY_BY_FORM = {form: wd for wd, forms in _WEEKDAY_FORMS.items() for form in forms}

# числа словами, голосовые часто так и распознаются: "через два часа", "в пять вечера"
_NUMBERS = {
    "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40, "сорок пять": 45,
}
_NUM_WORDS = "|".join(sorted(_NUMBERS, key=len, reverse=True))  # длинные первыми, чтоб "сорок пять" не съелось как "сорок"
_DAYPART = r"(утра|дня|вечера|ночи)"

Span = tuple[int, int]  # (начало, конец) куска в тексте

# --- регулярки. работают по тексту в нижнем регистре, где ё уже заменена на е ---

_OFFSET = re.compile(
    rf"\bчерез\s+(?:(?P<n>\d{{1,3}}|{_NUM_WORDS}|полтора|полторы)\s+)?"
    r"(?P<unit>полчаса|минут\w*|час\w*|дн\w*|день|недел\w*|месяц\w*)"
)
_RELATIVE_DAYS = (
    (re.compile(r"\bпослезавтра\b"), 2),
    (re.compile(r"\bзавтра\b"), 1),
    (re.compile(r"\bсегодня\b"), 0),
    (re.compile(r"\bвчера\b"), -1),
)
_DAY_MONTH = re.compile(
    rf"\b(?:(?:на|к|до|с)\s+)?(\d{{1,2}})(?:-?го)?\s+({'|'.join(MONTHS)})(?:\s+(\d{{4}})(?:\s*(?:года|г\.?))?)?"
)
_NUMERIC_DATE = re.compile(r"\b(?:(?:на|к|до)\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d:])")
_WEEKDAY = re.compile(
    r"\b(?:(?:в|во|к|ко|на|до)\s+)?(?:(эт\w+|следующ\w+|ближайш\w+)\s+)?"
    rf"({'|'.join(sorted(WEEKDAY_BY_FORM, key=len, reverse=True))})\b"
)
_NEXT_WEEK = re.compile(r"\b(?:(?:на|в)\s+)?следующ\w+\s+недел\w*")
_END_OF_WEEK = re.compile(r"\b(?:до|к)\s+конц\w*\s+(?:этой\s+)?недел\w*")
_THIS_WEEK = re.compile(r"\b(?:(?:на|в)\s+)?(?:эт\w+\s+)?недел[юеи]\b")
_WEEKEND = re.compile(r"\b(?:(?:на|в)\s+)?(?:эт\w+\s+)?выходн\w*")
_NEXT_MONTH = re.compile(r"\b(?:(?:на|в)\s+)?следующ\w+\s+месяц\w*")
_THIS_MONTH = re.compile(r"\b(?:(?:на|в)\s+)?(?:эт\w+\s+)?месяц[еа]?\b")

_RANGE = re.compile(rf"\bс\s+(\d{{1,2}})(?::(\d{{2}}))?\s+до\s+(\d{{1,2}})(?::(\d{{2}}))?(?:\s+{_DAYPART})?")
_CLOCK = re.compile(rf"\b(?:(?:в|к|на|до|около)\s+)?(\d{{1,2}}):(\d{{2}})(?:\s+{_DAYPART})?")
_CLOCK_DOT = re.compile(rf"\b(?:в|к|около)\s+(\d{{1,2}})\.(\d{{2}})(?![.\d])(?:\s+{_DAYPART})?")
_HOUR = re.compile(rf"\b(?:в|к|около)\s+(\d{{1,2}})(?:\s+час\w*)?(?:\s+{_DAYPART})?\b(?![.:]\d)")
_HOUR_WORDS = re.compile(rf"\b(?:в|к|около)\s+({_NUM_WORDS})(?:\s+час\w*)?\s+{_DAYPART}")
_HOUR_DAYPART = re.compile(rf"\b(\d{{1,2}})\s+{_DAYPART}")
_NAMED_TIMES = (
    (re.compile(r"\bв\s+час\s+дня\b"), time(13, 0)),
    (re.compile(r"\bв\s+час\s+ночи\b"), time(1, 0)),
    (re.compile(r"\bв\s+полдень\b"), time(12, 0)),
    (re.compile(r"\bв\s+полночь\b"), time(0, 0)),
    (re.compile(r"\b(?:с\s+утра|утром)\b"), time(9, 0)),
    (re.compile(r"\b(?:днем|после\s+обеда)\b"), time(14, 0)),
    (re.compile(r"\bвечером\b"), time(19, 0)),
)


@dataclass
class When:
    """что нашли во фразе: день, время, конец встречи и где эти куски стояли в тексте"""

    day: date | None = None
    at: time | None = None
    end_at: time | None = None
    spans: list[Span] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.day is not None or self.at is not None


def normalize(text: str) -> str:
    """нижний регистр и ё -> е. длина строки не меняется, поэтому позиции совпадают с исходным текстом"""
    return text.lower().replace("ё", "е")


def _number(token: str | None) -> int | None:
    if token is None:
        return None
    return int(token) if token.isdigit() else _NUMBERS.get(token)


def _hour(hour: int, daypart: str | None) -> int:
    if daypart in ("дня", "вечера") and hour < 12:
        return hour + 12
    if daypart == "ночи" and hour == 12:
        return 0
    if daypart is None and 1 <= hour <= 6:
        return hour + 12  # "в 3" почти всегда значит 15:00, в 3 ночи никто встречи не ставит
    return hour


def _make_time(hour: int, minute: int = 0) -> time | None:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def add_months(day: date, months: int) -> date:
    """прибавляет месяцы. если такого числа нет, берем последний день: 31 января + 1 месяц = 28 (29) февраля"""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())  # понедельник этой недели


# --- даты ---

def _specific_date(low: str, today: date, prefer_future: bool) -> tuple[date, Span] | None:
    """конкретный день: сегодня, 26 октября, 26.10, в пятницу"""
    for pattern, delta in _RELATIVE_DAYS:
        if m := pattern.search(low):
            return today + timedelta(days=delta), m.span()

    if m := _DAY_MONTH.search(low):
        day, month = int(m.group(1)), MONTHS.index(m.group(2)) + 1
        found = _calendar_date(day, month, m.group(3), today, prefer_future)
        if found:
            return found, m.span()

    for m in _NUMERIC_DATE.finditer(low):
        found = _calendar_date(int(m.group(1)), int(m.group(2)), m.group(3), today, prefer_future)
        if found:
            return found, m.span()

    if m := _WEEKDAY.search(low):
        weekday = WEEKDAY_BY_FORM[m.group(2)]
        modifier = m.group(1) or ""
        if modifier.startswith("следующ"):
            return week_start(today) + timedelta(days=7 + weekday), m.span()
        delta = (weekday - today.weekday()) % 7
        # "в пятницу", сказанное в пятницу, это уже следующая. а вот "в эту пятницу" - сегодня
        if delta == 0 and not modifier.startswith("эт"):
            delta = 7
        return today + timedelta(days=delta), m.span()
    return None


def _calendar_date(day: int, month: int, year: str | None, today: date, prefer_future: bool) -> date | None:
    if year:
        full_year = int(year) + (2000 if len(year) == 2 else 0)
    else:
        full_year = today.year
    try:
        found = date(full_year, month, day)
    except ValueError:
        return None  # 31.02 и прочее, такой даты нет
    # год не сказали: для новых записей берем ближайшую будущую дату, для поиска в прошлом - прошедшую
    if not year:
        if prefer_future and found < today:
            found = date(full_year + 1, month, day)
        elif not prefer_future and found > today:
            found = date(full_year - 1, month, day)
    return found


def _vague_date(low: str, today: date) -> tuple[date, Span] | None:
    """срок примерно: до конца недели, на следующей неделе, на выходных"""
    if m := _NEXT_WEEK.search(low):
        return week_start(today) + timedelta(days=7), m.span()
    if m := _END_OF_WEEK.search(low) or _THIS_WEEK.search(low):
        return week_start(today) + timedelta(days=6), m.span()
    if m := _WEEKEND.search(low):
        saturday = week_start(today) + timedelta(days=5)
        return max(saturday, today), m.span()
    return None


def find_date(text: str, today: date, prefer_future: bool = True) -> tuple[date, Span] | None:
    """дата из текста. prefer_future: "5 января" без года - ближайшее будущее, иначе прошедшее"""
    low = normalize(text)
    return _specific_date(low, today, prefer_future) or _vague_date(low, today)


def find_period(text: str, today: date, prefer_future: bool = True) -> tuple[date, date, Span] | None:
    """период для списка: день, неделя, выходные или месяц (границы включительно)"""
    low = normalize(text)
    if found := _specific_date(low, today, prefer_future):
        day, span = found
        return day, day, span
    monday = week_start(today)
    if m := _NEXT_WEEK.search(low):
        return monday + timedelta(days=7), monday + timedelta(days=13), m.span()
    if m := _END_OF_WEEK.search(low) or _THIS_WEEK.search(low):
        return monday, monday + timedelta(days=6), m.span()
    if m := _WEEKEND.search(low):
        return max(monday + timedelta(days=5), today), monday + timedelta(days=6), m.span()
    if m := _NEXT_MONTH.search(low):
        first = add_months(today.replace(day=1), 1)
        return first, add_months(first, 1) - timedelta(days=1), m.span()
    if m := _THIS_MONTH.search(low):
        last = add_months(today.replace(day=1), 1) - timedelta(days=1)
        return today, last, m.span()
    return None


# --- время ---

def find_time(text: str) -> tuple[time, time | None, Span] | None:
    """первое найденное время: (начало, конец или None, где стояло)"""
    low = normalize(text)

    # сначала "с 15 до 16:30", потом точное время, потом просто час, и в конце "утром"/"вечером"
    if m := _RANGE.search(low):
        start = _make_time(_hour(int(m.group(1)), m.group(5)), int(m.group(2) or 0))
        end = _make_time(_hour(int(m.group(3)), m.group(5)), int(m.group(4) or 0))
        if start and end:
            return start, end, m.span()

    for pattern in (_CLOCK, _CLOCK_DOT):
        if m := pattern.search(low):
            hour = int(m.group(1))
            if m.group(3):
                hour = _hour(hour, m.group(3))
            found = _make_time(hour, int(m.group(2)))
            if found:
                return found, None, m.span()

    for pattern in (_HOUR_WORDS, _HOUR, _HOUR_DAYPART):
        if m := pattern.search(low):
            found = _make_time(_hour(_number(m.group(1)), m.group(2)))
            if found:
                return found, None, m.span()

    for pattern, value in _NAMED_TIMES:
        if m := pattern.search(low):
            return value, None, m.span()
    return None


def _find_offset(low: str, now: datetime) -> tuple[datetime | None, date | None, Span] | None:
    """"через 20 минут", "через полтора часа", "через неделю" и т.п."""
    m = _OFFSET.search(low)
    if not m:
        return None
    unit, raw = m.group("unit"), m.group("n")
    if raw in ("полтора", "полторы"):
        amount = 1.5
    else:
        amount = _number(raw) if raw else 1
        if amount is None:
            return None
    if unit == "полчаса":
        return now + timedelta(minutes=30), None, m.span()
    if unit.startswith("минут"):
        return now + timedelta(minutes=amount), None, m.span()
    if unit.startswith("час"):
        return now + timedelta(minutes=round(amount * 60)), None, m.span()
    if unit.startswith(("дн", "день")):
        return None, now.date() + timedelta(days=int(amount)), m.span()
    if unit.startswith("недел"):
        return None, now.date() + timedelta(weeks=int(amount)), m.span()
    return None, add_months(now.date(), int(amount)), m.span()


def parse_when(text: str, now: datetime, prefer_future: bool = True) -> When:
    """достает из фразы все про дату и время. "через N минут/часов" задает сразу и день и время"""
    low = normalize(text)
    when = When()

    if offset := _find_offset(low, now):
        moment, day, span = offset
        when.spans.append(span)
        if moment is not None:
            when.day, when.at = moment.date(), moment.time().replace(second=0, microsecond=0)
            return when
        when.day = day

    if when.day is None and (found := find_date(low, now.date(), prefer_future)):
        when.day, span = found
        when.spans.append(span)

    if found_time := find_time(low):
        at, end_at, span = found_time
        # "12.11" можно понять и как дату и как время. если кусок уже ушел в дату, время из него не берем
        if not any(span[0] < other[1] and other[0] < span[1] for other in when.spans):
            when.at, when.end_at = at, end_at
            when.spans.append(span)
    return when


def combine(day: date | None, at: time | None, now: datetime) -> tuple[datetime | None, bool]:
    """собирает datetime из дня и времени. если есть только время, берем ближайшее такое (сегодня или завтра)"""
    if day is None and at is None:
        return None, False
    if day is None:
        moment = datetime.combine(now.date(), at)
        if moment <= now:
            moment += timedelta(days=1)
        return moment, True
    if at is None:
        return datetime.combine(day, time()), False
    return datetime.combine(day, at), True


def strip_spans(text: str, spans: list[Span]) -> str:
    """вырезает из текста найденные куски с датами и временем"""
    for start, end in sorted(spans, reverse=True):  # с конца, чтобы позиции не поехали
        text = text[:start] + " " + text[end:]
    return re.sub(r"\s+", " ", text).strip()


# --- как показывать даты пользователю ---

def short_date(day: date, today: date | None = None) -> str:
    text = f"{day.day} {MONTHS_SHORT[day.month - 1]}"
    if today and day.year != today.year:
        text += f" {day.year}"
    return text


def human_date(day: date, today: date) -> str:
    relative = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get((day - today).days)
    if relative:
        return relative
    text = f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    if day.year != today.year:
        text += f" {day.year}"
    return text


def human_datetime(moment: datetime | None, has_time: bool, today: date) -> str:
    if moment is None:
        return "без срока"
    text = human_date(moment.date(), today)
    return f"{text} в {moment:%H:%M}" if has_time else text


def day_header(day: date, today: date) -> str:
    """заголовок дня в списке: "Пн, 26 окт · сегодня" """
    text = f"{WEEKDAYS_SHORT[day.weekday()].capitalize()}, {short_date(day, today)}"
    relative = {0: "сегодня", 1: "завтра"}.get((day - today).days)
    return f"{text} · {relative}" if relative else text


def period_title(start: date, end: date, today: date) -> str:
    """название периода, которое встает после "на": "сегодня", "эту неделю (26 окт – 1 ноя)" """
    if start == end:
        relative = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get((start - today).days)
        return relative or f"{start.day} {MONTHS[start.month - 1]}"
    span = f"{short_date(start, today)} – {short_date(end, today)}"
    monday = week_start(today)
    if start.weekday() == 0 and (end - start).days == 6:
        if start == monday:
            return f"эту неделю ({span})"
        if start == monday + timedelta(days=7):
            return f"следующую неделю ({span})"
        return f"неделю {span}"
    if start.weekday() == 5 and (end - start).days == 1:
        return f"выходные ({span})"
    return f"период {span}"
