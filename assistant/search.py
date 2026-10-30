"""поиск записей: по словам и по смыслу

по словам ищем через основы, так находятся разные формы ("договору" -> "договор").
по смыслу - через эмбеддинги Yandex, так находятся фразы даже без общих слов
("созвон" -> "встреча"). если эмбеддинги недоступны, ищем только по словам
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .dates import normalize
from .domain import Item, ItemType
from .storage import Storage

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-zа-я0-9]+")
# слова, которые есть почти в любом запросе и ничего не значат для поиска
_STOP_WORDS = {
    "и", "в", "во", "на", "с", "со", "к", "ко", "по", "о", "об", "про", "для", "до", "от", "за", "у",
    "из", "а", "но", "что", "как", "мне", "меня", "мой", "моя", "мою", "я", "это", "эту", "ее", "его",
    "найди", "найти", "поищи", "когда", "где", "был", "была", "было", "будет", "ставил", "задачу",
    "задача", "запись", "пожалуйста",
}
# сколько недостающих эмбеддингов можно досчитать за один поиск (24 штуки - около 3 сек).
# остальные записи в этом поиске сравниваются только по словам, а эмбеддинги для них досчитаются позже
_MAX_EMBED_PER_SEARCH = 24
# эмбеддинги считаем и сохраняем порциями. если считать все 376 названий из /demo одной пачкой,
# поиск, запущенный в это время, ждет в общей очереди к API больше минуты
_EMBED_BATCH = 16
_MIN_KEYWORD_SCORE = 0.5  # какая доля слов запроса (с весами) должна быть в названии, чтобы считать что нашли


class Embedder(Protocol):
    """откуда берем эмбеддинги. сама реализация - YandexEmbedder в yandex.py"""

    name: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@dataclass
class Hit:
    """найденная запись и насколько она подходит"""

    item: Item
    score: float


def stem(word: str) -> str:
    """упрощенная основа слова: срезаем окончание, но оставляем минимум 4 буквы"""
    return word if len(word) <= 4 else word[:max(4, len(word) - 2)]


def stems(text: str) -> list[str]:
    result = []
    for word in _WORD.findall(normalize(text)):
        if word in _STOP_WORDS or (len(word) < 3 and not word.isdigit()):
            continue
        value = stem(word)
        if value not in result:
            result.append(value)
    return result


def _matches(query_stem: str, title: list[str]) -> bool:
    return any(t.startswith(query_stem) or query_stem.startswith(t) for t in title)


def word_weights(query: list[str], titles: list[list[str]]) -> dict[str, float]:
    """веса слов как в IDF: чем реже слово встречается в записях, тем оно важнее.
    слова, которых нет ни в одном названии, отбрасываем"""
    weights = {}
    for q in query:
        frequency = sum(1 for title in titles if _matches(q, title))
        if frequency:
            weights[q] = math.log(1 + len(titles) / frequency)
    return weights


def keyword_score(query: list[str], title: list[str], weights: dict[str, float] | None = None) -> float:
    weights = weights if weights is not None else {q: 1.0 for q in query}
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(w for q, w in weights.items() if _matches(q, title)) / total


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class Searcher:
    """ищет записи пользователя по описанию, можно с фильтром по типу и по времени"""

    def __init__(self, storage: Storage, embedder: Embedder | None = None,
                 semantic_threshold: float = 0.55, keyword_weight: float = 0.5):
        self._storage = storage
        self._embedder = embedder
        self._threshold = semantic_threshold
        self._keyword_weight = keyword_weight
        self._warming = 0  # сколько прогревов (warm_up) идет прямо сейчас

    async def search(self, user_id: int, query: str, now: datetime, *, item_type: ItemType | None = None,
                     scope: str | None = None, limit: int = 5) -> list[Hit]:
        everything = self._storage.all_items(user_id)
        items = _filter(everything, now, item_type, scope)
        if not items or not query.strip():
            return []

        # веса слов считаем по всем записям, а не только отфильтрованным, так они стабильнее
        title_stems = {item.title: stems(item.title) for item in everything}
        weights = word_weights(stems(query), list(title_stems.values()))
        semantic = await self._semantic_scores(query, items)
        hits = []
        for item in items:
            keyword = keyword_score(list(weights), title_stems[item.title], weights)
            similarity = semantic.get(item.title)
            if similarity is None:
                score, relevant = keyword, keyword >= _MIN_KEYWORD_SCORE
            else:
                # итоговая оценка - взвешенная сумма оценок по словам и по смыслу. запись подходит, если хорошо совпало хотя бы одно
                score = self._keyword_weight * keyword + (1 - self._keyword_weight) * similarity
                relevant = keyword >= _MIN_KEYWORD_SCORE or similarity >= self._threshold
            if relevant:
                hits.append(Hit(item, score))

        hits.sort(key=lambda hit: (-round(hit.score, 3), _distance(hit.item, now, scope)))
        return hits[:limit]

    async def warm_up(self, titles: list[str]) -> None:
        """заранее считает эмбеддинги, например сразу после /demo"""
        if not self._embedder:
            return
        self._warming += 1
        try:
            await self._embeddings(list(dict.fromkeys(titles)), limit=None)
        except Exception as exc:
            # не критично: недостающие эмбеддинги посчитаются во время поиска
            log.warning("Embeddings warm-up stopped: %s", exc)
        finally:
            self._warming -= 1

    async def _semantic_scores(self, query: str, items: list[Item]) -> dict[str, float]:
        if not self._embedder:
            return {}
        try:
            titles = list(dict.fromkeys(item.title for item in items))
            # пока идет прогрев, сами ничего не досчитываем: берем то, что уже в кеше, остальное ищется по словам.
            # иначе поиск встанет в очередь к API за всеми названиями из /demo и будет ждать
            limit = 0 if self._warming else _MAX_EMBED_PER_SEARCH
            vectors = await self._embeddings(titles, limit=limit)
            query_vector = await self._embedder.embed_query(query)
        except Exception as exc:
            log.warning("Semantic search unavailable, using keywords only: %s", exc)
            return {}
        return {title: cosine(query_vector, vector) for title, vector in vectors.items()}

    def _key(self, text: str) -> str:
        # ключ в кеше: модель + хеш текста. одинаковые названия считаются один раз
        return f"{self._embedder.name}:{hashlib.sha1(normalize(text).encode()).hexdigest()}"

    async def _embeddings(self, titles: list[str], limit: int | None) -> dict[str, list[float]]:
        keys = {title: self._key(title) for title in titles}
        cached = self._storage.get_embeddings(list(keys.values()))
        result = {title: cached[key] for title, key in keys.items() if key in cached}
        missing = [title for title in titles if title not in result]
        if limit is not None:
            missing = missing[:limit]
        for first in range(0, len(missing), _EMBED_BATCH):
            batch = missing[first:first + _EMBED_BATCH]
            vectors = await self._embedder.embed_documents(batch)
            for title, vector in zip(batch, vectors):
                self._storage.put_embedding(keys[title], vector)  # сразу в кеш, чтобы другие поиски уже видели
                result[title] = vector
        return result


def _filter(items: list[Item], now: datetime, item_type: ItemType | None, scope: str | None) -> list[Item]:
    """оставляем записи нужного типа и из нужного времени: past - прошлое, future - будущее"""
    if item_type:
        items = [i for i in items if i.type is item_type]
    if scope == "past":
        items = [i for i in items if i.start and i.start < now]
    elif scope == "future":
        items = [i for i in items if not i.start or i.start >= now or i.repeat]
    return items


def _distance(item: Item, now: datetime, scope: str | None) -> float:
    """как сортировать, если подходят одинаково: для прошлого сначала самые свежие, иначе ближайшие будущие"""
    if item.start is None:
        return math.inf
    delta = (item.start - now).total_seconds()
    if scope == "past":
        return abs(delta)
    return delta if delta >= 0 else 1e10 - delta
