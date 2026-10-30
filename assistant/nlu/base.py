"""общее для всех разборщиков (модель и правила)"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..domain import Command, Item

log = logging.getLogger(__name__)


class ParserError(Exception):
    """разборщик не смог вернуть команду: нет сети, модель вернула некорректный ответ и т.п."""


@dataclass
class ParseContext:
    """все, что передается разборщику вместе с фразой"""

    now: datetime
    recent: list[Item] = field(default_factory=list)  # записи, на которые можно сослаться номером
    history: list[tuple[str, str]] = field(default_factory=list)  # (роль, текст), от старых к новым


class Parser(Protocol):
    """разборщик: фраза пользователя -> Command"""

    async def parse(self, text: str, ctx: ParseContext) -> Command: ...


class FallbackParser:
    """сначала пробует основной разборщик (модель), а при его ошибке - запасной (правила).
    пользователь ошибку не видит, в лог пишется Primary parser failed"""

    def __init__(self, primary: Parser, fallback: Parser):
        self.primary = primary
        self.fallback = fallback

    async def parse(self, text: str, ctx: ParseContext) -> Command:
        try:
            return await self.primary.parse(text, ctx)
        except ParserError as exc:
            log.warning("Primary parser failed, using rules: %s", exc)
            return await self.fallback.parse(text, ctx)
