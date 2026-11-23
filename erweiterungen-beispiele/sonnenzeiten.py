# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
Beispiel-Erweiterung: Sonnenaufgang und -untergang – ohne Internet, aus dem Standort der Wetter-Einstellungen.
Zeigt eine Kachel und versteht „Hey Siri, Hauskern: Wann ist Sonnenuntergang?“
"""
import math
import time

NAME = "Sonnenzeiten"
DESCRIPTION = "Kachel mit Sonnenaufgang/-untergang und Tageslänge, Siri: „Sonnenuntergang“"
VERSION = "1.0"


def sun_times(lat, lon, day=None):
    """Sonnenauf- und -untergang (Unix-Zeit) nach der „Sunrise equation“. Genau auf ca. 1–2 Minuten."""
    day = day or time.time()
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
        return None  # Polarnacht / Mitternachtssonne
    w = math.degrees(math.acos(cos_w))
    to_unix = lambda j: (j - 2440587.5) * 86400  # noqa: E731
    return to_unix(transit - w / 360), to_unix(transit + w / 360)


def setup(hk):
    def text():
        loc = hk.location()
        if not loc:
            return None
        t = sun_times(*loc)
        if not t:
            return None
        rise, sset = t
        length = int(sset - rise) // 60
        return (time.strftime("%H:%M", time.localtime(rise)), time.strftime("%H:%M", time.localtime(sset)),
                f"{length // 60} Std. {length % 60} Min.")

    def update():
        t = text()
        if t:
            hk.tile("sonne", "🌅 Sonne", f"{t[0]} – {t[1]}", f"Tageslänge {t[2]}")
        else:
            hk.tile("sonne", "🌅 Sonne", "kein Standort", "Wetter-Standort einstellen")

    def answer(_):
        t = text()
        return f"Sonnenaufgang um {t[0]} Uhr, Sonnenuntergang um {t[1]} Uhr." if t else "Ich kenne den Standort nicht."

    hk.every(600, update)
    hk.siri(["sonnenuntergang"], answer)
    hk.siri(["sonnenaufgang"], answer)
