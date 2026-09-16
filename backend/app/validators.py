import re

# Разрешаем буквы (включая кириллицу и другие unicode-буквы), пробелы,
# дефисы и апострофы (Санкт-Петербург, Нью-Йорк, Кот-д'Ивуар).
# Явно запрещаем цифры и спецсимволы, которые не нужны в названии города
# и потенциально опасны при формировании запроса к внешнему API.
_CITY_RE = re.compile(r"^[^\d!@#$%^&*()_+=\[\]{};:\"\\|,.<>/?~`]{1,100}$", re.UNICODE)


class InvalidCityError(ValueError):
    """Некорректное имя города, пришедшее от клиента."""


def validate_city(raw_city: str) -> str:
    city = (raw_city or "").strip()

    if not city:
        raise InvalidCityError("City name must not be empty")
    if len(city) > 100:
        raise InvalidCityError("City name is too long")
    if not _CITY_RE.match(city):
        raise InvalidCityError("City name contains invalid characters")

    return city
