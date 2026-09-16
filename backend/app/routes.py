import logging

from flask import Blueprint, current_app, jsonify

from .cache import CacheService
from .validators import validate_city
from .weather_client import WeatherClient

logger = logging.getLogger(__name__)

bp = Blueprint("weather", __name__)


def _cache_service() -> CacheService:
    return current_app.extensions["cache_service"]


def _weather_client() -> WeatherClient:
    return current_app.extensions["weather_client"]


@bp.get("/weather/<city>")
def get_weather(city: str):
    clean_city = validate_city(city)
    cache = _cache_service()

    cached = cache.get(clean_city)
    if cached is not None:
        return jsonify({**cached, "cached": True}), 200

    payload = _weather_client().fetch(clean_city)
    cache.set(clean_city, payload)

    return jsonify({**payload, "cached": False}), 200


@bp.get("/healthz")
def healthz():
    """Liveness probe: процесс жив и может отвечать на HTTP."""
    return jsonify({"status": "ok"}), 200


@bp.get("/readyz")
def readyz():
    """Readiness probe: под готов принимать реальный трафик только тогда,
    когда доступен Redis и настроен ключ внешнего API."""
    redis_ok = _cache_service().ping()
    api_key_configured = bool(current_app.config.get("WEATHER_API_KEY"))
    ready = redis_ok and api_key_configured

    body = {
        "status": "ok" if ready else "not_ready",
        "redis": redis_ok,
        "weather_api_key_configured": api_key_configured,
    }
    return jsonify(body), 200 if ready else 503
