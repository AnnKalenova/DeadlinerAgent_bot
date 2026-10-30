"""обработчики Telegram: команды, кнопки меню, текст и голосовые, кнопки под сообщениями

логики здесь почти нет: получить текст, передать ассистенту и показать ответ
"""
from __future__ import annotations

import asyncio
import io
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from ..config import Settings
from ..demo_data import ORIGIN as DEMO_ORIGIN
from ..demo_data import import_demo
from ..domain import Reply
from ..search import Searcher
from ..service import Assistant
from ..storage import Storage, User
from ..texts import EXAMPLES, HELP, plural
from ..yandex import SpeechKit
from .keyboards import (MENU_ALL, MENU_HELP, MENU_TODAY, MENU_TOMORROW, MENU_WEEK, ActionCb, AnswerCb, BrowseCb,
                        ItemCb, main_menu)
from .render import render, speech_text

log = logging.getLogger(__name__)
router = Router()

VOICE_MAX_SECONDS = 30  # больше SpeechKit за один запрос не распознает
# ссылки на фоновые задачи: без них Python может удалить задачу из памяти до ее завершения
_background: set[asyncio.Task] = set()


async def answer(message: Message, reply: Reply, clock) -> None:
    """отправляет ответ новым сообщением (только текст)"""
    text, markup = render(reply, clock().date())
    await message.answer(text, reply_markup=markup)


async def send_reply(message: Message, reply: Reply, user: User, clock, speech: SpeechKit | None) -> None:
    """ответ текстом, а если включен /voice - еще и голосом"""
    await answer(message, reply, clock)
    if user.voice_replies and speech:
        try:
            audio = await speech.synthesize(speech_text(reply, clock().date()))
            await message.answer_voice(BufferedInputFile(audio, filename="reply.ogg"))
        except Exception as exc:
            log.warning("Voice reply failed: %s", exc)  # текст уже отправлен, поэтому ошибку голоса только пишем в лог


async def edit_reply(callback: CallbackQuery, reply: Reply, clock) -> None:
    """показывает результат прямо в том сообщении, под которым нажали кнопку"""
    text, markup = render(reply, clock().date())
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return  # повторное нажатие той же кнопки: менять нечего, новое сообщение не отправляем
        # сообщение слишком старое и его нельзя редактировать - отправляем новое
        await callback.message.answer(text, reply_markup=markup)


# --- команды и кнопки меню внизу ---

@router.message(CommandStart())
async def on_start(message: Message, user: User):
    name = escape(message.from_user.first_name or "")
    await message.answer(
        f"Привет, {name}! Я веду задачи, напоминания и встречи. Пишите или говорите как удобно, например:\n\n"
        f"{EXAMPLES}\n\n"
        "Сам напомню о делах в назначенное время, а по утрам пришлю план на день. "
        "Чтобы попробовать поиск по истории, загрузите демо-данные: /demo",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
@router.message(F.text == MENU_HELP)
async def on_help(message: Message):
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("today"))
@router.message(F.text == MENU_TODAY)
async def on_today(message: Message, assistant: Assistant, user: User, clock, speech):
    await send_reply(message, assistant.agenda(user.user_id, "today"), user, clock, speech)


@router.message(Command("tomorrow"))
@router.message(F.text == MENU_TOMORROW)
async def on_tomorrow(message: Message, assistant: Assistant, user: User, clock, speech):
    await send_reply(message, assistant.agenda(user.user_id, "tomorrow"), user, clock, speech)


@router.message(Command("week"))
@router.message(F.text == MENU_WEEK)
async def on_week(message: Message, assistant: Assistant, user: User, clock, speech):
    await send_reply(message, assistant.agenda(user.user_id, "week"), user, clock, speech)


@router.message(Command("all"))
@router.message(F.text == MENU_ALL)
async def on_all(message: Message, assistant: Assistant, user: User, clock):
    await answer(message, assistant.browse(user.user_id), clock)


