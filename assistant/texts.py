"""тексты для пользователя, которые нужны и в сервисе и в боте"""
from .domain import ItemType, Repeat

# названия типов в нужных падежах: "записал задачу", "задачи на сегодня", "на завтра встреч нет"
TYPE_ACCUSATIVE = {ItemType.TASK: "задачу", ItemType.REMINDER: "напоминание", ItemType.MEETING: "встречу"}
TYPE_PLURAL = {ItemType.TASK: "Задачи", ItemType.REMINDER: "Напоминания", ItemType.MEETING: "Встречи"}
TYPE_GENITIVE_PLURAL = {ItemType.TASK: "задач", ItemType.REMINDER: "напоминаний", ItemType.MEETING: "встреч"}
TYPE_ICONS = {ItemType.TASK: "📌", ItemType.REMINDER: "⏰", ItemType.MEETING: "🤝"}
REPEAT_NAMES = {Repeat.DAILY: "каждый день", Repeat.WEEKLY: "каждую неделю", Repeat.MONTHLY: "каждый месяц"}

EXAMPLES = (
    "• Напомни завтра в 10 утра позвонить Ивану\n"
    "• В четверг в 11 встреча с дизайнером\n"
    "• Поставь задачу купить билеты до конца недели\n"
    "• Что у меня на этой неделе?\n"
    "• Найди задачу про договор\n"
    "• Перенеси её на пятницу в 12:00"
)

HELP = (
    "Я веду ваши задачи, напоминания и встречи. Пишите или отправляйте голосовые "
    "в свободной форме, например:\n\n" + EXAMPLES + "\n\n"
    "Кнопки внизу показывают план на сегодня, завтра и неделю, а «Все записи» — "
    "календарь по неделям с фильтрами.\n\n"
    "/voice — отвечать голосом\n"
    "/digest — утренняя сводка\n"
    "/demo — загрузить демо-данные\n"
    "/clear — удалить все мои записи"
)


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 запись, 2 записи, 5 записей (11-19 всегда "записей")"""
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many
