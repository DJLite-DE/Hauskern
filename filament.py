# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
filament.py – Filament-Lager: Spulen mit Restmenge, Lagerort und AMS-Fach.

* Bambu-Rollen mit RFID: Restmenge kommt direkt vom AMS.
* Andere Rollen: nach jedem Druck zieht Hauskern den Verbrauch ab (aus dem Druckauftrag).
* Nachwiegen: Gewicht mit Spule − Leerspule = Rest.
"""
import time
import uuid

TYPES = ["PLA", "PLA Matte", "PLA Silk", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "Support", "Sonstiges"]
DEFAULT_LOW = 150   # g – darunter gilt eine Spule als knapp

COLORS = [  # für Namensvorschläge
    ("Schwarz", (20, 20, 20)), ("Weiß", (245, 245, 245)), ("Grau", (128, 128, 128)), ("Silber", (192, 192, 192)),
    ("Rot", (200, 30, 30)), ("Orange", (240, 120, 20)), ("Gelb", (240, 210, 30)), ("Grün", (40, 160, 60)),
    ("Dunkelgrün", (20, 80, 40)), ("Türkis", (30, 180, 180)), ("Hellblau", (110, 170, 230)), ("Blau", (30, 70, 200)),
    ("Dunkelblau", (20, 30, 90)), ("Lila", (120, 50, 160)), ("Pink", (235, 100, 170)), ("Braun", (110, 70, 40)),
    ("Beige", (225, 205, 165)), ("Gold", (200, 160, 50)),
]


def norm_color(c):
    c = (c or "").strip().lower()
    if c and not c.startswith("#"):
        c = "#" + c
    return c[:7] if len(c) >= 7 else ""


def color_name(c):
    c = norm_color(c)
    if not c:
        return ""
    try:
        rgb = tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return ""
    return min(COLORS, key=lambda x: sum((a - b) ** 2 for a, b in zip(rgb, x[1])))[0]


def same_color(a, b, tol=40):
    a, b = norm_color(a), norm_color(b)
    if not a or not b:
        return not a and not b
    try:
        ra = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        rb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return a == b
    return max(abs(x - y) for x, y in zip(ra, rb)) <= tol


def same_type(a, b):
    a, b = (a or "").upper().strip(), (b or "").upper().strip()
    return a == b or a.split()[0:1] == b.split()[0:1] and bool(a) and bool(b)


def new_spool(**kw):
    s = {"id": uuid.uuid4().hex[:8], "name": "", "type": "PLA", "color": "", "brand": "", "total": 1000,
         "left": 1000.0, "tare": 0, "loc": "", "slot": "", "uuid": "", "added": int(time.time()), "warned": False,
         "used": 0.0}
    s.update({k: v for k, v in kw.items() if v is not None})
    s["color"] = norm_color(s["color"])
    if not s["name"]:
        s["name"] = suggest_name(s)
    return s


def suggest_name(s):
    parts = [s.get("brand") or "", s.get("type") or "", color_name(s.get("color"))]
    return " ".join(p for p in parts if p).strip() or "Spule"


def pct(s):
    try:
        return max(0, min(100, round(float(s["left"]) / float(s["total"]) * 100)))
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None


def fmt_left(s):
    p = pct(s)
    return f"{round(float(s.get('left') or 0))} g" + (f" · {p} %" if p is not None else "")


def slot_key(serial, unit_id, slot):
    return f"{serial}:{unit_id}:{slot}"


def slot_label(key, printers, units_by_serial):
    """„X2D · AMS 1 · Fach B“ aus dem Schlüssel."""
    try:
        serial, uid, slot = key.split(":", 2)
    except ValueError:
        return ""
    pname = printers.get(serial, serial)
    unit = next((u for u in units_by_serial.get(serial, []) if str(u["id"]) == uid), None)
    uname = unit["name"] if unit else ("Externe Spule" if uid == "254" else f"AMS {int(uid) + 1}" if uid.isdigit() else uid)
    return f"{pname} · {uname}" + ("" if uid == "254" else f" · Fach {slot}")


def trays(serial, units):
    """Alle belegten Fächer: [(key, unit, tray)]."""
    return [(slot_key(serial, u["id"], t["slot"]), u, t) for u in units or [] for t in u["trays"] if not t["empty"]]


def sync_ams(spools, serial, units):
    """Abgleich mit dem AMS. Gibt (geänderte Spulen-Namen, unbekannte Fächer [(key, tray)]) zurück.
    * RFID-Rolle bekannt → Fach zuordnen, Restmenge vom AMS übernehmen
    * Fach leer oder andere Rolle drin → Zuordnung lösen (Spule liegt wieder im Regal)"""
    changed, unknown = [], []
    occupied = {}
    for key, _u, t in trays(serial, units):
        occupied[key] = t
    # Zuordnungen lösen, die nicht mehr passen
    for s in spools:
        k = s.get("slot") or ""
        if not k.startswith(serial + ":"):
            continue
        t = occupied.get(k)
        if t is None or (t.get("uuid") and s.get("uuid") and t["uuid"] != s["uuid"]) or not same_type(t["type"], s["type"]) \
                or (t.get("color") and s.get("color") and not same_color(t["color"], s["color"])):
            s["slot"] = ""
            changed.append(s["name"])
    for key, t in occupied.items():
        sp = None
        if t.get("uuid") and t["uuid"].strip("0"):
            sp = next((s for s in spools if s.get("uuid") == t["uuid"]), None)
        if sp is None:
            sp = next((s for s in spools if s.get("slot") == key), None)
        if sp is None:
            unknown.append((key, t))
            continue
        if sp.get("slot") != key:
            for o in spools:
                if o is not sp and o.get("slot") == key:
                    o["slot"] = ""
            sp["slot"] = key
            changed.append(sp["name"])
        if sp.get("uuid") and t.get("grams") is not None and abs(float(sp.get("left") or 0) - t["grams"]) >= 1:
            sp["left"] = float(t["grams"])
            if t.get("weight"):
                sp["total"] = t["weight"]
            changed.append(sp["name"])
    return changed, unknown


def from_tray(key, t):
    """Neue Spule aus einem AMS-Fach (Werte vom Drucker)."""
    total = t.get("weight") or 1000
    left = t["grams"] if t.get("grams") is not None else total
    return new_spool(type=t.get("type") or "PLA", color=t.get("color") or "", brand="Bambu" if t.get("rfid") else "",
                     total=total, left=float(left), slot=key, uuid=t.get("uuid") if t.get("rfid") else "",
                     name=" ".join(x for x in (t.get("brand") or "", color_name(t.get("color"))) if x) or None)


def find_for(spools, serial, units, f):
    """Welche Spule hat ein Filament aus dem Druckauftrag {type, color, grams} geliefert?"""
    match = [(k, t) for k, _u, t in trays(serial, units)
             if same_type(t["type"], f.get("type")) and (not f.get("color") or same_color(t.get("color"), f["color"]))]
    for k, t in match:
        sp = next((s for s in spools if s.get("slot") == k), None)
        if sp:
            return sp, t
    # nicht im AMS zugeordnet: nur wenn es genau eine passende Spule im Lager gibt
    cand = [s for s in spools if not s.get("slot") and same_type(s["type"], f.get("type"))
            and (not f.get("color") or same_color(s.get("color"), f["color"])) and float(s.get("left") or 0) > 0]
    return (cand[0], None) if len(cand) == 1 else (None, None)


def consume(spools, serial, units, filaments, factor=1.0):
    """Verbrauch eines Drucks abziehen. Rollen mit RFID überspringen (die meldet das AMS selbst).
    → ([(name, gramm)], [nicht zuordenbare Filamente])"""
    done, missing = [], []
    for f in filaments or []:
        g = float(f.get("grams") or 0) * max(0.0, min(1.0, factor))
        if g <= 0:
            continue
        sp, t = find_for(spools, serial, units, f)
        if not sp:
            missing.append(f"{f.get('type') or '?'} {color_name(f.get('color')) or ''}".strip())
            continue
        sp["used"] = round(float(sp.get("used") or 0) + g, 1)
        if sp.get("uuid") and t is not None and t.get("grams") is not None:
            done.append((sp["name"], round(g, 1)))  # Restmenge kommt vom AMS
            continue
        sp["left"] = round(max(0.0, float(sp.get("left") or 0) - g), 1)
        done.append((sp["name"], round(g, 1)))
    return done, missing


def check_low(spools, limit=DEFAULT_LOW):
    """Spulen, die gerade knapp geworden sind (einmalig melden). → [spule]"""
    new = []
    for s in spools:
        left = float(s.get("left") or 0)
        if left < limit and not s.get("warned"):
            s["warned"] = True
            new.append(s)
        elif left >= limit + 50 and s.get("warned"):
            s["warned"] = False
    return new


def weigh(s, gross):
    """Nachwiegen: Gewicht mit Spule → Restmenge."""
    s["left"] = round(max(0.0, float(gross) - float(s.get("tare") or 0)), 1)
    return s["left"]


def summary(spools, limit=DEFAULT_LOW):
    low = [s for s in spools if float(s.get("left") or 0) < limit]
    return {"count": len(spools), "grams": round(sum(float(s.get("left") or 0) for s in spools)),
            "low": [s["name"] for s in low]}
