"""команда /demo: загружает csv из задания (id, date, time, text) в записи пользователя.
нужно, чтобы было на чем проверить поиск по старым записям"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from .domain import Item, ItemType, Status
from .storage import Storage

ORIGIN = "demo"  # так помечаем загруженные записи, чтобы потом заменить или удалить только их
_MEETING = re.compile(r"^(?:встреча|созвон|демо|провести\s+планерку|обсудить\s.+\sс\s)", re.IGNORECASE)
_REMINDER = re.compile(r"^напомнить\b", re.IGNORECASE)


def guess_type(text: str) -> ItemType:
    """в файле нет типа записи, поэтому определяем его по началу фразы"""
    normalized = text.replace("ё", "е")
    if _MEETING.search(normalized):
        return ItemType.MEETING
    if _REMINDER.search(normalized):
        return ItemType.REMINDER
    return ItemType.TASK


def read_csv(path: Path, user_id: int, now: datetime) -> list[Item]:
    items = []
    with open(path, encoding="utf-8-sig", newline="") as file:  # utf-8-sig: в файле есть BOM
        for row in csv.DictReader(file):
            title = row["text"].strip()
            item_type = guess_type(title)
            start = datetime.fromisoformat(f"{row['date']}T{row['time']}")
            items.append(Item(
                user_id=user_id,
                type=item_type,
                title=title,
                start=start,
                has_time=True,
                # прошедшие задачи считаем выполненными. по демо-записям напоминания не отправляем (notified=True)
                status=Status.DONE if start < now and item_type is not ItemType.MEETING else Status.OPEN,
                notified=True,
                origin=ORIGIN,
                source_text=title,
            ))
    return items


def import_demo(storage: Storage, user_id: int, path: Path, now: datetime) -> list[Item]:
    """старые демо-записи пользователя удаляем и загружаем заново. его собственные записи не трогаем"""
    items = read_csv(path, user_id, now)
    storage.delete_user_items(user_id, origin=ORIGIN)
    storage.add_items(items)
    return items
