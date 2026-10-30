import asyncio
import sys

from assistant.bot.app import run
from assistant.config import load_settings


def main() -> None:
    """читаем .env и запускаем бота. без токена телеги запускать нечего"""
    settings = load_settings()
    if not settings.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass  # ctrl+c в консоли, просто выходим без простыни ошибок


if __name__ == "__main__":
    main()
