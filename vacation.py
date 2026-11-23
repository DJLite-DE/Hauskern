# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
vacation.py – Urlaubsmodus: Anwesenheit vortäuschen, Heizung sparen, bei Personen an der Kamera melden.

Nur Logik ohne Seiteneffekte – geschaltet wird in Hauskern.pyw.

    cfg(raw)                       → Einstellungen mit Standardwerten
    plan(day, ain, c, loc)         → (an, aus) als Unix-Zeit für diesen Abend und diese Steckdose
    desired(now, ain, c, loc)      → soll die Lampe gerade an sein?
    until_ts(c)                    → Rückkehr als Unix-Zeit (oder None)
    heat_phase(now, c)             → Heizung gerade absenken? (endet „preheat_h“ Stunden vor der Rückkehr)

Jeder Abend und jede Lampe bekommt eigene, zufällige Zeiten – aber reproduzierbar (gleicher Tag = gleicher Plan),
damit ein Neustart mitten am Abend nichts durcheinanderbringt.
"""
import math
import random
import time

DEFAULTS = {"active": False, "since": 0, "until": "", "back": "18:00", "plugs": [], "start": "sunset",
            "end": "23:00", "spread": 30, "cam_push": True, "heat": True, "heat_temp": 16.0, "preheat_h": 3,
            "auto_end": True, "saved_soll": {}}
START_CHOICES = [("sunset", "bei Sonnenuntergang"), ("17:00", "17:00"), ("18:00", "18:00"), ("19:00", "19:00"),
                 ("20:00", "20:00")]
FALLBACK_SUNSET = "17:30"  # wenn der Standort unbekannt ist


def cfg(raw):
    c = dict(DEFAULTS)
    c.update(raw or {})
    return c


def sun_set(lat, lon, day):
    """Sonnenuntergang (Unix-Zeit) für den Tag, in dem „day“ liegt. None in Polarnacht/Mitternachtssonne."""
    jd = day / 86400 + 2440587.5
    n = math.ceil(jd - 2451545.0 + 0.0008)
    j_star = n - lon / 360
    m = (357.5291 + 0.98560028 * j_star) % 360
    mr = math.radians(m)
    c = 1.9148 * math.sin(mr) + 0.02 * math.sin(2 * mr) + 0.0003 * math.sin(3 * mr)
    lam = math.radians((m + c + 180 + 102.9372) % 360)
    transit = 2451545.0 + j_star + 0.0053 * math.sin(mr) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if cos_w >= 1 or cos_w <= -1:
        return None
    w = math.degrees(math.acos(cos_w))
    return (transit + w / 360 - 2440587.5) * 86400


def _hm(s, default="18:00"):
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return _hm(default) if s != default else (18, 0)


def _midnight(day):
    lt = time.localtime(day)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def plan(day, ain, c, loc=None):
    """An/Aus für den Abend des Tages, in dem „day“ liegt."""
    base = _midnight(day)
    rnd = random.Random(f"{time.strftime('%Y-%m-%d', time.localtime(base))}|{ain}|{c.get('since', 0)}")
    spread = max(0, min(120, int(c.get("spread", 30) or 0))) * 60
    if c.get("start", "sunset") == "sunset":
        ss = sun_set(loc[0], loc[1], base + 12 * 3600) if loc else None
        if ss is None:
            h, m = _hm(FALLBACK_SUNSET)
            ss = base + h * 3600 + m * 60
        on = ss + 10 * 60 + rnd.uniform(0, spread)  # kurz nach Sonnenuntergang – wie echte Menschen
    else:
        h, m = _hm(c["start"])
        on = base + h * 3600 + m * 60 + rnd.uniform(-spread / 2, spread / 2)
    h, m = _hm(c.get("end", "23:00"), "23:00")
    off = base + h * 3600 + m * 60 + rnd.uniform(-spread / 2, spread / 2)
    if off <= on:  # Ende nach Mitternacht (z. B. 00:30)
        off += 86400
    return on, off


def desired(now, ain, c, loc=None):
    for d in (now, now - 86400):  # gestern mitzählen, falls es bis nach Mitternacht geht
        on, off = plan(d, ain, c, loc)
        if on <= now < off:
            return True
    return False


def until_ts(c):
    u = str(c.get("until") or "").strip()
    if not u:
        return None
    try:
        y, mo, d = (int(x) for x in u.split("-"))
    except ValueError:
        return None
    h, m = _hm(c.get("back", "18:00"))
    return time.mktime((y, mo, d, h, m, 0, 0, 0, -1))


def heat_phase(now, c):
    if not (c.get("active") and c.get("heat")):
        return False
    u = until_ts(c)
    return u is None or now < u - float(c.get("preheat_h", 3) or 0) * 3600


def describe(c, now=None):
    """Kurztext für Kachel und Logbuch."""
    if not c.get("active"):
        return "aus"
    u = until_ts(c)
    if u:
        wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][time.localtime(u).tm_wday]
        return f"an bis {wd} {time.strftime('%d.%m. %H:%M', time.localtime(u))}"
    return "an"
