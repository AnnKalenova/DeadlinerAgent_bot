"""база sqlite: записи, пользователи, состояние диалога и кеш эмбеддингов

весь sql тут. остальной код работает с Item и User и про таблицы не знает
"""
from __future__ import annotations

import json
import sqlite3
from array import array
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .domain import Item, ItemType, Repeat, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('task', 'reminder', 'meeting')),
    title       TEXT NOT NULL,
    start_at    TEXT,                 -- местное время в ISO, NULL если у записи нет даты
    end_at      TEXT,
    has_time    INTEGER NOT NULL DEFAULT 0,  -- 0: есть только дата, время не говорили
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    repeat      TEXT CHECK (repeat IN ('daily', 'weekly', 'monthly')),
    remind_at   TEXT,                 -- когда отправить следующее напоминание
    notified    INTEGER NOT NULL DEFAULT 0,  -- напоминание уже отправлено
    origin      TEXT NOT NULL DEFAULT 'chat',  -- chat или demo (записи из csv)
    source_text TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_user_start ON items (user_id, start_at);
CREATE INDEX IF NOT EXISTS idx_items_reminders ON items (notified, remind_at);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    name          TEXT,
    voice_replies INTEGER NOT NULL DEFAULT 0,
    digest        INTEGER NOT NULL DEFAULT 1,
    last_digest   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dialog_state (
    user_id INTEGER PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, id);

