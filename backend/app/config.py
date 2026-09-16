import os


class Config:
    """Вся конфигурация берётся из переменных окружения — так приложение
    остаётся 12-factor и легко настраивается через ConfigMap/Secret в k8s."""

    # --- Redis ---
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
    REDIS_SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", "2"))

    # --- Кэш ---
    CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))  # секунды

    # --- Внешний API погоды (OpenWeatherMap) ---
    # OpenWeatherMap выбран как "простой и без мороки" вариант:
    # один параметр q=<город>, один API-ключ, стабильный бесплатный тариф,
    # понятная JSON-схема ответа.
    WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")  # секрет, обязателен
    WEATHER_API_BASE_URL = os.getenv(
        "WEATHER_API_BASE_URL", "https://api.openweathermap.org/data/2.5/weather"
    )
    WEATHER_API_TIMEOUT = float(os.getenv("WEATHER_API_TIMEOUT", "5"))
    WEATHER_API_UNITS = os.getenv("WEATHER_API_UNITS", "metric")

    # --- CORS ---
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

    # --- Приложение ---
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    JSON_SORT_KEYS = False
