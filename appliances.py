# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
appliances.py – Waschmaschine, Trockner, Spülmaschine & Co.: Programmende am Stromverbrauch erkennen.

Ablauf je Gerät (hängt an einer FRITZ!DECT-Messsteckdose):
    bereit  → läuft    sobald die Leistung 1 Minute lang über „start_w“ liegt
    läuft   → fertig   wenn sie danach „end_min“ Minuten am Stück unter „end_w“ bleibt
                       (Pausen beim Einweichen/Trocknen sind kürzer – deshalb die Wartezeit)
    fertig  → Erinnerung nach „remind_min“ Minuten (optional), nach 6 Stunden wieder „bereit“

    defaults(kind)                     → Schwellwerte je Geräteart
    step(st, a, watt, now) → (st, ev)  ev = None | "start" | "done" | "remind" | "abort"
    message(a, st, ev, price_ct)       → (Titel, Text)
"""

KINDS = {
    "wash": {"icon": "🧺", "name": "Waschmaschine", "start_w": 15, "end_w": 4, "end_min": 5, "min_run": 10,
             "remind": "Wäsche ausräumen nicht vergessen 😉"},
    "dry": {"icon": "🌀", "name": "Trockner", "start_w": 50, "end_w": 10, "end_min": 3, "min_run": 10,
            "remind": "Trockner ausräumen – sonst knittert's 😉"},
    "dish": {"icon": "🍽", "name": "Spülmaschine", "start_w": 15, "end_w": 3, "end_min": 15, "min_run": 15,
             "remind": "Spülmaschine ausräumen 😉"},
    "other": {"icon": "🔌", "name": "Gerät", "start_w": 10, "end_w": 3, "end_min": 5, "min_run": 5,
              "remind": "Bitte nachsehen 😉"},
}
KIND_CHOICES = [(k, f"{v['icon']} {v['name']}") for k, v in KINDS.items()]
CONFIRM_S = 60          # so lange über start_w, bevor es als „läuft“ gilt
RESET_AFTER_S = 6 * 3600


def defaults(kind):
    k = KINDS.get(kind) or KINDS["other"]
    return {x: k[x] for x in ("start_w", "end_w", "end_min", "min_run")}


def norm(a):
    """Gespeichertes Gerät mit Standardwerten ergänzen."""
    out = dict(defaults(a.get("kind", "wash")))
    out.update({k: v for k, v in a.items() if v not in (None, "")})
    out.setdefault("remind_min", 0)
    out.setdefault("enabled", True)
    return out


def icon(a):
    return (KINDS.get(a.get("kind")) or KINDS["other"])["icon"]


def new_state():
    return {"state": "idle", "above": None, "below": None, "start": None, "wh": 0.0, "last": None,
            "done": None, "dur": 0, "reminded": False}


def step(st, a, watt, now):
    """Einen Messwert verarbeiten. Gibt (neuer Zustand, Ereignis) zurück."""
    st = dict(st or new_state())
    a = norm(a)
    if watt is None:
        st["last"] = None
        return st, None
    last, st["last"] = st.get("last"), now
    ev = None
    if st["state"] == "run":
        if last is not None and 0 < now - last < 600:
            st["wh"] += watt * (now - last) / 3600
        if watt < a["end_w"]:
            st["below"] = st["below"] or now
            if now - st["below"] >= a["end_min"] * 60:
                if st["below"] - st["start"] >= a["min_run"] * 60:
                    st.update(state="done", done=now, dur=int(st["below"] - st["start"]), reminded=False, above=None)
                    ev = "done"
                else:  # nur kurz angegangen (z. B. Tür auf, Display an) – war kein Programm
                    st.update(state="idle", above=None, below=None, start=None, wh=0.0)
                    ev = "abort"
        else:
            st["below"] = None
        return st, ev
    # bereit oder fertig: neuer Lauf?
    if watt >= a["start_w"]:
        st["above"] = st["above"] or now
        if now - st["above"] >= CONFIRM_S:
            st.update(state="run", start=st["above"], below=None, wh=0.0, reminded=False)
            return st, "start"
    else:
        st["above"] = None
    if st["state"] == "done":
        if a.get("remind_min") and not st["reminded"] and now - st["done"] >= float(a["remind_min"]) * 60:
            st["reminded"] = True
            ev = "remind"
        if now - st["done"] >= RESET_AFTER_S:
            st.update(state="idle", done=None)
    return st, ev


def fmt_dur(sec):
    m = int(sec // 60)
    return f"{m // 60}:{m % 60:02d} Std." if m >= 60 else f"{m} Min."


def message(a, st, ev, price_ct=None):
    k = KINDS.get(a.get("kind")) or KINDS["other"]
    name = a.get("name") or k["name"]
    if ev == "done":
        bits = [f"Lief {fmt_dur(st['dur'])}"]
        kwh = st.get("wh", 0) / 1000
        if kwh >= 0.01:
            bits.append(f"{kwh:.2f} kWh".replace(".", ","))
            if price_ct:
                bits.append(f"≈ {kwh * float(price_ct) / 100:.2f} €".replace(".", ","))
        return f"{k['icon']} {name} ist fertig", " · ".join(bits)
    if ev == "remind":
        return f"{k['icon']} {name} wartet seit {fmt_dur(float(a.get('remind_min') or 0) * 60)}", k["remind"]
    if ev == "start":
        return f"{k['icon']} {name} läuft", ""
    return "", ""


def status(a, st, now):
    """Kurztext für Kachel/Handy."""
    st = st or new_state()
    if st["state"] == "run":
        return "läuft", f"seit {fmt_dur(max(0, now - st['start']))}"
    if st["state"] == "done":
        return "fertig", f"seit {fmt_dur(max(0, now - st['done']))}"
    return "bereit", ""
