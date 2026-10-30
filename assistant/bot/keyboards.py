"""кнопки: меню внизу и данные для кнопок под сообщениями"""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

MENU_TODAY = "📅 Сегодня"
MENU_TOMORROW = "📆 Завтра"
MENU_WEEK = "🗓 Неделя"
MENU_ALL = "📋 Все записи"
MENU_HELP = "❓ Помощь"

# фильтры в разделе "Все записи"
VIEWS = (
    ("all", "Все"), ("meeting", "Встречи"), ("task", "Задачи"),
    ("reminder", "Напоминания"), ("undated", "Без срока"), ("done", "Выполненные"),
)

# данные кнопок. Telegram возвращает их при нажатии, например "item:done:12"


class ItemCb(CallbackData, prefix="item"):
    action: str  # open | done | move | delete | snooze (delete_ok - только в сообщениях старой версии)
    item_id: int


class AnswerCb(CallbackData, prefix="ans"):
    """кнопка-ответ на уточняющий вопрос. token - метка вопроса: от старого вопроса кнопка не сработает"""
    token: str
    value: str  # время ЧЧММ (без двоеточия: им aiogram разделяет поля), номер записи, yes или no


class BrowseCb(CallbackData, prefix="browse"):
    offset: int
    view: str


class ActionCb(CallbackData, prefix="act"):
    name: str  # cancel | clear_ok


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_TODAY), KeyboardButton(text=MENU_TOMORROW), KeyboardButton(text=MENU_WEEK)],
            [KeyboardButton(text=MENU_ALL), KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите или запишите голосовое",
    )
