# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
voice.py – Alexa & Google Home (über Matter).

Hauskern entscheidet hier, WAS nach außen geht und WIE es dort heißt. Die eigentliche Matter-Bridge
(Matterbridge + Hauskern-Plugin auf dem Heimserver) fragt Hauskern über /api/bridge ab und schaltet darüber.
Nur Geräte, die du ausdrücklich freigibst, sind für die Sprachassistenten sichtbar.

Gerätetypen (so legt die Bridge sie in Matter an):
  plug       – Steckdose/Schalter/Lampe an/aus          → „Alexa, Schreibtisch aus“
  thermostat – Heizkörperregler mit Solltemperatur       → „Hey Google, Wohnzimmer auf 21 Grad“
  scene      – Szene als Schalter (geht danach wieder aus) → „Alexa, Feierabend an“
  presence   – Person/Gegenstand da (Belegungssensor)    → für Routinen in der Alexa-/Google-App
  printer    – 3D-Drucker druckt (Belegungssensor)       → z. B. Ansage, wenn der Druck fertig ist
"""
import re
import time

TYPE_TEXT = {"plug": "Schalter", "thermostat": "Heizung", "scene": "Szene", "presence": "Anwesenheit",
             "printer": "3D-Drucker"}
GROUPS = [("plug", "🔌 Steckdosen, Schalter & Lampen"), ("thermostat", "🌡 Heizung"), ("scene", "▶ Szenen"),
          ("presence", "🏠 Anwesenheit (für Routinen)"), ("printer", "🖨️ 3D-Drucker (für Routinen)")]


def _speakable(name):
    """Namen für Sprachbefehle: Emojis und Sonderzeichen raus."""
    n = re.sub(r"[^\w\s\-äöüÄÖÜß.]", "", name or "").strip()
    return re.sub(r"\s+", " ", n) or "Gerät"


def candidates(app):
    """Alles, was Hauskern freigeben könnte."""
    out = []
    for d in app.data.get("smart", []):
        if d.get("is_group") and not (d.get("switch") is not None):
            continue
        if d.get("hkr"):
            out.append({"id": "therm:" + d["ain"], "type": "thermostat", "default": _speakable(d["name"]),
                        "label": d["name"]})
        elif d.get("switch") is not None or d.get("simpleonoff") is not None:
            out.append({"id": "plug:" + d["ain"], "type": "plug", "default": _speakable(d["name"]),
                        "label": f"{d['name']} ({d.get('kind') or 'Schalter'})"})
    for r in app.cfg.get("automations", []):
        if r.get("trigger", {}).get("type") == "manual":
            out.append({"id": "scene:" + r["id"], "type": "scene", "default": _speakable(r["name"]), "label": r["name"]})
    for p in app.cfg.get("persons", []):
        out.append({"id": "presence:" + p["id"], "type": "presence",
                    "default": _speakable(p["name"]) + (" da" if p.get("kind") == "item" else " zu Hause"),
                    "label": p["name"]})
    for serial, pr in getattr(app, "printers", {}).items():
        out.append({"id": "printer:" + serial, "type": "printer", "default": _speakable(pr.name) + " druckt",
                    "label": pr.name})
    order = [g for g, _ in GROUPS]
    out.sort(key=lambda c: (order.index(c["type"]), c["label"].lower()))
    return out


def selection(app):
    return dict((app.cfg.get("voice") or {}).get("items") or {})


def save_selection(app, enabled, items):
    """items: {id: {"on": bool, "name": str}} – prüft doppelte Namen."""
    clean, seen = {}, {}
    cands = {c["id"]: c for c in candidates(app)}
    for cid, v in items.items():
        if cid not in cands:
            continue
        name = _speakable(v.get("name") or cands[cid]["default"])[:40]
        on = bool(v.get("on"))
        if on:
            k = name.lower()
            if k in seen:
                raise ValueError(f"Der Name „{name}“ ist doppelt – Alexa und Google brauchen eindeutige Namen")
            seen[k] = cid
        clean[cid] = {"on": on, "name": name}
    import secrets
    v = dict(app.cfg.get("voice") or {})
    v.update(enabled=bool(enabled), items=clean)
    v.setdefault("token", secrets.token_urlsafe(24))
    app.cfg["voice"] = v
    app.save()
    return sum(1 for x in clean.values() if x["on"])


def _state(app, c):
    typ, ident = c["type"], c["id"].split(":", 1)[1]
    if typ in ("plug", "thermostat"):
        d = app._dev(ident)
        if not d:
            return {"reachable": False}
        if typ == "plug":
            on = d["switch"] if d.get("switch") is not None else d.get("simpleonoff")
            return {"reachable": bool(d.get("present")), "on": bool(on)}
        h = d.get("hkr") or {}
        soll = h.get("soll")
        return {"reachable": bool(d.get("present")), "current": h.get("ist") if isinstance(h.get("ist"), (int, float))
                else d.get("temp"), "target": soll if isinstance(soll, (int, float)) else None,
                "mode": "off" if soll == "AUS" else "heat", "window_open": bool(h.get("window_open"))}
    if typ == "scene":
        return {"reachable": True, "on": False}
    if typ == "presence":
        return {"reachable": True, "occupied": app.presence_snapshot().get(ident) == "home"}
    if typ == "printer":
        pr = getattr(app, "printers", {}).get(ident)
        if not pr:
            return {"reachable": False}
        s = pr.summary()
        return {"reachable": bool(getattr(pr, "connected", False)), "occupied": s.get("state") in ("RUNNING", "PREPARE")}
    return {"reachable": False}


def devices(app):
    """Was die Bridge sieht: nur freigegebene Geräte."""
    v = app.cfg.get("voice") or {}
    if not v.get("enabled"):
        return []
    sel = v.get("items") or {}
    out = []
    for c in candidates(app):
        s = sel.get(c["id"])
        if s and s.get("on"):
            out.append({"id": c["id"], "type": c["type"], "name": s.get("name") or c["default"], **_state(app, c)})
    return out


def command(app, cid, cmd, value=None):
    """Befehl von Alexa/Google (über die Bridge). Nur für freigegebene Geräte."""
    v = app.cfg.get("voice") or {}
    s = (v.get("items") or {}).get(cid)
    if not v.get("enabled") or not s or not s.get("on"):
        return False, "nicht freigegeben"
    typ, _, ident = cid.partition(":")
    if typ == "plug" and cmd in ("on", "off"):
        msg = app.run_action({"type": "switch", "ain": ident, "mode": cmd})
    elif typ == "therm" and cmd == "setpoint":
        val = max(8.0, min(28.0, round(float(value) * 2) / 2))
        msg = app.run_action({"type": "thermostat", "ain": ident, "value": val})
    elif typ == "therm" and cmd == "off":
        app.jobs.put(("action", lambda c, a=ident: c.set_thermostat(a, "aus")))  # Wort, keine Zahl: sonst 28 °C
        msg = f"{s.get('name')} aus"
    elif typ == "therm" and cmd == "on":
        msg = app.run_action({"type": "thermostat", "ain": ident, "value": 20.0})
    elif typ == "scene" and cmd == "on":
        rule = next((r for r in app.cfg.get("automations", []) if r["id"] == ident), None)
        if not rule:
            return False, "Szene nicht gefunden"
        app.engine.trigger(rule, reason="Sprachassistent")
        msg = f"Szene „{rule['name']}“ gestartet"
    elif typ == "scene" and cmd == "off":
        return True, "Szene ist ein Taster"
    else:
        return False, "Befehl passt nicht zum Gerät"
    app.log("Sprache", s.get("name"), msg)
    v["last_cmd"] = time.time()
    return True, msg
