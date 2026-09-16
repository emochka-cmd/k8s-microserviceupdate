import logging

from flask import jsonify

from .validators import InvalidCityError
from .weather_client import WeatherAPIError

logger = logging.getLogger(__name__)


def register_error_handlers(app):
    @app.errorhandler(InvalidCityError)
    def handle_invalid_city(err):
        return jsonify({"error": "bad_request", "message": str(err)}), 400

    @app.errorhandler(WeatherAPIError)
    def handle_weather_api_error(err):
        logger.warning("Weather API error: %s", err)
        return jsonify({"error": "weather_provider_error", "message": str(err)}), err.status_code

    @app.errorhandler(404)
    def handle_not_found(err):
        return jsonify({"error": "not_found", "message": "Resource not found"}), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(err):
        return jsonify({"error": "method_not_allowed", "message": "Method not allowed"}), 405

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        # Ничего из внутренних деталей исключения наружу не отдаём —
        # только generic-сообщение, детали идут в лог.
        logger.exception("Unhandled exception")
        return jsonify({"error": "internal_error", "message": "Internal server error"}), 500