@router.message(Command("voice"))
async def on_voice_toggle(message: Message, db: Storage, user: User, speech):
    if speech is None:
        await message.answer("Голосовые ответы недоступны: не настроены ключи Yandex Cloud.")
        return
    db.update_user(user.user_id, voice_replies=not user.voice_replies)
    await message.answer("Буду отвечать и голосом." if not user.voice_replies else "Отвечаю только текстом.")


@router.message(Command("digest"))
async def on_digest_toggle(message: Message, db: Storage, user: User, settings: Settings):
    db.update_user(user.user_id, digest=not user.digest)
    if user.digest:
        await message.answer("Утреннюю сводку больше не присылаю.")
    else:
        await message.answer(f"Буду присылать план на день в {settings.digest_time:%H:%M}.")


@router.message(Command("demo"))
async def on_demo(message: Message, db: Storage, searcher: Searcher, settings: Settings, user: User, clock):
    if not settings.demo_csv.exists():
        await message.answer("Файл с демо-данными не найден. Положите CSV в папку data (путь задаётся в DEMO_CSV).")
        return
    already = db.count_items(user.user_id, origin=DEMO_ORIGIN)
    items = import_demo(db, user.user_id, settings.demo_csv, clock())
    if not items:
        await message.answer("В файле с демо-данными нет записей.")
        return
    # эмбеддинги для поиска считаются в фоне, чтобы пользователь не ждал ответа
    task = asyncio.create_task(searcher.warm_up([item.title for item in items]))
    _background.add(task)
    task.add_done_callback(_background.discard)

    first, last = items[0].start.year, items[-1].start.year
    action = "Обновил" if already else "Загрузил"
    count = len(items)
    await message.answer(
        f"{action} {count} {plural(count, 'демо-запись', 'демо-записи', 'демо-записей')} за {first}–{last} годы. "
        "Попробуйте:\n\n• Когда у меня была встреча с Иваном?\n• Найди задачу про договор\n"
        "• Что у меня на следующей неделе?\n\nУдалить всё: /clear"
    )


@router.message(Command("clear"))
async def on_clear(message: Message, db: Storage, user: User):
    count = db.count_items(user.user_id)
    if not count:
        await message.answer("Записей пока нет.")
        return
    await message.answer(
        f"Удалить все записи ({count})? Это нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить всё", callback_data=ActionCb(name="clear_ok").pack()),
            InlineKeyboardButton(text="Отмена", callback_data=ActionCb(name="cancel").pack()),
        ]]),
    )


# --- обычные сообщения: голос и текст ---

@router.message(F.voice)
async def on_voice(message: Message, bot: Bot, assistant: Assistant, user: User, clock, speech: SpeechKit | None):
    if speech is None:
        await message.answer("Голосовые пока недоступны: не настроены ключи Yandex Cloud. Напишите текстом.")
        return
    if message.voice.duration > VOICE_MAX_SECONDS:
        await message.answer("Голосовое длиннее 30 секунд. Скажите, пожалуйста, короче или напишите текстом.")
        return
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    buffer = io.BytesIO()
    await bot.download(message.voice, destination=buffer)
    try:
        text = await speech.recognize(buffer.getvalue())
    except Exception as exc:
        log.warning("Speech recognition failed: %s", exc)
        await message.answer("Не получилось распознать речь. Попробуйте ещё раз или напишите текстом.")
        return
    if not text:
        await message.answer("Не расслышал. Повторите, пожалуйста.")
        return
    await message.answer(f"🎤 <i>{escape(text)}</i>")  # показываем распознанный текст, чтобы было видно, если речь распознана неточно
    await send_reply(message, await assistant.handle(user.user_id, text), user, clock, speech)


@router.message(F.text)
async def on_text(message: Message, bot: Bot, assistant: Assistant, user: User, clock, speech):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await send_reply(message, await assistant.handle(user.user_id, message.text), user, clock, speech)


