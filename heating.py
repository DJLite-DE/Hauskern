# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
heating.py – Heizungs-Check: Hinweise aus dem Temperaturverlauf der FRITZ!DECT-Heizkörperregler.

Hauskern stellt dabei nichts um – es gibt nur Hinweise. Was du daraus machst, entscheidest du.

Pro Raum (15-Minuten-Raster, Standard: letzte 14 Tage):
  * Aufheizzeit     – wie lange es nach dem Hochdrehen dauert, bis die Wunschtemperatur erreicht ist
  * Wird nicht warm – Wunschtemperatur wird oft deutlich verfehlt (Heizkörper entlüften? Ventil?)
  * Nachtabsenkung  – nachts genauso warm wie tagsüber?
  * Niemand zu Hause – geheizt, während alle unterwegs sind (aus der Anwesenheit)
  * Fenster offen   – lange offen, während geheizt wird
  * Auskühlen       – kühlt ein Raum deutlich schneller aus als die anderen?
"""
import statistics
import time

STEP = 900          # 15 Minuten
COMFORT = 18.0      # ab dieser Solltemperatur zählt es als „heizen“
SAVE_PER_DEG = 6    # Faustregel: 1 °C weniger ≈ 6 % weniger Heizenergie


def fd(v, d=1):
    return f"{v:.{d}f}".replace(".", ",")


def grid(rows):
    """[(ts, mittel, …)] → {ts15: wert}"""
    return {int(r[0]) // STEP * STEP: r[1] for r in rows if r[1] is not None}


def away_mask(events, person_names, start, end):
    """Aus Logbuch-Einträgen (ts, kind, name, text) → Funktion ts → True, wenn sicher alle weg sind."""
    persons = [n for n in person_names]
    if not persons:
        return None
    ev = sorted((e for e in events if e[2] in persons), key=lambda e: e[0])
    # Zustand vor dem ersten Eintrag ableiten: erster Eintrag „nach Hause gekommen“ → vorher weg
    first = {}
    for ts, _k, name, text in ev:
        first.setdefault(name, "away" if "nach Hause" in text else "home")
    timeline = {n: [(start - 1, first.get(n))] for n in persons}
    for ts, _k, name, text in ev:
        timeline[name].append((ts, "home" if "nach Hause" in text else "away"))

    def state(name, ts):
        st = None
        for t0, s in timeline[name]:
            if t0 <= ts:
                st = s
            else:
                break
        return st

    def all_away(ts):
        return all(state(n, ts) == "away" for n in persons)
    return all_away


def _heatups(ist, soll):
    """Minuten bis zur Wunschtemperatur nach jedem Hochdrehen (≥ 1,5 °C). None = in 4 h nicht erreicht."""
    out = []
    ts_sorted = sorted(soll)
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        if b - a != STEP or soll[b] - soll[a] < 1.5 or soll[b] < COMFORT:
            continue
        if ist.get(b) is not None and ist[b] >= soll[b] - 0.5:
            continue  # war schon warm
        target, reached = soll[b], None
        for k in range(1, 17):
            t = b + k * STEP
            if soll.get(t) is not None and soll[t] < target - 0.6:
                break  # wieder runtergedreht
            if ist.get(t) is not None and ist[t] >= target - 0.5:
                reached = k * 15
                break
        else:
            out.append(None)
            continue
        if reached is not None:
            out.append(reached)
    return out


def _cooldown(ist, soll, off):
    """°C pro Stunde, die der Raum nachts verliert, wenn nicht geheizt wird."""
    rates = []
    ts = sorted(ist)
    for a in ts:
        h = time.localtime(a).tm_hour
        if not 0 <= h <= 4:
            continue
        b = a + 4 * STEP
        if b not in ist:
            continue
        heating = any((soll.get(a + k * STEP) or 0) > ist[a] - 1 and not off.get(a + k * STEP) for k in range(5))
        if heating:
            continue
        rates.append(ist[a] - ist[b])
    return statistics.median(rates) if len(rates) >= 6 else None


def analyze_room(name, ist_rows, soll_rows, off_rows=(), win_rows=(), all_away=None, days=14):
    ist, soll, off, win = grid(ist_rows), grid(soll_rows), grid(off_rows), grid(win_rows)
    span_days = (max(ist) - min(ist)) / 86400 if len(ist) > 1 else 0
    room = {"name": name, "days": round(span_days, 1), "findings": [], "avg_ist": None, "avg_soll": None,
            "heat_h_week": None, "heatup_min": None, "cool_h": None}
    if span_days < 3 or len(ist) < 200:
        room["findings"].append(("info", f"Noch zu wenig Daten ({span_days:.0f} Tage) – nach ein paar Tagen wird es "
                                         "aussagekräftig."))
        return room
    room["avg_ist"] = round(statistics.mean(ist.values()), 1)
    heat = [t for t, v in soll.items() if v >= COMFORT and not off.get(t)]
    if soll:
        room["avg_soll"] = round(statistics.mean(v for t, v in soll.items() if not off.get(t)), 1) if heat else None
    week = 7 / max(1.0, span_days)
    room["heat_h_week"] = round(len(heat) * STEP / 3600 * week)
    f = room["findings"]

    # Aufheizen
    ups = _heatups(ist, soll)
    ok = [m for m in ups if m is not None]
    if len(ups) >= 3:
        if ok:
            room["heatup_min"] = round(statistics.median(ok))
        miss = sum(1 for m in ups if m is None)
        if miss >= max(2, len(ups) / 2):
            f.append(("warn", f"Erreicht die Wunschtemperatur nach dem Hochdrehen oft gar nicht ({miss} von {len(ups)} "
                              "Mal in 4 Stunden). Heizkörper entlüften, Ventil prüfen – oder die Vorlauftemperatur "
                              "der Heizung ist zu niedrig."))
        elif room["heatup_min"] and room["heatup_min"] >= 90:
            f.append(("tip", f"Braucht lange zum Aufheizen: im Schnitt {room['heatup_min']} Minuten. Tipp: die "
                             "Heizzeit etwas früher starten lassen – oder Heizkörper entlüften."))
        elif room["heatup_min"]:
            f.append(("ok", f"Heizt zügig auf: im Schnitt {room['heatup_min']} Minuten bis zur Wunschtemperatur."))

    # Wird nicht warm genug (nach der ersten Stunde einer Heizphase)
    settled = [t for t in heat if all(soll.get(t - k * STEP) == soll[t] for k in range(1, 5)) and t in ist]
    if len(settled) >= 40:
        cold = sum(1 for t in settled if ist[t] < soll[t] - 1.5)
        share = cold / len(settled)
        if share >= 0.3 and not any(x[0] == "warn" for x in f):
            f.append(("warn", f"Wird oft nicht warm genug: In {round(share * 100)} % der Heizzeit mehr als 1,5 °C "
                              "unter der Wunschtemperatur."))

    # Nachtabsenkung
    night = [v for t, v in soll.items() if time.localtime(t).tm_hour in (1, 2, 3, 4) and not off.get(t)]
    day = [v for t, v in soll.items() if 8 <= time.localtime(t).tm_hour <= 20 and not off.get(t)]
    if len(night) >= 20 and len(day) >= 40:
        n_avg, d_avg = statistics.mean(night), statistics.mean(day)
        if d_avg >= 19 and n_avg >= d_avg - 0.5:
            f.append(("tip", f"Keine Nachtabsenkung: nachts wie tagsüber ca. {fd(d_avg)} °C. Faustregel: 1 °C weniger "
                             f"spart etwa {SAVE_PER_DEG} % Heizenergie."))
        elif n_avg <= d_avg - 2:
            f.append(("ok", f"Nachtabsenkung aktiv (nachts ca. {fd(n_avg)} °C)."))

    # Niemand zu Hause
    if all_away:
        away_heat = [t for t in heat if all_away(t)]
        h = len(away_heat) * STEP / 3600 * week
        if h >= 8:
            f.append(("tip", f"Heizt, während niemand zu Hause ist: ca. {round(h)} Stunden pro Woche. Wenn du magst, "
                             "kannst du dir eine Automation „Alle weg → Heizung runter“ anlegen."))

    # Fenster offen beim Heizen
    if win:
        open_heat = [t for t, v in win.items() if v >= 0.5 and (soll.get(t) or 0) >= COMFORT]
        h = len(open_heat) * STEP / 3600 * week
        if h >= 3:
            f.append(("tip", f"Fenster oft lange offen, während geheizt wird: ca. {round(h)} Stunden pro Woche. "
                             "Lieber kurz und kräftig stoßlüften."))

    room["cool_h"] = _cooldown(ist, soll, off)
    return room


def compare_cooling(rooms):
    """Raum, der deutlich schneller auskühlt als die anderen → Hinweis."""
    rates = [(r, r["cool_h"]) for r in rooms if r.get("cool_h") is not None]
    if len(rates) < 2:
        return
    med = statistics.median(c for _, c in rates)
    for r, c in rates:
        if c >= 0.4 and c >= med * 1.6:
            r["findings"].append(("tip", f"Kühlt nachts schneller aus als die anderen Räume ({fd(c)} °C pro Stunde). "
                                         "Fenster dicht? Rollladen runter? Heizkörper frei?"))


def summary(rooms):
    tips = sum(1 for r in rooms for x in r["findings"] if x[0] in ("tip", "warn"))
    warn = sum(1 for r in rooms for x in r["findings"] if x[0] == "warn")
    return {"rooms": len(rooms), "tips": tips, "warn": warn}