CREATE TABLE IF NOT EXISTS embeddings (
    key    TEXT PRIMARY KEY,
    vector BLOB NOT NULL
);
"""

_HISTORY_KEEP = 40  # сколько последних реплик диалога держим на пользователя, старое удаляем

# один INSERT на всех: и для одной записи, и для пачки из /demo
_ITEM_COLUMNS = ("user_id", "type", "title", "start_at", "end_at", "has_time", "status", "repeat",
                 "remind_at", "notified", "origin", "source_text", "created_at", "updated_at")
_INSERT_ITEM = f"INSERT INTO items ({', '.join(_ITEM_COLUMNS)}) VALUES ({', '.join('?' * len(_ITEM_COLUMNS))})"


@dataclass
class User:
    """пользователь телеги и его настройки"""

    user_id: int
    name: str | None
    voice_replies: bool
    digest: bool
    last_digest: date | None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="minutes") if moment else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _item_values(item: Item, now: str) -> tuple:
    # порядок как в _ITEM_COLUMNS
    return (item.user_id, item.type.value, item.title, _iso(item.start), _iso(item.end), int(item.has_time),
            item.status.value, item.repeat.value if item.repeat else None, _iso(item.remind_at),
            int(item.notified), item.origin, item.source_text, now, now)


def _item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        user_id=row["user_id"],
        type=ItemType(row["type"]),
        title=row["title"],
        start=_dt(row["start_at"]),
        end=_dt(row["end_at"]),
        has_time=bool(row["has_time"]),
        status=Status(row["status"]),
        repeat=Repeat(row["repeat"]) if row["repeat"] else None,
        remind_at=_dt(row["remind_at"]),
        notified=bool(row["notified"]),
        origin=row["origin"],
        source_text=row["source_text"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        name=row["name"],
        voice_replies=bool(row["voice_replies"]),
        digest=bool(row["digest"]),
        last_digest=date.fromisoformat(row["last_digest"]) if row["last_digest"] else None,
    )


class Storage:
    """все запросы к базе"""

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # режим WAL: чтение и запись меньше мешают друг другу
        self._conn.executescript(SCHEMA)  # таблицы создаются сами при первом запуске

    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, args).fetchall()

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._conn:  # with = коммит, а если ошибка то откат
            return self._conn.execute(sql, args)

    # --- записи ---

    def add_item(self, item: Item) -> Item:
        cursor = self._write(_INSERT_ITEM, _item_values(item, _now()))
        return self.get_item(item.user_id, cursor.lastrowid)

    def add_items(self, items: list[Item]) -> int:
        now = _now()
        with self._conn:
            self._conn.executemany(_INSERT_ITEM, [_item_values(item, now) for item in items])
        return len(items)

    def save_item(self, item: Item) -> Item:
        self._write(
            "UPDATE items SET type = ?, title = ?, start_at = ?, end_at = ?, has_time = ?, status = ?,"
            " repeat = ?, remind_at = ?, notified = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (item.type.value, item.title, _iso(item.start), _iso(item.end), int(item.has_time),
             item.status.value, item.repeat.value if item.repeat else None, _iso(item.remind_at),
             int(item.notified), _now(), item.id, item.user_id),
        )
        return self.get_item(item.user_id, item.id)

    # везде проверяем и user_id тоже, чтобы никто не мог открыть чужую запись по номеру
    def get_item(self, user_id: int, item_id: int) -> Item | None:
        rows = self._query("SELECT * FROM items WHERE id = ? AND user_id = ?", (item_id, user_id))
        return _item(rows[0]) if rows else None

    def delete_item(self, user_id: int, item_id: int) -> None:
        self._write("DELETE FROM items WHERE id = ? AND user_id = ?", (item_id, user_id))

    def delete_user_items(self, user_id: int, origin: str | None = None) -> int:
        if origin:
            return self._write("DELETE FROM items WHERE user_id = ? AND origin = ?", (user_id, origin)).rowcount
        return self._write("DELETE FROM items WHERE user_id = ?", (user_id,)).rowcount

    def items_between(self, user_id: int, start: datetime, end: datetime) -> list[Item]:
        """разовые записи, которые начинаются в [start, end), и все повторяющиеся, начавшиеся до end.
        повторы потом раскладываются по датам в schedule.occurrences"""
        rows = self._query(
            "SELECT * FROM items WHERE user_id = ? AND ("
            " (repeat IS NULL AND start_at >= ? AND start_at < ?)"
            " OR (repeat IS NOT NULL AND start_at < ?)) ORDER BY start_at",
            (user_id, _iso(start), _iso(end), _iso(end)),
        )
        return [_item(r) for r in rows]

    def undated_items(self, user_id: int) -> list[Item]:
        rows = self._query(
            "SELECT * FROM items WHERE user_id = ? AND start_at IS NULL AND status = 'open' ORDER BY id",
            (user_id,),
        )
        return [_item(r) for r in rows]

    def done_items(self, user_id: int, limit: int = 30) -> list[Item]:
        rows = self._query(
            "SELECT * FROM items WHERE user_id = ? AND status = 'done' ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [_item(r) for r in rows]

    def all_items(self, user_id: int) -> list[Item]:
        rows = self._query("SELECT * FROM items WHERE user_id = ? ORDER BY start_at", (user_id,))
        return [_item(r) for r in rows]

    def count_items(self, user_id: int, origin: str | None = None) -> int:
        if origin:
            rows = self._query("SELECT COUNT(*) FROM items WHERE user_id = ? AND origin = ?", (user_id, origin))
        else:
            rows = self._query("SELECT COUNT(*) FROM items WHERE user_id = ?", (user_id,))
        return rows[0][0]

    def due_reminders(self, now: datetime) -> list[Item]:
        """напоминания, время которых уже наступило, а отправить их еще не успели"""
        rows = self._query(
            "SELECT * FROM items WHERE notified = 0 AND status = 'open'"
            " AND remind_at IS NOT NULL AND remind_at <= ? ORDER BY remind_at",
            (_iso(now),),
        )
        return [_item(r) for r in rows]

    # --- пользователи ---

    def upsert_user(self, user_id: int, name: str | None) -> User:
        self._write(
            "INSERT INTO users (user_id, name, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET name = excluded.name",
            (user_id, name, _now()),
        )
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> User | None:
        rows = self._query("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    def update_user(self, user_id: int, **fields) -> None:
        # имена колонок подставляются в sql, поэтому пускаем только известные
        allowed = {"voice_replies", "digest", "last_digest"}
        values = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items() if k in allowed}
        if values:
            assignments = ", ".join(f"{k} = ?" for k in values)
            self._write(f"UPDATE users SET {assignments} WHERE user_id = ?", (*values.values(), user_id))

    def digest_users(self) -> list[User]:
        return [_user(r) for r in self._query("SELECT * FROM users WHERE digest = 1")]

    # --- диалог ---

    def load_state(self, user_id: int) -> dict:
        rows = self._query("SELECT data FROM dialog_state WHERE user_id = ?", (user_id,))
        return json.loads(rows[0]["data"]) if rows else {}

    def save_state(self, user_id: int, data: dict) -> None:
        self._write(
            "INSERT INTO dialog_state (user_id, data) VALUES (?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET data = excluded.data",
            (user_id, json.dumps(data, ensure_ascii=False)),
        )

    def add_message(self, user_id: int, role: str, text: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO messages (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, text, _now()),
            )
            self._conn.execute(
                "DELETE FROM messages WHERE user_id = ? AND id NOT IN"
                " (SELECT id FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
                (user_id, user_id, _HISTORY_KEEP),
            )

    def recent_messages(self, user_id: int, limit: int) -> list[tuple[str, str]]:
        rows = self._query(
            "SELECT role, text FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def clear_dialog(self, user_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM dialog_state WHERE user_id = ?", (user_id,))

    # --- кеш эмбеддингов, чтобы не платить яндексу за одно и то же ---

    def get_embeddings(self, keys: list[str]) -> dict[str, list[float]]:
        found = {}
        # в sqlite есть лимит на число параметров в запросе, поэтому читаем кусками по 500
        for chunk_start in range(0, len(keys), 500):
            chunk = keys[chunk_start:chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._query(f"SELECT key, vector FROM embeddings WHERE key IN ({marks})", chunk):
                found[row["key"]] = array("f", row["vector"]).tolist()
        return found

    def put_embedding(self, key: str, vector: list[float]) -> None:
        self._write(
            "INSERT OR REPLACE INTO embeddings (key, vector) VALUES (?, ?)",
            (key, array("f", vector).tobytes()),
        )
