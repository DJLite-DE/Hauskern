# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
wxalerts.py – Amtliche Unwetterwarnungen des Deutschen Wetterdienstes (DWD) für den eigenen Ort.

Quelle: DWD-Geodienst (WFS), Layer „dwd:Warnungen_Gemeinden“ – kostenlos, ohne Konto, auf Gemeinde-Ebene.
Abgefragt wird ein winziges Rechteck um den Wetter-Standort; zurück kommen alle Warnungen der Gemeinde.

    url(lat, lon)          → Abfrage-Adresse
    parse(geojson, now)    → [{"id","event","level","headline","desc","instr","onset","expires","icon",...}]
    fetch(lat, lon)        → wie parse, holt selbst
    new_or_worse(old, new) → Warnungen, über die neu informiert werden sollte

Stufen wie beim DWD: 1 = Wetterwarnung (gelb), 2 = markantes Wetter (orange), 3 = Unwetter (rot),
4 = extremes Unwetter (violett).
"""
import json
import re
import time
import urllib.parse
import urllib.request

WFS = "https://maps.dwd.de/geoserver/dwd/ows"
UA = {"User-Agent": "Hauskern (lokale Smart-Home-App)"}
LEVELS = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}
LEVEL_NAMES = {1: "Wetterwarnung", 2: "Markantes Wetter", 3: "Unwetter", 4: "Extremes Unwetter"}
LEVEL_COLORS = {1: "#f2d14b", 2: "#f59a3c", 3: "#ef5a5a", 4: "#b15cf0"}
ICONS = [(r"gewitter", "⛈"), (r"orkan|sturm|wind|böen|boeen", "💨"), (r"glatteis|glätte|glaette|frost", "🧊"),
         (r"schnee", "🌨"), (r"regen|niederschl", "🌧"), (r"nebel", "🌫"), (r"hitze", "🌡"), (r"uv", "☀"),
         (r"tauwetter|hochwasser", "🌊")]


class AlertError(Exception):
    pass


def url(lat, lon, d=0.002):
    """WFS 1.0.0: BBOX in Länge,Breite (EPSG:4326)."""
    q = {"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": "dwd:Warnungen_Gemeinden",
         "outputFormat": "application/json", "srsName": "EPSG:4326",
         "bbox": f"{lon - d:.5f},{lat - d:.5f},{lon + d:.5f},{lat + d:.5f}"}
    return WFS + "?" + urllib.parse.urlencode(q)


def _ts(s):
    """„2026-11-18T13:00:00Z“ oder mit +01:00 → Unix-Zeit (None, wenn leer)."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x or 0) for x in m.groups()[:6])
    import calendar
    t = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))
    tz = m.group(7)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        t -= sign * (hh * 3600 + mm * 60)
    return t


def icon_for(event):
    e = (event or "").lower()
    for pat, ic in ICONS:
        if re.search(pat, e):
            return ic
    return "⚠"


def _nice_event(event):
    """„STURMBÖEN“ → „Sturmböen“ (DWD schreibt Ereignisse groß)."""
    e = str(event or "").strip()
    return e[:1].upper() + e[1:].lower() if e.isupper() else e


def _nice_headline(head, raw_event, event):
    """„Amtliche Warnung vor STURMBÖEN“ → „Amtliche Warnung vor Sturmböen“ (auch andere GROSSE Wörter)."""
    h = str(head or "").strip() or f"Amtliche Warnung vor {event}"
    if raw_event and raw_event in h:
        h = h.replace(raw_event, event)
    return re.sub(r"\b([A-ZÄÖÜ]{4,})\b", lambda m: m.group(1).capitalize(), h)


def parse(data, now=None):
    now = now or time.time()
    feats = (data or {}).get("features") or []
    seen, out = set(), []
    for f in feats:
        p = (f or {}).get("properties") or {}
        if str(p.get("MSGTYPE", "")).lower() == "cancel" or str(p.get("STATUS", "Actual")).lower() not in ("actual", ""):
            continue
        level = LEVELS.get(str(p.get("SEVERITY", "")).lower())
        if not level:
            continue
        onset, expires = _ts(p.get("ONSET") or p.get("EFFECTIVE")), _ts(p.get("EXPIRES"))
        if expires and expires < now:
            continue
        raw_event = str(p.get("EVENT") or "").strip()
        event = _nice_event(raw_event)
        key = (event, level, onset, expires)
        if key in seen:  # gleiche Warnung für Nachbar-Gemeinde am Rand des Rechtecks
            continue
        seen.add(key)
        out.append({"id": p.get("IDENTIFIER") or "|".join(map(str, key)), "event": event, "level": level,
                    "level_name": LEVEL_NAMES[level], "color": LEVEL_COLORS[level], "icon": icon_for(event),
                    "headline": _nice_headline(p.get("HEADLINE"), raw_event, event),
                    "desc": (p.get("DESCRIPTION") or "").strip(), "instr": (p.get("INSTRUCTION") or "").strip(),
                    "onset": onset, "expires": expires, "area": p.get("AREADESC") or p.get("NAME") or "",
                    "pre": str(p.get("URGENCY", "")).lower() == "future"})
    out.sort(key=lambda a: (-a["level"], a["onset"] or 0))
    return out


def fetch(lat, lon, timeout=15):
    try:
        req = urllib.request.Request(url(lat, lon), headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 – feste DWD-Adresse
            raw = r.read(4_000_000)
    except OSError as e:
        raise AlertError(f"DWD nicht erreichbar: {e}") from e
    try:
        return parse(json.loads(raw.decode("utf-8", "replace")))
    except ValueError as e:
        raise AlertError("Antwort des DWD nicht lesbar") from e


def new_or_worse(old, new):
    """Neue Warnung (anderes Ereignis) oder eine Stufe höher als vorher → melden. Verlängerungen nicht."""
    best = {}
    for a in old or []:
        best[a["event"]] = max(best.get(a["event"], 0), a["level"])
    return [a for a in new if a["level"] > best.get(a["event"], 0)]


def when(a, now=None):
    """„bis 20:00“ / „ab Do 14:00 bis 22:00“."""
    now = now or time.time()
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

    def f(t):
        lt, ln = time.localtime(t), time.localtime(now)
        day = "" if (lt.tm_yday, lt.tm_year) == (ln.tm_yday, ln.tm_year) else wd[lt.tm_wday] + " "
        return day + time.strftime("%H:%M", lt)
    parts = []
    if a.get("onset") and a["onset"] > now + 60:
        parts.append("ab " + f(a["onset"]))
    if a.get("expires"):
        parts.append("bis " + f(a["expires"]))
    return " ".join(parts)
