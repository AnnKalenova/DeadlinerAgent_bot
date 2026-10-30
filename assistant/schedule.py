"""повторы (каждый день/неделю/месяц) и расчет, когда напоминать"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from .dates import add_months
from .domain import Item, ItemType, Repeat

UNTIMED_REMINDER_AT = time(9, 0)  # если у записи только день без времени, напоминаем в 9 утра этого дня
_MAX_OCCURRENCES = 2000  # ограничение, чтобы цикл по повторам не стал бесконечным


def shift(moment: datetime, repeat: Repeat, times: int = 1) -> datetime:
    """сдвигает момент на times шагов повтора. для месяца 31 число превращается в последний день месяца"""
    if repeat is Repeat.DAILY:
        return moment + timedelta(days=times)
    if repeat is Repeat.WEEKLY:
        return moment + timedelta(weeks=times)
    return datetime.combine(add_months(moment.date(), times), moment.time())


def occurrences(item: Item, start: datetime, end: datetime) -> list[datetime]:
    """все разы, когда запись случается в промежутке [start, end)"""
    if item.start is None:
        return []
    if item.repeat is None:
        return [item.start] if start <= item.start < end else []
    result = []
    for n in range(_MAX_OCCURRENCES):
        moment = shift(item.start, item.repeat, n)
        if moment >= end:
            break
        if moment >= start:
            result.append(moment)
    return result


def next_occurrence(item: Item, after: datetime) -> datetime | None:
    """следующий повтор строго после after"""
    if item.start is None or item.repeat is None:
        return None
    for n in range(1, _MAX_OCCURRENCES):
        moment = shift(item.start, item.repeat, n)
        if moment > after:
            return moment
    return None


def _lead(item: Item, meeting_lead: int) -> timedelta:
    # о встрече со временем напоминаем заранее (по умолчанию за 15 мин), об остальном - ровно в срок
    return timedelta(minutes=meeting_lead) if item.type is ItemType.MEETING and item.has_time else timedelta()


def _alert_for(item: Item, occurrence: datetime, meeting_lead: int) -> datetime:
    if not item.has_time:
        return datetime.combine(occurrence.date(), UNTIMED_REMINDER_AT)
    return occurrence - _lead(item, meeting_lead)


def reminder_time(item: Item, now: datetime, meeting_lead: int) -> datetime | None:
    """когда напомнить о записи в следующий раз. None - напоминать уже не о чем"""
    if item.start is None or item.is_done:
        return None
    if item.repeat is not None:
        occurrence = item.start
        for n in range(1, _MAX_OCCURRENCES):
            alert = _alert_for(item, occurrence, meeting_lead)
            if alert > now:
                return alert
            if item.has_time and occurrence > now:
                return now  # ближайший повтор начнется меньше чем через 15 минут: напоминаем сразу, а не завтра
            occurrence = shift(item.start, item.repeat, n)
        return None
    alert = _alert_for(item, item.start, meeting_lead)
    if alert > now:
        return alert
    if item.has_time and item.start > now:
        return now  # встреча начнется меньше чем через 15 минут, напоминаем сразу
    return None


def occurrence_for_alert(item: Item, alert: datetime, meeting_lead: int) -> datetime:
    """к какому именно повтору относится сработавшее напоминание (чтобы написать правильное время)"""
    if not item.has_time:
        return datetime.combine(alert.date(), time())
    if item.repeat is None:
        return item.start
    # последний повтор, который начинается не позже alert + lead. у обычного напоминания это ровно alert + lead,
    # а после "+15 мин" - тот повтор, о котором напоминали (иначе время встречи сдвинулось бы на 15 минут)
    target = alert + _lead(item, meeting_lead)
    found = item.start
    for n in range(1, _MAX_OCCURRENCES):
        moment = shift(item.start, item.repeat, n)
        if moment > target:
            break
        found = moment
    return found
