# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
calls.py – Anrufliste der FRITZ!Box (TR-064 „X_AVM-DE_OnTel“ → calllist.lua).

Voraussetzung: Der FRITZ!Box-Benutzer von Hauskern braucht das Recht
„Sprachnachrichten, Faxnachrichten, FRITZ!App Fon und Anrufliste“.

Typen laut AVM: 1 angenommen, 2 verpasst, 3 ausgehend, 9 aktiv eingehend, 10 abgewiesen, 11 aktiv ausgehend.
Hat der Anrufbeantworter abgenommen (Port 40–49), zählt das hier als „verpasst – auf dem AB“.
"""
import time
import xml.etree.ElementTree as ET

S_ONTEL = "urn:dslforum-org:service:X_AVM-DE_OnTel:1"
TYPES = {1: "in", 2: "missed", 3: "out", 9: "active_in", 10: "rejected", 11: "active_out"}
LABEL = {"in": "angenommen", "missed": "verpasst", "out": "ausgehend", "rejected": "abgewiesen", "tam": "auf dem AB",
         "active_in": "läuft gerade", "active_out": "läuft gerade"}
ICON = {"in": "📞", "missed": "❗", "out": "↗️", "rejected": "⛔", "tam": "📼", "active_in": "🔔", "active_out": "📞"}


def parse_date(s):
    """„17.11.26 15:30“ → Zeitstempel"""
    try:
        return time.mktime(time.strptime(s.strip(), "%d.%m.%y %H:%M"))
    except (ValueError, AttributeError):
        return 0


def parse_duration(s):
    """„0:05“ (h:mm) → Minuten"""
    try:
        h, m = (s or "0:00").split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for c in root.iter("Call"):
        g = lambda k: (c.findtext(k) or "").strip()  # noqa: E731 – direkt verwendet
        try:
            typ = TYPES.get(int(g("Type") or 0))
        except ValueError:
            typ = None
        if not typ:
            continue
        port = g("Port")
        tam = typ == "in" and port.isdigit() and 40 <= int(port) <= 49
        incoming = typ in ("in", "missed", "rejected", "active_in")
        number = g("Caller") if incoming else g("Called")
        out.append({"id": int(g("Id") or 0), "type": "tam" if tam else typ, "name": g("Name"),
                    "number": number, "own": g("CalledNumber") if incoming else g("CallerNumber"),
                    "device": g("Device"), "ts": parse_date(g("Date")), "minutes": parse_duration(g("Duration")),
                    "count": int(g("Count") or 1) if (g("Count") or "1").isdigit() else 1,
                    "message": bool(g("Path"))})
    out.sort(key=lambda x: (x["ts"], x["id"]), reverse=True)
    return out


def who(c):
    if c["name"]:
        return c["name"]
    return pretty_number(c["number"]) if c["number"] else "Unbekannt"


def pretty_number(n):
    n = (n or "").strip()
    if not n:
        return "Unbekannt"
    if n.startswith("0049"):
        n = "0" + n[4:]
    elif n.startswith("+49"):
        n = "0" + n[3:]
    return n


def is_missed(c):
    return c["type"] in ("missed", "tam")


def new_missed(calls, last_id):
    """Verpasste Anrufe mit Id > last_id (älteste zuerst)."""
    return sorted([c for c in calls if is_missed(c) and c["id"] > last_id], key=lambda c: c["id"])


def when(ts, now=None):
    now = now or time.time()
    if not ts:
        return ""
    lt, ln = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%H:%M", lt)
    if lt[:3] == ln[:3]:
        return f"heute {hm}"
    if lt[:3] == time.localtime(now - 86400)[:3]:
        return f"gestern {hm}"
    if now - ts < 6 * 86400:
        return ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[lt.tm_wday] + f" {hm}"
    return time.strftime("%d.%m. %H:%M", lt)


def summary(calls, since):
    """Für Kachel/Siri: verpasste Anrufe seit `since`."""
    miss = [c for c in calls if is_missed(c) and c["ts"] >= since]
    return {"missed": len(miss), "last": calls[0] if calls else None, "missed_list": miss[:5]}
