"""разбор через языковую модель: она выбирает инструмент и заполняет аргументы, а мы делаем из этого Command

модели не доверяем: каждый аргумент проверяется, кривая дата или неизвестный тип просто выкидываются
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Protocol

from ..domain import Command, Intent, ItemType, Repeat
from .base import ParseContext, ParserError
from .prompt import TOOLS, build_system_prompt


@dataclass
class ChatResult:
    """ответ модели: либо вызов инструмента с аргументами, либо просто текст"""

    tool: str | None = None
    arguments: dict = field(default_factory=dict)
    text: str = ""


class ChatModel(Protocol):
    """модель с вызовом инструментов. сейчас это YandexGPT из yandex.py, но можно подсунуть любую с методом chat"""

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult: ...


# дальше маленькие проверялки аргументов: если значение кривое, возвращают None


def _date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _time(value) -> time | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return time.fromisoformat(value.strip().replace(".", ":")[:5])
    except ValueError:
        return None


def _enum(enum, value):
    try:
        return enum(value) if value else None
    except ValueError:
        return None


def _number(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def to_command(result: ChatResult, user_text: str, ctx: ParseContext) -> Command:
    """переводит выбранный моделью инструмент в Command"""
    if result.tool is None:
        if result.text.strip():
            return Command(Intent.CHAT, text=result.text.strip())
        raise ParserError("empty model response")

    args = result.arguments
    if result.tool == "create_item":
        return Command(
            Intent.CREATE,
            item_type=_enum(ItemType, args.get("type")) or ItemType.TASK,
            title=_text(args.get("title")),
            day=_date(args.get("date")),
            at=_time(args.get("time")),
            end_at=_time(args.get("end_time")),
            repeat=_enum(Repeat, args.get("repeat")),
        )
    if result.tool == "list_items":
        start = _date(args.get("date_from")) or ctx.now.date()
        end = _date(args.get("date_to")) or start
        if end < start:
            start, end = end, start  # модель иногда путает местами
        return Command(Intent.LIST, period=(start, end), item_type=_enum(ItemType, args.get("type")))
    if result.tool == "search_items":
        scope = args.get("when") if args.get("when") in ("past", "future") else None
        return Command(
            Intent.SEARCH, query=_text(args.get("query")) or user_text,
            item_type=_enum(ItemType, args.get("type")), scope=scope,
        )
    if result.tool == "update_item":
        return Command(
            Intent.UPDATE, ref=_number(args.get("item_number")), query=_text(args.get("query")),
            day=_date(args.get("date")), at=_time(args.get("time")), title=_text(args.get("title")),
        )
    targeted = {"delete_item": Intent.DELETE, "complete_item": Intent.COMPLETE}
    if result.tool in targeted:
        return Command(targeted[result.tool], ref=_number(args.get("item_number")), query=_text(args.get("query")))
    raise ParserError(f"unknown tool {result.tool!r}")


class LLMParser:
    """разборщик через языковую модель"""

    def __init__(self, model: ChatModel, history_size: int = 6):
        self._model = model
        self._history_size = history_size

    async def parse(self, text: str, ctx: ParseContext) -> Command:
        messages = [{"role": "system", "content": build_system_prompt(ctx)}]
        # несколько последних реплик, чтобы модель понимала "а перенеси ее на завтра"
        messages += [{"role": role, "content": content} for role, content in ctx.history[-self._history_size:]]
        messages.append({"role": "user", "content": text})
        try:
            result = await self._model.chat(messages, TOOLS)
        except Exception as exc:
            raise ParserError(f"model call failed: {exc}") from exc
        return to_command(result, text, ctx)
