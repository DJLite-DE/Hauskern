# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
weather.py – Wetter über Open-Meteo (kostenlos, ohne Konto/Schlüssel).

  locate_auto()        -> {"name", "lat", "lon"}  Standort grob aus der öffentlichen IP (Stadt-genau)
  search(name)         -> [{"name", "lat", "lon", "region"}]  Ortssuche für die manuelle Eingabe
  fetch(lat, lon)      -> {"current": {...}, "hourly": [...], "daily": [...]}
  describe(code)       -> ("Leichter Regen", "🌦")

Hinweis zur Veröffentlichung: Open-Meteo ist für nicht-kommerzielle Nutzung frei.
"""
import json
import time
import urllib.parse
import urllib.request

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
IP_SERVICES = [  # (URL, Feld Breite, Feld Länge, Feld Stadt) – der Reihe nach probiert
    ("https://free.freeipapi.com/api/json", "latitude", "longitude", "cityName"),
    ("https://ipwho.is/", "latitude", "longitude", "city"),
    ("https://get.geojs.io/v1/ip/geo.json", "latitude", "longitude", "city"),
    ("https://ipapi.co/json/", "latitude", "longitude", "city"),
]
UA = {"User-Agent": "Hauskern (lokale Smart-Home-App)"}


class WeatherError(Exception):
    pass


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise WeatherError(f"nicht erreichbar ({getattr(e, 'reason', e)})")


def locate_auto():
    errs = []
    for url, la, lo, city in IP_SERVICES:
        try:
            d = _get(url, 8)
            lat, lon = float(d[la]), float(d[lo])
            if lat or lon:
                return {"name": d.get(city) or "Mein Standort", "lat": round(lat, 3), "lon": round(lon, 3)}
        except (WeatherError, KeyError, TypeError, ValueError) as e:
            errs.append(f"{urllib.parse.urlparse(url).hostname}: {e}")
    raise WeatherError("Standort automatisch nicht ermittelbar – bitte im Wetter-Fenster einen Ort wählen "
                       f"({'; '.join(errs)[:200]})")


def search(name, count=8):
    q = urllib.parse.urlencode({"name": name.strip(), "count": count, "language": "de", "format": "json"})
    d = _get(f"{GEOCODE}?{q}")
    out = []
    for r in d.get("results") or []:
        region = ", ".join(x for x in (r.get("admin1"), r.get("country")) if x)
        out.append({"name": r.get("name", "?"), "lat": round(r["latitude"], 3), "lon": round(r["longitude"], 3),
                    "region": region})
    return out


CODES = {
    0: ("Klar", "☀️", "🌙"), 1: ("Überwiegend klar", "🌤️", "🌙"), 2: ("Teilweise bewölkt", "⛅", "☁️"),
    3: ("Bedeckt", "☁️", "☁️"), 45: ("Nebel", "🌫️", "🌫️"), 48: ("Reifnebel", "🌫️", "🌫️"),
    51: ("Leichter Nieselregen", "🌦️", "🌧️"), 53: ("Nieselregen", "🌦️", "🌧️"), 55: ("Starker Nieselregen", "🌧️", "🌧️"),
    56: ("Gefrierender Niesel", "🌧️", "🌧️"), 57: ("Gefrierender Niesel", "🌧️", "🌧️"),
    61: ("Leichter Regen", "🌦️", "🌧️"), 63: ("Regen", "🌧️", "🌧️"), 65: ("Starker Regen", "🌧️", "🌧️"),
    66: ("Gefrierender Regen", "🌧️", "🌧️"), 67: ("Gefrierender Regen", "🌧️", "🌧️"),
    71: ("Leichter Schneefall", "🌨️", "🌨️"), 73: ("Schneefall", "🌨️", "🌨️"), 75: ("Starker Schneefall", "❄️", "❄️"),
    77: ("Schneegriesel", "🌨️", "🌨️"), 80: ("Regenschauer", "🌦️", "🌧️"), 81: ("Regenschauer", "🌧️", "🌧️"),
    82: ("Heftige Schauer", "⛈️", "⛈️"), 85: ("Schneeschauer", "🌨️", "🌨️"), 86: ("Schneeschauer", "🌨️", "🌨️"),
    95: ("Gewitter", "⛈️", "⛈️"), 96: ("Gewitter mit Hagel", "⛈️", "⛈️"), 99: ("Gewitter mit Hagel", "⛈️", "⛈️"),
}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}


def describe(code, day=True):
    t, d, n = CODES.get(int(code or 0), ("–", "🌡️", "🌡️"))
    return t, (d if day else n)


def _ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M" if "T" in s else "%Y-%m-%d"))


def fetch(lat, lon):
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                   "wind_speed_10m,wind_gusts_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,sunrise,sunset,wind_gusts_10m_max",
    })
    try:
        d = _get(f"{FORECAST}?{q}")
    except WeatherError as e:
        raise WeatherError(f"Wetterdienst Open-Meteo {e} – neuer Versuch in 2 Minuten")
    try:
        c = d["current"]
        cur = {"temp": c["temperature_2m"], "feels": c.get("apparent_temperature"), "hum": c.get("relative_humidity_2m"),
               "precip": c.get("precipitation") or 0.0, "code": c.get("weather_code", 0), "wind": c.get("wind_speed_10m"),
               "gusts": c.get("wind_gusts_10m"), "day": bool(c.get("is_day", 1)), "time": _ts(c["time"])}
        h = d["hourly"]
        hourly = [{"ts": _ts(t), "temp": h["temperature_2m"][i], "prob": (h.get("precipitation_probability") or [None] * 999)[i],
                   "precip": h["precipitation"][i], "code": h["weather_code"][i]} for i, t in enumerate(h["time"])]
        dd = d["daily"]
        daily = [{"ts": _ts(t), "code": dd["weather_code"][i], "max": dd["temperature_2m_max"][i],
                  "min": dd["temperature_2m_min"][i], "rain": dd["precipitation_sum"][i],
                  "prob": (dd.get("precipitation_probability_max") or [None] * 99)[i],
                  "sunrise": dd["sunrise"][i][-5:], "sunset": dd["sunset"][i][-5:],
                  "gusts": (dd.get("wind_gusts_10m_max") or [None] * 99)[i]} for i, t in enumerate(dd["time"])]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise WeatherError(f"unerwartete Antwort ({e})")
    cur["raining"] = cur["code"] in RAIN_CODES or cur["precip"] > 0.1
    now = time.time()
    nxt = [x for x in hourly if now - 3600 < x["ts"] <= now + 3600]
    cur["rain_soon"] = any((x["prob"] or 0) >= 60 or x["code"] in RAIN_CODES for x in nxt)
    return {"current": cur, "hourly": hourly, "daily": daily, "fetched": now}
