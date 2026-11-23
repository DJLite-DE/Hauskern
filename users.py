# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
users.py – mehrere Handy-Nutzer mit eigener PIN und eigenen Rechten.

Rechte je Bereich: 0 = aus, 1 = ansehen, 2 = steuern.
Ein Nutzer: {"id", "name", "pin" (Hash), "admin": bool, "rights": {bereich: stufe}, "rev"}
"rev" ändert sich bei neuer PIN oder „überall abmelden“ – alte Anmeldungen werden damit ungültig.
Sitzungen: [{"h": Token-Hash, "u": Nutzer-ID, "rev", "ts"}]
"""
import secrets
import time

# (Schlüssel, Bezeichnung, höchste Stufe, Erklärung)
AREAS = [
    ("home", "Personen & Termine", 1, "wer zu Hause ist, Kalender"),
    ("smart", "Smart Home & Szenen", 2, "Steckdosen, Heizung, Szenen"),
    ("cams", "Kameras", 2, "Livebild; steuern = schwenken"),
    ("printers", "3D-Drucker", 2, "Status, AMS; steuern = Pause/Stopp/Licht"),
    ("net", "Netzwerk & WLAN", 2, "WLAN, Gast-WLAN, Geräte, Aufwecken"),
    ("power", "Stromkosten", 1, "Kosten pro Monat und Gerät"),
    ("calc", "Druckkosten-Rechner", 2, "steuern = Kalkulationen speichern/löschen"),
    ("history", "Verlauf", 1, "Diagramme"),
    ("system", "Diagnose", 1, "Prüfung von Hauskern"),
    ("automations", "Automationen", 2, "ansehen; steuern = anlegen, ändern, löschen"),
    ("calls", "Anrufliste", 1, "wer angerufen hat, verpasste Anrufe"),
    ("lists", "Einkaufsliste & Aufgaben", 2, "ansehen; steuern = eintragen und abhaken"),
]
# Neue Bereiche: Was gilt für Nutzer, die angelegt wurden, bevor es den Bereich gab?
NEW_AREA_DEFAULT = {"lists": 2}  # die Einkaufsliste ist für alle da
AREA_KEYS = [a[0] for a in AREAS]
MAX = {a[0]: a[2] for a in AREAS}
LEVEL_TEXT = {0: "aus", 1: "ansehen", 2: "steuern"}

PRESETS = [
    ("Vollzugriff", {k: MAX[k] for k in AREA_KEYS}),
    ("Alles sehen, Zuhause steuern", dict({k: 1 for k in AREA_KEYS}, smart=2, net=1, automations=1)),
    ("Nur ansehen", {k: 1 for k in AREA_KEYS}),
    ("Eingeschränkt (z. B. Kinder)", dict({k: 0 for k in AREA_KEYS}, home=1, smart=2, lists=2)),
]

# Teile der Handy-Oberfläche → Bereich
STATE_AREA = {"persons": "home", "agenda": "home", "scenes": "smart", "smart": "smart", "energy": "smart",
              "cams": "cams", "printers": "printers", "net": "net", "wlan": "net", "guest": "net", "wol": "net",
              "devices": "net", "diag": "system", "health": "system",
              "battery": "smart", "filament": "printers", "heating": "smart", "calls": "calls", "plugins": "smart", "lists": "lists",
              "vacation": "smart", "appliances": "smart"}
EMPTY = {"persons": [], "agenda": [], "scenes": [], "smart": [], "energy": None, "cams": [], "printers": [],
         "net": {}, "wlan": [], "guest": None, "wol": [], "devices": [], "diag": None, "health": None,
         "battery": None, "filament": None, "heating": None, "calls": None, "plugins": [], "lists": None,
         "vacation": None, "appliances": []}
# Aktions-Schlüssel (vor dem ":") → Bereich
ACTION_AREA = {"dect": "smart", "scene": "smart", "cam": "cams", "printer": "printers", "wlan": "net",
               "guest": "net", "wol": "net", "sys": "system"}


def full_rights():
    return {k: MAX[k] for k in AREA_KEYS}


def new_user(name, pin_hash, rights=None, admin=False):
    return {"id": "u" + secrets.token_hex(4), "name": name, "pin": pin_hash, "admin": bool(admin),
            "rights": full_rights() if admin else dict(rights or PRESETS[1][1]), "rev": secrets.token_hex(4)}


def level(user, area):
    if not user:
        return 0
    if user.get("admin"):
        return MAX.get(area, 2)
    r = user.get("rights") or {}
    return min(int(r.get(area, NEW_AREA_DEFAULT.get(area, 0))), MAX.get(area, 2))


def can(user, area, need=1):
    return level(user, area) >= need


def action_area(key):
    return ACTION_AREA.get(str(key).partition(":")[0], "smart")


def action_need(key, action):
    """Welche Stufe braucht eine Aktion? Diagnose starten zählt als „ansehen“."""
    return 1 if str(key) == "sys:diag" else 2


def filter_state(state, user):
    st = dict(state or {})
    for k, area in STATE_AREA.items():
        if k in st and not can(user, area, 1):
            st[k] = EMPTY.get(k)
    st["me"] = {"name": (user or {}).get("name", ""), "admin": bool((user or {}).get("admin")),
                "rights": {k: level(user, k) for k in AREA_KEYS}, "tfa": bool((user or {}).get("totp"))}
    return st


def describe(user):
    if user.get("admin"):
        return "Vollzugriff (Admin)"
    r = user.get("rights") or {}
    for name, preset in PRESETS:
        if all(int(r.get(k, 0)) == v for k, v in preset.items()):
            return name
    on = [a[1] for a in AREAS if int(r.get(a[0], 0)) == 2]
    return "Steuern: " + (", ".join(on) if on else "nichts")


def migrate(cfg):
    """Alte Einzel-PIN → erster Nutzer (Admin). Alte Sitzungen bleiben gültig. Gibt True zurück, wenn geändert."""
    changed = False
    users = cfg.get("web_users")
    if not users and cfg.get("web_pin"):
        u = new_user("Hauptnutzer", cfg["web_pin"], admin=True)
        u["id"] = "u0"
        cfg["web_users"] = [u]
        changed = True
    users = cfg.get("web_users") or []
    sess = cfg.get("web_sessions") or []
    if any(isinstance(s, str) for s in sess):
        admin = next((u for u in users if u.get("admin")), users[0] if users else None)
        cfg["web_sessions"] = [{"h": s, "u": admin["id"], "rev": admin["rev"], "ts": int(time.time())}
                               if isinstance(s, str) else s for s in sess if admin or not isinstance(s, str)]
        changed = True
    first_admin = next((u for u in users if u.get("admin")), None)
    if first_admin and cfg.get("web_pin") != first_admin["pin"]:
        cfg["web_pin"] = first_admin["pin"]  # für ältere Stellen, die „ist eine PIN gesetzt?“ fragen
        changed = True
    return changed


def session_user(cfg, h):
    users = {u["id"]: u for u in cfg.get("web_users") or []}
    for s in cfg.get("web_sessions") or []:
        if isinstance(s, dict) and s.get("h") == h:
            u = users.get(s.get("u"))
            if u and u.get("rev") == s.get("rev"):
                return u
    return None


def add_session(cfg, h, user, keep=60):
    lst = [s for s in cfg.get("web_sessions") or [] if isinstance(s, dict) and s.get("h") != h]
    users = {u["id"]: u for u in cfg.get("web_users") or []}
    lst = [s for s in lst if s.get("u") in users and users[s["u"]].get("rev") == s.get("rev")]  # Ungültige aufräumen
    lst.append({"h": h, "u": user["id"], "rev": user["rev"], "ts": int(time.time())})
    cfg["web_sessions"] = lst[-keep:]


def count_sessions(cfg, user):
    return sum(1 for s in cfg.get("web_sessions") or []
               if isinstance(s, dict) and s.get("u") == user["id"] and s.get("rev") == user.get("rev"))


def find_by_pin(users, pin, check_pin):
    for u in users or []:
        if pin and check_pin(pin, u.get("pin", "")):
            return u
    return None
