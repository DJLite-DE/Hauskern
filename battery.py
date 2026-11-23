# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
battery.py – Batterie-Übersicht für FRITZ!DECT-Geräte (Heizkörperregler, Taster, Sensoren).

Hauskern merkt sich pro Gerät einen Batteriewert pro Tag. Daraus:
  * Batteriewechsel erkennen (Wert springt deutlich nach oben)
  * schätzen, wie lange die Batterie noch reicht (lineare Abnahme seit dem letzten Wechsel)
"""
import time

DAY = 86400
JUMP = 25          # % – so viel höher als vorher = neue Batterien
EMPTY_AT = 10      # % – ab hier meldet die FRITZ!Box meist „Batterie schwach“
MIN_SPAN = 14      # Tage – kürzer ist für eine Schätzung zu unsicher
MIN_DROP = 4       # % – so viel muss es seit dem Wechsel gefallen sein
REMIND_DAYS = 21   # so früh vor „leer“ erinnert Hauskern


def last_change(samples):
    """Index des letzten Batteriewechsels in [(ts, pct)] (0 = keiner erkannt)."""
    idx = 0
    for i in range(1, len(samples)):
        if samples[i][1] - samples[i - 1][1] >= JUMP:
            idx = i
    return idx


def estimate(samples, now=None):
    """samples: [(ts, pct)] aufsteigend. → {pct, changed, days_left, per_month} (Werte ggf. None)."""
    now = now or time.time()
    if not samples:
        return {"pct": None, "changed": None, "days_left": None, "per_month": None}
    i0 = last_change(samples)
    seg = samples[i0:]
    pct = seg[-1][1]
    res = {"pct": pct, "changed": samples[i0][0] if i0 else None, "days_left": None, "per_month": None}
    span = (seg[-1][0] - seg[0][0]) / DAY
    if len(seg) < 3 or span < MIN_SPAN or seg[0][1] - pct < MIN_DROP:
        return res
    # lineare Regression pct über Tage
    xs = [(t - seg[0][0]) / DAY for t, _ in seg]
    ys = [p for _, p in seg]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return res
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den  # % pro Tag (negativ)
    if slope >= -0.005:
        return res
    res["per_month"] = round(-slope * 30, 1)
    left = (pct - EMPTY_AT) / -slope - (now - seg[-1][0]) / DAY
    res["days_left"] = max(0, round(left))
    return res


def fmt_left(days):
    if days is None:
        return "noch unbekannt"
    if days <= 3:
        return "fast leer"
    if days < 14:
        return f"noch ca. {days} Tage"
    if days < 60:
        return f"noch ca. {round(days / 7)} Wochen"
    if days < 365:
        return f"noch ca. {round(days / 30)} Monate"
    y = days / 365
    return "noch über 1 Jahr" if y < 2 else f"noch ca. {round(y)} Jahre"


def overview(devs, history, now=None):
    """devs: FRITZ!DECT-Geräte (mit battery/batterylow), history: {ain: [(ts, pct)]}.
    → sortierte Liste: schwach zuerst, dann was am frühesten leer ist."""
    now = now or time.time()
    out = []
    for d in devs:
        if d.get("is_group") or d.get("battery") in (None, ""):
            continue
        try:
            pct = int(float(d["battery"]))
        except (TypeError, ValueError):
            continue
        e = estimate(history.get(d["ain"], []), now)
        out.append({"ain": d["ain"], "name": d["name"], "product": d.get("product", ""), "pct": pct,
                    "low": bool(d.get("batterylow")), "present": bool(d.get("present", True)),
                    "days_left": e["days_left"], "left_text": "Batterie schwach – bitte tauschen" if d.get(
                        "batterylow") else fmt_left(e["days_left"]),
                    "changed": e["changed"], "per_month": e["per_month"]})
    out.sort(key=lambda x: (not x["low"], x["days_left"] if x["days_left"] is not None else 99999, x["pct"]))
    return out


def worst(items):
    return items[0] if items else None
