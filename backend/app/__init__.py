import logging

from flask import Flask
from flask_cors import CORS

from .cache import CacheService
from .config import Config
from .errors import register_error_handlers
from .extensions import init_redis
from .routes import bp as weather_bp
from .weather_client import WeatherClient


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    logging.basicConfig(
        level=app.config["LOG_LEVEL"],
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger(__name__)

    if not app.config.get("WEATHER_API_KEY"):
        # Намеренно не роняем процесс: пусть под поднимется и /readyz
        # честно покажет "not_ready", это понятнее в k8s, чем CrashLoopBackOff.
        logger.warning(
            "WEATHER_API_KEY is not set. /weather will return 500 and "
            "/readyz will report not_ready until it is configured."
        )

    origins = app.config["CORS_ORIGINS"]
    CORS(
        app,
        resources={
            r"/weather/*": {"origins": origins},
            r"/healthz": {"origins": origins},
            r"/readyz": {"origins": origins},
        },
    )

    redis_client = init_redis(app)

    app.extensions["cache_service"] = CacheService(redis_client, ttl=app.config["CACHE_TTL"])
    app.extensions["weather_client"] = WeatherClient(
        base_url=app.config["WEATHER_API_BASE_URL"],
        api_key=app.config["WEATHER_API_KEY"],
        units=app.config["WEATHER_API_UNITS"],
        timeout=app.config["WEATHER_API_TIMEOUT"],
    )

    register_error_handlers(app)
    app.register_blueprint(weather_bp)

    logger.info("Application initialized")
    return app