@router.message()
async def on_other(message: Message):
    # стикеры, фото и другие типы сообщений
    await message.answer("Я понимаю текст и голосовые сообщения.")


# --- кнопки под сообщениями ---

@router.callback_query(ItemCb.filter())
async def on_item_button(callback: CallbackQuery, callback_data: ItemCb, assistant: Assistant, user: User, clock):
    item_id, action = callback_data.item_id, callback_data.action
    await callback.answer()  # убирает индикатор загрузки на кнопке
    if action == "open":
        await answer(callback.message, assistant.open_item(user.user_id, item_id), clock)
    elif action == "done":
        await edit_reply(callback, assistant.complete(user.user_id, item_id), clock)
    elif action == "snooze":
        await edit_reply(callback, assistant.snooze(user.user_id, item_id), clock)
    elif action == "move":
        await answer(callback.message, assistant.start_reschedule(user.user_id, item_id), clock)
    elif action == "delete":
        await answer(callback.message, assistant.ask_delete(user.user_id, item_id), clock)
    elif action == "delete_ok":
        # "Да, удалить" из сообщения старой версии бота: кнопка не привязана к вопросу, поэтому ничего не удаляем
        await edit_reply(callback, Reply("Эта кнопка устарела. Если нужно удалить запись, попросите ещё раз."), clock)


_YES_NO = {"yes": "да", "no": "нет"}


@router.callback_query(AnswerCb.filter())
async def on_answer_button(callback: CallbackQuery, callback_data: AnswerCb, assistant: Assistant, user: User,
                           clock, speech):
    # кнопки под уточняющим вопросом работают так же, как ответ текстом: "в 15:00", "2", "да"
    await callback.answer()
    value = callback_data.value
    if value in _YES_NO:
        text = _YES_NO[value]
    elif len(value) == 4 and value.isdigit():
        text = f"в {value[:2]}:{value[2:]}"  # кнопка времени: "1500" -> "в 15:00"
    else:
        text = value  # номер записи из списка
    reply = await assistant.answer_button(user.user_id, callback_data.token, text)
    if value in _YES_NO:
        await edit_reply(callback, reply, clock)  # результат удаления показываем на месте вопроса
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # убираем кнопки, чтобы их не нажали повторно
    except TelegramBadRequest:
        pass
    await send_reply(callback.message, reply, user, clock, speech)


@router.callback_query(BrowseCb.filter())
async def on_browse(callback: CallbackQuery, callback_data: BrowseCb, assistant: Assistant, user: User, clock):
    await callback.answer()
    await edit_reply(callback, assistant.browse(user.user_id, callback_data.offset, callback_data.view), clock)


@router.callback_query(ActionCb.filter(F.name == "clear_ok"))
async def on_clear_confirmed(callback: CallbackQuery, assistant: Assistant, user: User, clock):
    removed = assistant.clear(user.user_id)
    await callback.answer()
    await edit_reply(callback, Reply(f"Удалил записи: {removed}."), clock)


@router.callback_query(ActionCb.filter(F.name == "cancel"))
async def on_cancel(callback: CallbackQuery, assistant: Assistant, user: User, clock):
    await callback.answer()
    await edit_reply(callback, assistant.cancel(user.user_id), clock)


@router.callback_query()
async def on_old_button(callback: CallbackQuery):
    # кнопки из старых сообщений, формат которых бот уже не знает
    await callback.answer("Эта кнопка устарела")


@router.error()
async def on_error(event: ErrorEvent):
    # необработанные ошибки: полный текст в лог, пользователю - короткое сообщение
    log.exception("Unhandled error", exc_info=event.exception)
    message = event.update.message or (event.update.callback_query and event.update.callback_query.message)
    if message:
        try:
            await message.answer("Что-то пошло не так. Попробуйте ещё раз.")
        except Exception:
            pass
