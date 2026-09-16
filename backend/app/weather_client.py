import logging
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class WeatherAPIError(Exception):
    """Базовая ошибка при обращении к внешнему API погоды."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class CityNotFoundError(WeatherAPIError):
    def __init__(self, city: str):
        super().__init__(f"City '{city}' not found", status_code=404)


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class WeatherClient:
    """Обёртка над OpenWeatherMap Current Weather API.

    Документация: https://openweathermap.org/current
    Запрос: GET {base_url}?q=<city>&appid=<key>&units=metric
    """

    def __init__(self, base_url: str, api_key: str, units: str, timeout: float):
        self._base_url = base_url
        self._api_key = api_key
        self._units = units
        self._timeout = timeout
        self._session = _build_session()

    def fetch(self, city: str) -> dict:
        if not self._api_key:
            raise WeatherAPIError(
                "WEATHER_API_KEY is not configured on the server", status_code=500
            )

        params = {"q": city, "appid": self._api_key, "units": self._units}
        try:
            response = self._session.get(self._base_url, params=params, timeout=self._timeout)
        except requests.Timeout as exc:
            raise WeatherAPIError("Weather provider timed out", status_code=504) from exc
        except requests.RequestException as exc:
            raise WeatherAPIError(f"Weather provider is unreachable: {exc}", status_code=502) from exc

        if response.status_code == 404:
            raise CityNotFoundError(city)
        if response.status_code == 401:
            logger.error("Weather API rejected the API key (401)")
            raise WeatherAPIError("Weather provider rejected credentials", status_code=502)
        if not response.ok:
            raise WeatherAPIError(
                f"Weather provider returned status {response.status_code}", status_code=502
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise WeatherAPIError("Weather provider returned invalid JSON", status_code=502) from exc

        return self._normalize(data)

    def _normalize(self, data: dict) -> dict:
        """Приводим ответ провайдера к стабильной схеме, независимой от
        деталей конкретного API — фронтенд не должен знать про OpenWeatherMap."""
        try:
            weather_list = data.get("weather") or [{}]
            main = data.get("main", {})
            wind = data.get("wind", {})
            return {
                "city": data.get("name"),
                "country": data.get("sys", {}).get("country"),
                "temperature": main.get("temp"),
                "feels_like": main.get("feels_like"),
                "humidity": main.get("humidity"),
                "pressure": main.get("pressure"),
                "wind_speed": wind.get("speed"),
                "description": weather_list[0].get("description"),
                "icon": weather_list[0].get("icon"),
                "units": self._units,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except (AttributeError, IndexError, KeyError) as exc:
            raise WeatherAPIError(
                f"Unexpected response shape from weather provider: {exc}", status_code=502
            ) from exc
