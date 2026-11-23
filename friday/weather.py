"""Live weather from Open-Meteo (free, no account or API key)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=3"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
)

# WMO weather codes -> spoken words
CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}


@dataclass
class Place:
    name: str
    country: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return self.name


def _get_json(url: str, timeout: float = 8) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "FridayAssistant/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_place(city: str) -> Place | None:
    data = _get_json(GEO_URL + urllib.parse.quote(city.strip()))
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    return Place(r["name"], r.get("country", ""), r["latitude"], r["longitude"])


def forecast(place: Place) -> dict:
    return _get_json(FORECAST_URL.format(lat=place.lat, lon=place.lon))


def _deg(x: float) -> str:
    return f"{round(x)} degrees"


def describe(place: Place, data: dict, day: str = "today") -> str:
    """A short spoken summary. day: "today" (current conditions + the day) or "tomorrow"."""
    daily = data["daily"]
    i = 1 if day == "tomorrow" else 0
    high, low = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
    rain = daily["precipitation_probability_max"][i]
    sky = CODES.get(daily["weather_code"][i], "mixed conditions")
    wet_sky = any(w in sky for w in ("rain", "drizzle", "shower", "thunder", "snow"))
    rain_part = (f" with a {rain} percent chance of rain" if rain and rain >= 20 else
                 "" if wet_sky or rain is None else ", and rain is unlikely")
    if day == "tomorrow":
        return f"Tomorrow in {place.label}: {sky}, a high of {_deg(high)} and a low of {_deg(low)}{rain_part}."
    cur = data["current"]
    now_sky = CODES.get(cur["weather_code"], sky)
    feels = cur["apparent_temperature"]
    feels_part = f", feels like {round(feels)}" if abs(feels - cur["temperature_2m"]) >= 3 else ""
    return (f"Right now in {place.label} it's {_deg(cur['temperature_2m'])}{feels_part}, with {now_sky}. "
            f"Today: a high of {_deg(high)} and a low of {_deg(low)}{rain_part}.")


def weather_report(place: Place, day: str = "today") -> str:
    return describe(place, forecast(place), day)
