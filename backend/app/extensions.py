import redis

_redis_client = None


def init_redis(app):
    """Создаёт Redis-клиент с пулом соединений на основе конфигурации Flask.

    Клиент создаётся лениво по установлению TCP-соединения — если Redis
    временно недоступен при старте пода, приложение всё равно поднимется
    (liveness пройдёт), а недоступность будет видна через /readyz.
    """
    global _redis_client
    pool = redis.ConnectionPool(
        host=app.config["REDIS_HOST"],
        port=app.config["REDIS_PORT"],
        db=app.config["REDIS_DB"],
        password=app.config["REDIS_PASSWORD"],
        socket_timeout=app.config["REDIS_SOCKET_TIMEOUT"],
        socket_connect_timeout=app.config["REDIS_SOCKET_TIMEOUT"],
        decode_responses=True,
    )
    _redis_client = redis.Redis(connection_pool=pool)
    return _redis_client


def get_redis():
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized. Call init_redis(app) first.")
    return _redis_client
