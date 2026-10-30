"""замер того, насколько хорошо бот понимает фразы. к работе бота не относится, запускается вручную

    python -m eval.run_eval              # правила
    python -m eval.run_eval --parser llm # языковая модель (нужны ключи в .env)

в каждой строке phrases.jsonl фраза, что должно получиться (действие) и какие параметры проверить.
фраза считается понятой, если совпало действие и все указанные параметры.
"сегодня" здесь зафиксировано: понедельник, 26 октября 2026, 10:00, иначе ожидаемые даты бы смещались
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from assistant.config import load_settings
from assistant.domain import Command, Item, ItemType
from assistant.nlu import LLMParser, ParseContext, Parser, RuleParser

PHRASES = Path(__file__).with_name("phrases.jsonl")
NOW = datetime(2026, 10, 26, 10, 0)
# записи, которые пользователь как будто только что видел. на них ссылаются фразы вроде "удали вторую"
RECENT = [
    Item(id=1, user_id=0, type=ItemType.MEETING, title="Встреча с Иваном", start=datetime(2026, 10, 27, 15), has_time=True),
    Item(id=2, user_id=0, type=ItemType.TASK, title="Подготовить договор", start=datetime(2026, 10, 28)),
    Item(id=3, user_id=0, type=ItemType.TASK, title="Отправить отчёт", start=None),
]


def actual_slots(cmd: Command) -> dict:
    return {
        "type": cmd.item_type.value if cmd.item_type else None,
        "title": cmd.title,
        "date": cmd.day.isoformat() if cmd.day else None,
        "time": cmd.at.strftime("%H:%M") if cmd.at else None,
        "end_time": cmd.end_at.strftime("%H:%M") if cmd.end_at else None,
        "repeat": cmd.repeat.value if cmd.repeat else None,
        "period": [cmd.period[0].isoformat(), cmd.period[1].isoformat()] if cmd.period else None,
        "query": cmd.query,
        "ref": cmd.ref,
        "scope": cmd.scope,
    }


def mismatches(case: dict, cmd: Command) -> list[str]:
    problems = []
    if cmd.intent.value not in case["intent"].split("|"):
        problems.append(f"intent={cmd.intent.value}")
    actual = actual_slots(cmd)
    for slot, expected in case.items():
        if slot in ("text", "intent"):
            continue
        value = actual[slot]
        if slot in ("title", "query"):
            ok = expected is None and value is None or (
                expected is not None and value is not None and expected.lower() in value.lower().replace("ё", "е")
            )
        else:
            ok = value == expected
        if not ok:
            problems.append(f"{slot}={value!r} (ждали {expected!r})")
    return problems


async def evaluate(parser: Parser) -> None:
    cases = [json.loads(line) for line in PHRASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    passed: Counter = Counter()
    total: Counter = Counter()
    failures = []
    for case in cases:
        cmd = await parser.parse(case["text"], ParseContext(now=NOW, recent=RECENT))
        group = case["intent"].split("|")[0]
        total[group] += 1
        problems = mismatches(case, cmd)
        if problems:
            failures.append((case["text"], problems))
        else:
            passed[group] += 1

    for text, problems in failures:
        print(f"✗ {text}\n    {'; '.join(problems)}")
    print("\nПо типам запросов:")
    for group in total:
        print(f"  {group:<9} {passed[group]:>2}/{total[group]}")
    print(f"\nИтого: {sum(passed.values())}/{sum(total.values())} "
          f"({100 * sum(passed.values()) / sum(total.values()):.0f}%)")


def build_parser(kind: str) -> Parser:
    if kind == "rules":
        return RuleParser()
    from assistant.yandex import YandexGPT

    settings = load_settings()
    if not settings.yandex_enabled:
        raise SystemExit("Для --parser llm нужны YANDEX_API_KEY и YANDEX_FOLDER_ID в .env")
    return LLMParser(YandexGPT(settings.yandex_api_key, settings.yandex_folder_id, settings.llm_model))


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--parser", choices=["rules", "llm"], default="rules")
    asyncio.run(evaluate(build_parser(args.parse_args().parser)))
