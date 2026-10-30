"""быстрая проверка новой версии перед тем как ее включить. запускает deploy/install.sh

это не тесты, а просто защита от того, чтобы выкатить сломанное:
- все модули импортируются (значит зависимости встали и в коде нет синтаксических ошибок)
- .env читается и значения в нем нормальные (числа, время, часовой пояс)
- бот на правилах понимает одну фразу и сохраняет запись (база в памяти, сеть не нужна)

    .venv/bin/python deploy/check.py путь/к/.env
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        # .env, с которым бот будет работать на сервере. load_settings ниже эти значения уже не перебьет
        load_dotenv(sys.argv[1])

    import main as entry  # noqa: F401  тянет за собой все модули бота
    from assistant.config import load_settings
    from assistant.nlu import RuleParser
    from assistant.search import Searcher
    from assistant.service import Assistant
    from assistant.storage import Storage

    settings = load_settings()  # упадет, если в .env кривые числа или время
    if not settings.telegram_token:
        sys.exit("в .env нет TELEGRAM_BOT_TOKEN")
    settings.clock()()  # упадет, если кривой TIMEZONE

    now = datetime(2026, 10, 26, 10, 0)  # понедельник, чтобы "завтра" всегда было одним и тем же днем
    db = Storage(":memory:")
    assistant = Assistant(db, RuleParser(), Searcher(db), lambda: now)
    reply = asyncio.run(assistant.handle(1, "напомни завтра в 10 позвонить Ивану"))
    if not reply.card or reply.card.start != datetime(2026, 10, 27, 10, 0):
        sys.exit(f"бот не понял проверочную фразу, ответил: {reply.text}")
    print("ok")


if __name__ == "__main__":
    main()
