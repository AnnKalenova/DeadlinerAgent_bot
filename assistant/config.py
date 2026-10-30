"""настройки бота из .env

если переменная уже задана в окружении, она важнее значения из .env
(на сервере так задаются DB_PATH и LOG_PATH, см. deploy/tg-assistant.service)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # корень проекта, здесь лежат .env, data и logs


@dataclass(frozen=True)
class Settings:
    """все настройки бота. значения по умолчанию в load_settings ниже и в .env.example"""

    telegram_token: str
    yandex_api_key: str
    yandex_folder_id: str
    llm_model: str
    tts_voice: str
    timezone: str
    db_path: Path
    demo_csv: Path
    log_path: Path
    meeting_minutes: int
    meeting_reminder_minutes: int
    digest_time: time
    semantic_threshold: float
    telegram_proxy: str = ""  # socks5://... или http://..., если с сервера не открывается api.telegram.org

    @property
    def yandex_enabled(self) -> bool:
        # нет ключа или каталога - значит бот работает только на правилах
        return bool(self.yandex_api_key and self.yandex_folder_id)

    def clock(self):
        """возвращает функцию текущего времени в часовом поясе из настроек.
        без tzinfo и секунд, чтобы везде сравнивались одинаковые даты"""
        zone = ZoneInfo(self.timezone)
        return lambda: datetime.now(zone).replace(tzinfo=None, second=0, microsecond=0)

    def now(self) -> datetime:
        """текущее время в часовом поясе из настроек, с секундами. нужно базе для дат создания и изменения"""
        return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None, microsecond=0)


def _path(name: str, default: str) -> Path:
    # относительный путь считается от корня проекта, а не от папки, из которой запустили
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        yandex_api_key=os.getenv("YANDEX_API_KEY", "").strip(),
        yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", "").strip(),
        llm_model=os.getenv("YANDEX_LLM_MODEL", "aliceai-llm/latest"),
        tts_voice=os.getenv("YANDEX_TTS_VOICE", "alena"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
        db_path=_path("DB_PATH", "data/assistant.db"),
        demo_csv=_path("DEMO_CSV", "data/voice_assistant_events_1000.csv"),
        log_path=_path("LOG_PATH", "logs/bot.log"),
        meeting_minutes=int(os.getenv("MEETING_MINUTES", "60")),
        meeting_reminder_minutes=int(os.getenv("MEETING_REMINDER_MINUTES", "15")),
        digest_time=time.fromisoformat(os.getenv("DIGEST_TIME", "09:00")),
        semantic_threshold=float(os.getenv("SEMANTIC_THRESHOLD", "0.55")),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
    )
