// app.js
"use strict";

const API_BASE_PATH = "/api";

const form = document.getElementById("weather-form");
const cityInput = document.getElementById("city");
const submitButton = document.getElementById("submit-button");
const buttonText = document.getElementById("button-text");
const buttonSpinner = document.getElementById("button-spinner");
const message = document.getElementById("form-message");
const weatherCard = document.getElementById("weather-card");

const weatherLocation = document.getElementById("weather-location");
const weatherDescription = document.getElementById("weather-description");
const weatherTemperature = document.getElementById("weather-temperature");
const weatherFeelsLike = document.getElementById("weather-feels-like");
const weatherHumidity = document.getElementById("weather-humidity");
const weatherPressure = document.getElementById("weather-pressure");
const weatherWind = document.getElementById("weather-wind");
const weatherCache = document.getElementById("weather-cache");
const weatherTime = document.getElementById("weather-time");

const CITY_PATTERN = /^[^\d!@#$%^&*()_+=\[\]{};:"\\|,.<>/?~`]{1,100}$/u;

let requestController = null;

function setLoading(isLoading) {
    submitButton.disabled = isLoading;
    cityInput.disabled = isLoading;
    buttonText.textContent = isLoading
        ? "Загрузка…"
        : "Узнать погоду";

    buttonSpinner.hidden = !isLoading;
}

function setMessage(text, type = "") {
    message.textContent = text;
    message.className = `message ${type}`.trim();
}

function clearMessage() {
    setMessage("");
}

function clearWeather() {
    weatherCard.hidden = true;
}

function normalizeCity(value) {
    return value.trim().replace(/\s+/gu, " ");
}

function validateCity(city) {
    if (!city) {
        return "Введите название города.";
    }

    if (city.length > 100) {
        return "Название города не должно превышать 100 символов.";
    }

    if (!CITY_PATTERN.test(city)) {
        return "Название города содержит недопустимые символы.";
    }

    return null;
}

function formatNumber(value, digits = 1) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return "—";
    }

    return new Intl.NumberFormat("ru-RU", {
        maximumFractionDigits: digits,
    }).format(value);
}

function formatInteger(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return "—";
    }

    return new Intl.NumberFormat("ru-RU", {
        maximumFractionDigits: 0,
    }).format(value);
}

function formatFetchedAt(value) {
    if (typeof value !== "string") {
        return "";
    }

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return "";
    }

    return new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "short",
        timeStyle: "short",
    }).format(date);
}

function renderWeather(data) {
    /*
     * Бэкенд намеренно отдаёт стабильную схему:
     * city, country, temperature, feels_like, humidity,
     * pressure, wind_speed, description, icon, units,
     * fetched_at и cached.
     *
     * Значения вставляются через textContent, а не innerHTML.
     */
    weatherLocation.textContent = [data.city, data.country]
        .filter((value) => typeof value === "string" && value.trim())
        .join(", ");

    weatherDescription.textContent =
        typeof data.description === "string"
            ? data.description
            : "Описание недоступно";

    weatherTemperature.textContent = formatNumber(data.temperature);
    weatherFeelsLike.textContent = formatNumber(data.feels_like);
    weatherHumidity.textContent = formatInteger(data.humidity);
    weatherPressure.textContent = formatInteger(data.pressure);
    weatherWind.textContent = formatNumber(data.wind_speed);

    weatherCache.textContent = data.cached
        ? "Данные получены из кэша"
        : "Данные получены от погодного сервиса";

    const formattedTime = formatFetchedAt(data.fetched_at);

    weatherTime.textContent = formattedTime
        ? `Обновлено: ${formattedTime}`
        : "";

    if (data.icon && typeof data.icon === "string") {
        weatherCard.dataset.weatherIcon = data.icon;
    } else {
        delete weatherCard.dataset.weatherIcon;
    }

    weatherCard.hidden = false;
}

async function parseErrorResponse(response) {
    const fallbackMessages = {
        400: "Некорректное название города.",
        404: "Город не найден.",
        405: "Метод запроса не поддерживается.",
        502: "Погодный сервис временно недоступен.",
        503: "Сервис временно не готов принимать запросы.",
        504: "Погодный сервис не ответил вовремя.",
        500: "Внутренняя ошибка сервера.",
    };

    let payload = null;

    try {
        payload = await response.json();
    } catch {
        // Ответ может быть не JSON.
    }

    if (
        payload &&
        typeof payload.message === "string" &&
        payload.message.trim()
    ) {
        if (response.status === 404) {
            return "Город не найден.";
        }

        if (response.status >= 500) {
            return fallbackMessages[response.status] ||
                "Сервис временно недоступен.";
        }

        return payload.message;
    }

    return fallbackMessages[response.status] ||
        "Не удалось получить данные о погоде.";
}

async function fetchWeather(city, signal) {
    /*
     * Backend route:
     * GET /weather/<city>
     *
     * Nginx проксирует:
     * /api/weather/<city> -> backend /weather/<city>
     */
    const encodedCity = encodeURIComponent(city);
    const url = `${API_BASE_PATH}/weather/${encodedCity}`;

    const response = await fetch(url, {
        method: "GET",
        headers: {
            Accept: "application/json",
        },
        credentials: "same-origin",
        cache: "no-store",
        signal,
    });

    if (!response.ok) {
        throw new Error(await parseErrorResponse(response));
    }

    const data = await response.json();

    if (!data || typeof data !== "object") {
        throw new Error("Сервер вернул некорректный ответ.");
    }

    return data;
}

async function handleSubmit(event) {
    event.preventDefault();

    const city = normalizeCity(cityInput.value);
    const validationError = validateCity(city);

    clearMessage();

    if (validationError) {
        clearWeather();
        setMessage(validationError, "error");
        cityInput.focus();
        return;
    }

    if (requestController) {
        requestController.abort();
    }

    requestController = new AbortController();

    setLoading(true);

    try {
        const data = await fetchWeather(
            city,
            requestController.signal,
        );

        renderWeather(data);
        setMessage("Погода успешно получена.", "success");
    } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
            return;
        }

        clearWeather();

        const text =
            error instanceof Error
                ? error.message
                : "Не удалось получить данные о погоде.";

        setMessage(text, "error");
    } finally {
        setLoading(false);
        requestController = null;
    }
}

form.addEventListener("submit", handleSubmit);

cityInput.addEventListener("input", () => {
    if (message.classList.contains("error")) {
        clearMessage();
    }
});

buttonSpinner.hidden = true;