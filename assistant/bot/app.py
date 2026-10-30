"""тут собираем все части вместе и запускаем бота

бот сам опрашивает телегу (long polling), поэтому белый ip, домен и https не нужны
"""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from ..config import Settings
from ..nlu import FallbackParser, LLMParser, RuleParser
from ..reminders import ReminderService
from ..search import Searcher
from ..service import Assistant
from ..storage import Storage
from ..yandex import SpeechKit, YandexEmbedder, YandexGPT
from . import handlers
from .middleware import UserMiddleware
from .notifier import TelegramNotifier

log = logging.getLogger(__name__)

# меню команд, которое видно в телеге по кнопке "/"
COMMANDS = [
    BotCommand(command="today", description="План на сегодня"),
    BotCommand(command="tomorrow", description="План на завтра"),
    BotCommand(command="week", description="План на неделю"),
    BotCommand(command="all", description="Все записи по неделям"),
    BotCommand(command="voice", description="Включить или выключить ответы голосом"),
    BotCommand(command="digest", description="Утренняя сводка"),
    BotCommand(command="demo", description="Загрузить демо-данные"),
    BotCommand(command="clear", description="Удалить все записи"),
    BotCommand(command="help", description="Что я умею"),
]


def setup_logging(settings: Settings) -> None:
    """лог пишем и в консоль, и в logs/bot.log. файл режется по 2 МБ, хранятся 3 старых куска"""
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(settings.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )
    # сетевые библиотеки пишут строку на каждый http запрос, в логе от этого только мусор
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run(settings: Settings) -> None:
    setup_logging(settings)
    clock = settings.clock()
    db = Storage(settings.db_path)

    speech = None
    embedder = None
    # без ключей яндекса бот тоже работает: на правилах, без голоса и без поиска по смыслу
    if settings.yandex_enabled:
        llm = YandexGPT(settings.yandex_api_key, settings.yandex_folder_id, settings.llm_model)
        parser = FallbackParser(LLMParser(llm), RuleParser())  # модель, а если упала - правила
        embedder = YandexEmbedder(settings.yandex_api_key, settings.yandex_folder_id)
        speech = SpeechKit(settings.yandex_api_key, settings.tts_voice)
        log.info("LLM %s, embeddings and SpeechKit enabled", settings.llm_model)
    else:
        parser = RuleParser()
        log.warning("Yandex Cloud keys are not set: rule parser only, keyword search, no voice")

    searcher = Searcher(db, embedder, settings.semantic_threshold)
    assistant = Assistant(
        db, parser, searcher, clock,
        meeting_minutes=settings.meeting_minutes,
        reminder_lead_minutes=settings.meeting_reminder_minutes,
    )

    # прокси нужен, только если с сервера нет прямого доступа к api.telegram.org (например сервер в рф)
    session = None
    if settings.telegram_proxy:
        session = AiohttpSession(proxy=settings.telegram_proxy)
        log.info("Telegram API via proxy")
    bot = Bot(settings.telegram_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # все что передали в Dispatcher, aiogram сам подставит в обработчики по имени параметра
    dispatcher = Dispatcher(
        assistant=assistant, db=db, searcher=searcher, speech=speech, settings=settings, clock=clock,
    )
    middleware = UserMiddleware(db)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)
    dispatcher.include_router(handlers.router)

    reminders = ReminderService(
        db, assistant, TelegramNotifier(bot, clock), clock,
        meeting_lead=settings.meeting_reminder_minutes, digest_time=settings.digest_time,
    )
    reminder_task = asyncio.create_task(reminders.run())  # напоминания крутятся в фоне рядом с ботом
    try:
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        # по этой строке deploy/install.sh понимает что бот запустился, ее не менять
        log.info("Bot @%s is running", me.username)
        await dispatcher.start_polling(bot)
    finally:
        reminder_task.cancel()
        if speech:
            await speech.close()
        await bot.session.close()
        db.close()
