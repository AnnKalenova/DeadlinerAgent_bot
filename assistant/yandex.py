"""все что ходит в Yandex Cloud: языковая модель, эмбеддинги и SpeechKit (голос)

модель и эмбеддинги через OpenAI-совместимый API AI Studio, SpeechKit через REST API v1.
везде один и тот же API-ключ сервисного аккаунта
"""
from __future__ import annotations

import asyncio
import json

import httpx
from openai import AsyncOpenAI, RateLimitError

from .nlu.llm import ChatResult

LLM_BASE_URL = "https://llm.api.cloud.yandex.net/v1"
STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
TTS_MAX_CHARS = 4900  # у API лимит 5000 символов, берем с запасом


def _openai_client(api_key: str, folder_id: str, timeout: float, max_retries: int = 1) -> AsyncOpenAI:
    # project уходит в заголовок OpenAI-Project, по нему AI Studio понимает какой каталог
    return AsyncOpenAI(api_key=api_key, project=folder_id, base_url=LLM_BASE_URL,
                       timeout=timeout, max_retries=max_retries)


class YandexGPT:
    """языковая модель с вызовом инструментов (function calling). какая модель - задается в YANDEX_LLM_MODEL"""

    def __init__(self, api_key: str, folder_id: str, model: str = "aliceai-llm/latest",
                 temperature: float = 0.1, timeout: float = 25):
        self._client = _openai_client(api_key, folder_id, timeout)
        self._model = f"gpt://{folder_id}/{model}"
        self._temperature = temperature  # низкая, чтобы на одну фразу отвечала одинаково

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self._temperature,
        )
        message = response.choices[0].message
        # модель либо выбирает инструмент, либо просто отвечает текстом
        if message.tool_calls:
            call = message.tool_calls[0].function
            arguments = call.arguments
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            return ChatResult(tool=call.name, arguments=arguments or {})
        return ChatResult(text=message.content or "")


class YandexEmbedder:
    """эмбеддинги текста для поиска по смыслу

    API берет один текст за запрос и по умолчанию не больше 10 запросов в секунду.
    поэтому шлем параллельно, но притормаживаем, а на ответ 429 (слишком часто) пробуем еще раз
    """

    def __init__(self, api_key: str, folder_id: str, requests_per_second: float = 8, timeout: float = 15):
        self._client = _openai_client(api_key, folder_id, timeout, max_retries=0)
        self._doc_model = f"emb://{folder_id}/text-search-doc/latest"  # для названий записей
        self._query_model = f"emb://{folder_id}/text-search-query/latest"  # для поискового запроса
        self._interval = 1 / requests_per_second
        self._next_slot = 0.0
        self._lock = asyncio.Lock()
        self.name = "yandex-text-search"

    async def _throttle(self) -> None:
        """пропускает запросы не чаще чем раз в interval секунд"""
        loop = asyncio.get_running_loop()
        async with self._lock:
            wait = self._next_slot - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = max(loop.time(), self._next_slot) + self._interval

    async def _embed(self, model: str, text: str, attempts: int = 4) -> list[float]:
        for attempt in range(attempts):
            await self._throttle()
            try:
                response = await self._client.embeddings.create(model=model, input=text, encoding_format="float")
                return list(response.data[0].embedding)
            except RateLimitError:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(1 + attempt)  # каждый раз ждем чуть дольше
        raise RuntimeError("unreachable")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return list(await asyncio.gather(*(self._embed(self._doc_model, t) for t in texts)))

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(self._query_model, text)


class SpeechError(Exception):
    """SpeechKit вернул ошибку"""


class SpeechKit:
    """голос: распознать голосовое и озвучить ответ"""

    def __init__(self, api_key: str, voice: str = "alena", timeout: float = 20):
        self._headers = {"Authorization": f"Api-Key {api_key}"}
        self._voice = voice
        self._http = httpx.AsyncClient(timeout=timeout)

    async def recognize(self, audio: bytes) -> str:
        """короткое аудио (до 30 сек и 1 МБ) в OGG/Opus. телега как раз в нем присылает голосовые,
        так что ничего конвертировать не надо"""
        response = await self._http.post(
            STT_URL, params={"lang": "ru-RU", "format": "oggopus"}, headers=self._headers, content=audio,
        )
        if response.status_code != 200:
            raise SpeechError(f"STT {response.status_code}: {response.text[:200]}")
        return response.json().get("result", "").strip()

    async def synthesize(self, text: str) -> bytes:
        response = await self._http.post(
            TTS_URL,
            headers=self._headers,
            data={"text": text[:TTS_MAX_CHARS], "lang": "ru-RU", "voice": self._voice, "format": "oggopus"},
        )
        if response.status_code != 200:
            raise SpeechError(f"TTS {response.status_code}: {response.text[:200]}")
        return response.content

    async def close(self) -> None:
        await self._http.aclose()
