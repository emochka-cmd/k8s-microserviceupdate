import json
import logging
from typing import Optional

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "weather"


def build_cache_key(city: str) -> str:
    return f"{CACHE_KEY_PREFIX}:{city.strip().lower()}"


class CacheService:
    """Инкапсулирует работу с Redis-кэшем.

    Важное решение для продакшена: если Redis недоступен, сервис не роняет
    запрос пользователя, а просто ведёт себя так, будто кэша нет —
    приложение продолжает работать напрямую с внешним API.
    """

    def __init__(self, redis_client, ttl: int):
        self._redis = redis_client
        self._ttl = ttl

    def get(self, city: str) -> Optional[dict]:
        key = build_cache_key(city)
        try:
            raw = self._redis.get(key)
        except RedisError as exc:
            logger.warning("Redis GET failed for key=%s: %s", key, exc)
            return None

        if raw is None:
            return None

        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Corrupt cache entry for key=%s: %s", key, exc)
            return None

    def set(self, city: str, payload: dict) -> None:
        key = build_cache_key(city)
        try:
            self._redis.set(key, json.dumps(payload), ex=self._ttl)
        except RedisError as exc:
            logger.warning("Redis SET failed for key=%s: %s", key, exc)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False
