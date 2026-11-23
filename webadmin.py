# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
webadmin.py – Einrichten & Einstellen über das Handy (für den Heimserver ohne Bildschirm).

Jeder Bereich liefert Felder in einer einfachen Beschreibung, die die Handy-Seite selbst darstellt:
    {"key", "label", "kind": text|password|number|time|choice|multi|bool|days|info, "opts": [[wert, text], …], "hint"}

    get(app, section, ident, user)          → dict für die Anzeige
    post(app, section, op, ident, vals, user) → (ok, meldung, extra)

Läuft im Haupt-Thread der App (über app.main_call), damit nichts gleichzeitig an den Einstellungen dreht.
Wichtig: Hier wird nur gespeichert, was der Nutzer eingibt – nie etwas von selbst angelegt.
"""
import re
import time

import users as U

ADMIN_SECTIONS = [
    ("router", "📡 Router", "FRITZ!Box oder anderer Router"),
    ("persons", "🏠 Personen & Gegenstände", "Anwesenheit über das WLAN"),
    ("devices", "🔌 Geräte", "Geräte aus dem Netz mit Kachel"),
    ("printers", "🖨️ 3D-Drucker", "Bambu Lab im LAN"),
    ("cameras", "📷 Kameras", "Tapo, Reolink, ONVIF, RTSP"),
    ("users", "👥 Handy-Nutzer & Rechte", "PINs und wer was darf"),
    ("push", "🔔 Push-Nachrichten", "ntfy, Pushover, Telegram"),
    ("power", "⚡ Strompreis & Warnungen", "Arbeitspreis, Budget"),
    ("weather", "🌦 Wetter", "Ort automatisch oder fest"),
    ("voice", "🎙 Alexa & Google Home", "Was die Sprachassistenten sehen dürfen"),
    ("backup", "💾 Sicherung", "Jede Nacht automatisch – wiederherstellen"),
    ("timelapse", "🎬 Zeitraffer", "Videos von Drucken – standardmäßig aus"),
    ("clips", "🎥 Kamera-Aufnahmen", "Speichergrenze – ältere werden automatisch gelöscht"),
    ("vacation", "🏖 Urlaubsmodus", "Licht wie zu Hause, Heizung sparen, Kamera-Push"),
    ("appliances", "🧺 Waschmaschine & Co.", "Meldet, wenn das Programm fertig ist"),
    ("integrations", "🔌 Weitere Hersteller", "Shelly, Philips Hue, Tasmota, ESPHome, Zigbee, Sky Q, Fernseher"),
    ("filament", "🧵 Filament-Lager", "Spulen, Restmengen, AMS-Fächer"),
    ("wall", "🖼 Wand-Display", "Altes Tablet als Info-Bildschirm an der Wand"),
    ("vision", "🧠 Kamera-Erkennung", "Person, Katze, Hund, Fahrzeug – lokal, standardmäßig aus"),
    ("plugins", "🧩 Erweiterungen", "Zusätzliche Kacheln, Siri-Befehle und Abläufe"),
]
# Bereiche, die auch Nicht-Admins mit passenden Rechten öffnen dürfen: Bereich → (Recht, lesen, ändern)
SECTION_AREA = {"filament": ("printers", 1, 2), "siri": ("*", 1, 1), "bugreport": ("*", 1, 1), "twofa": ("*", 1, 1),
                "vacation": ("smart", 1, 2),
                "appliances": ("smart", 1, 2)}
SETUP_FLOW = ["router", "persons", "printers", "cameras", "push"]


class Bad(Exception):
    """Eingabe ungültig – Text geht so ans Handy."""


def F(key, label, kind="text", opts=None, hint=""):
    d = {"key": key, "label": label, "kind": kind}
    if opts is not None:
        d["opts"] = [[v, n] for v, n in opts]
    if hint:
        d["hint"] = hint
    return d


def _num(v, what, lo=None, hi=None):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise Bad(f"{what}: bitte eine Zahl eingeben")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise Bad(f"{what}: bitte zwischen {lo:g} und {hi:g}")
    return x


def _need(vals, key, what):
    v = str(vals.get(key) or "").strip()
    if not v:
        raise Bad(f"Bitte {what} eintragen")
    return v


def _choice(v, opts, what):
    allowed = [o[0] for o in opts]
    if v not in allowed:
        raise Bad(f"{what}: ungültige Auswahl")
    return v


# --------------------------------------------------------------------------- #
# Netzwerkgeräte als Auswahlliste
# --------------------------------------------------------------------------- #
def host_opts(app, only_free=False, keep=()):
    try:
        hosts = app.describe_hosts()
    except Exception:  # noqa: BLE001
        hosts = []
    keep = {k.upper() for k in keep}
    out = []
    for h in sorted(hosts, key=lambda h: (not h["active"], h["name"].lower())):
        if only_free and h["mac"] not in keep and app.device_usage(h["mac"]):
            continue
        cat = h.get("category") or ""
        out.append((h["mac"], f"{'●' if h['active'] else '○'} {h['name'] or h['ip']}"
                              + (f" · {cat}" if cat and cat != "Unbekannt" else "") + f" · {h['ip']}"))
    for m in keep:  # gespeicherte Geräte, die gerade nicht im Netz sind
        if m and m not in {o[0] for o in out}:
            out.append((m, f"○ {m} (gerade nicht im Netz)"))
    return out


# --------------------------------------------------------------------------- #
# Automationen
# --------------------------------------------------------------------------- #
def auto_schema(app):
    import automations as A
    import pages as P
    ent = app.entities()
    trig = {}
    for typ, label in A.TRIGGER_TYPES.items():
        trig[typ] = {"label": label, "fields": [F(k, l, kind, o) for k, l, kind, o in P.trigger_spec(typ, ent)],
                     "defaults": P.TRIGGER_DEFAULTS.get(typ, {})}
    act = {}
    for typ, label in A.ACTION_TYPES.items():
        act[typ] = {"label": label, "fields": [F(k, l, kind, o) for k, l, kind, o in P.action_spec(typ, ent)],
                    "defaults": P.ACTION_DEFAULTS.get(typ, {})}
    return {"triggers": trig, "actions": act, "presence": [[k, n] for k, n in P.presence_choices(ent)],
            "devices": [[k, n] for k, n in P.device_choices(ent)],
            "days": A.DAYS}


def _clean_fields(fields, vals, where):
    import automations as A
    out = {}
    for f in fields:
        k, kind, v = f["key"], f["kind"], (vals or {}).get(f["key"])
        if kind == "choice":
            opts = f.get("opts") or []
            if not opts:
                raise Bad(f"{where}: „{f['label']}“ – dafür ist noch nichts eingerichtet")
            out[k] = v if v in [o[0] for o in opts] else opts[0][0]
        elif kind == "number":
            out[k] = _num(v if v not in (None, "") else 0, f"{where} – {f['label']}")
        elif kind == "time":
            if A._hm(v) is None:
                raise Bad(f"{where}: Uhrzeit bitte als HH:MM, z. B. 23:00")
            out[k] = str(v).strip()
        elif kind == "days":
            d = sorted({int(x) for x in (v or []) if str(x).isdigit() and 0 <= int(x) <= 6})
            out[k] = [] if len(d) == 7 else d
        else:
            out[k] = str(v or "").strip()
    return out


def clean_rule(app, rule):
    import automations as A
    sch = auto_schema(app)
    t = rule.get("trigger") or {}
    typ = t.get("type")
    if typ not in sch["triggers"]:
        raise Bad("Bitte einen Auslöser wählen")
    trig = _clean_fields(sch["triggers"][typ]["fields"], t, "Auslöser")
    trig["type"] = typ
    acts = []
    for a in rule.get("actions") or []:
        at = a.get("type")
        if at not in sch["actions"]:
            continue
        x = _clean_fields(sch["actions"][at]["fields"], a, sch["actions"][at]["label"])
        x["type"] = at
        acts.append(x)
    if not acts:
        raise Bad("Bitte mindestens eine Aktion hinzufügen")
    c = rule.get("condition") or {}
    cond = {}
    if c.get("from") or c.get("to"):
        if A._hm(c.get("from")) is None or A._hm(c.get("to")) is None:
            raise Bad("Zeitfenster bitte als HH:MM angeben")
        cond.update({"from": c["from"].strip(), "to": c["to"].strip()})
    days = sorted({int(x) for x in (c.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
    if days and len(days) < 7:
        cond["days"] = days
    pres = c.get("presence") or ""
    if isinstance(pres, dict):
        pres = f"{pres.get('who')}:{pres.get('state')}" if pres.get("who") else ""
    if pres and pres in [k for k, _ in sch["presence"]]:
        who, _, state = pres.rpartition(":")
        cond["presence"] = {"who": who, "state": state}
    dv = c.get("device") or ""
    if isinstance(dv, dict):
        dv = f"{dv.get('ain')}|{dv.get('state')}" if dv.get("ain") else ""
    if dv and dv in [k for k, _ in sch["devices"]]:
        ain, _, state = dv.rpartition("|")
        cond["device"] = {"ain": ain, "state": state}
    old = next((r for r in app.cfg.get("automations", []) if r["id"] == rule.get("id")), None)
    r = dict(old or A.new_automation())
    r.update(name=str(rule.get("name") or "Automation").strip()[:80], enabled=bool(rule.get("enabled", True)),
             trigger=trig, condition=cond, actions=acts)
    return r


def auto_list(app):
    import pages as P
    names = app.entity_names()
    out = []
    for r in app.cfg.get("automations", []):
        out.append({"id": r["id"], "name": r["name"], "enabled": r.get("enabled", True),
                    "manual": r.get("trigger", {}).get("type") == "manual",
                    "trigger": P.describe_trigger(r.get("trigger", {}), names),
                    "condition": P.describe_condition(r.get("condition") or {}, names),
                    "actions": [P.describe_action(a, names) for a in r.get("actions", [])], "rule": r})
    return out


def auto_post(app, op, ident, vals, user):
    lst = app.cfg.setdefault("automations", [])
    if op == "save":
        r = clean_rule(app, vals.get("rule") or {})
        if any(x["id"] == r["id"] for x in lst):  # Reihenfolge behalten
            app.cfg["automations"] = [r if x["id"] == r["id"] else x for x in lst]
        else:
            app.cfg["automations"] = lst + [r]
        app.automations_changed()
        app.log("Automation", r["name"], f"am Handy gespeichert ({user.get('name', '')})")
        return True, f"„{r['name']}“ gespeichert", {"id": r["id"]}
    r = next((x for x in lst if x["id"] == ident), None)
    if not r:
        raise Bad("Automation nicht gefunden")
    if op == "toggle":
        r["enabled"] = bool(vals.get("enabled"))
        app.automations_changed()
        return True, f"„{r['name']}“ {'an' if r['enabled'] else 'aus'}", {}
    if op == "delete":
        app.cfg["automations"] = [x for x in lst if x["id"] != ident]
        app.automations_changed()
        app.log("Automation", r["name"], f"am Handy gelöscht ({user.get('name', '')})")
        return True, f"„{r['name']}“ gelöscht", {}
    if op == "run":
        if not U.can(user, "smart", 2):
            raise Bad("Dafür brauchst du das Recht „Smart Home steuern“")
        app.engine.trigger(r, reason=f"Handy ({user.get('name', '')})")
        return True, f"„{r['name']}“ läuft", {}
    raise Bad("Unbekannte Aktion")


# --------------------------------------------------------------------------- #
# Admin-Bereiche
# --------------------------------------------------------------------------- #
def _list(items, fields=None, new=True, hint="", title=""):
    return {"kind": "list", "items": items, "fields": fields or [], "new": new, "hint": hint, "title": title}


def _form(fields, values, hint="", buttons=None, title=""):
    return {"kind": "form", "fields": fields, "values": values, "hint": hint, "buttons": buttons or [], "title": title}


def get(app, section, ident, user):
    if section == "menu":
        return {"sections": [{"id": k, "title": t, "sub": s} for k, t, s in ADMIN_SECTIONS], "flow": SETUP_FLOW}
    fn = globals().get("get_" + section)
    if not fn:
        raise Bad("Unbekannter Bereich")
    return fn(app, ident, user) if fn.__code__.co_argcount >= 3 else fn(app, ident)


def post(app, section, op, ident, vals, user):
    fn = globals().get("post_" + section)
    if not fn:
        raise Bad("Unbekannter Bereich")
    return fn(app, op, ident, vals or {}, user)


# --- Problem melden (für jeden angemeldeten Nutzer) ------------------------- #
def get_bugreport(app, ident, user=None):
    import bugreport as BR
    fields = [F("who", "Dein Name"), F("area", "Worum geht es?", "choice", BR.AREAS),
              F("text", "Was ist passiert?", "longtext",
                hint="Was hast du gemacht, was hast du erwartet, was kam stattdessen? Gern mit der genauen Meldung."),
              F("attach", "Fehlerprotokoll mitschicken (ohne Passwörter)", "bool")]
    vals = {"who": (user or {}).get("name") or app.cfg.get("bug_name", ""), "area": "allgemein", "text": "",
            "attach": True}
    if BR.PUBLIC:
        return _form(fields[1:3], vals, hint="Hauskern öffnet danach eine vorausgefüllte Fehlermeldung auf der "
                                             "Projektseite (GitHub) – die ist öffentlich sichtbar. IP-Adressen, Namen "
                                             "und Passwörter werden vorher entfernt; abgeschickt wird erst, wenn du dort "
                                             "auf „Create“ tippst. Mit „Vorschau“ siehst du genau, was drinsteht.",
                     buttons=[{"op": "send", "label": "🌐 Auf GitHub melden"}, {"op": "preview", "label": "👁 Vorschau"}],
                     title="🐞 Problem melden") | {"nosave": True}
    return _form(fields, vals, hint="Die Meldung geht direkt an Pascal. Passwörter und Zugangscodes werden vorher "
                                    "geschwärzt. Mit „Vorschau“ siehst du genau, was verschickt wird.",
                 buttons=[{"op": "send", "label": "📨 An Pascal senden"}, {"op": "preview", "label": "👁 Vorschau"}],
                 title="🐞 Problem melden") | {"nosave": True}


def post_bugreport(app, op, ident, vals, user):
    import bugreport as BR
    area = vals.get("area") if vals.get("area") in dict(BR.AREAS) else "allgemein"
    who = str(vals.get("who") or (user or {}).get("name") or "").strip()
    text = str(vals.get("text") or "")[:3000]
    try:
        if op == "preview":
            title, body = app.bug_report(who, area, text, preview=True)
            return True, "Vorschau unten", {"preview": title + "\n\n" + body}
        if op != "send":
            raise Bad("Unbekannte Aktion")
        app.bug_report(who, area, text, attach=bool(vals.get("attach", True)))
    except BR.ReportError as e:
        raise Bad(str(e)) from e
    if BR.PUBLIC:
        url = (getattr(app, "_last_bug", None) or {}).get("url")
        if not url:
            raise Bad("Für diese Version ist noch keine Projektseite eingetragen – die Meldung liegt als Datei auf dem "
                      "Hauskern-Rechner")
        return True, "🌐 Öffne GitHub – dort prüfen und auf „Create“ tippen. Danke! 🙂", {"open": url}
    return True, "📨 Wird an Pascal verschickt – danke! 🙂", {}


# --- Zwei-Faktor (für jeden angemeldeten Nutzer, nur das eigene Konto) ----- #
TFA_APPS = ("Apple „Passwörter“ (ab iOS 18 vorinstalliert), Google Authenticator, Microsoft Authenticator oder 2FAS – "
            "alle kostenlos.")


def _me(app, user):
    u = next((x for x in app.cfg.get("web_users") or [] if x.get("id") == (user or {}).get("id")), None)
    if not u:
        raise Bad("Bitte neu anmelden")
    return u


def get_twofa(app, ident, user=None):
    import qr
    import totp as T
    u = _me(app, user)
    if T.enabled(u):
        since = time.strftime("%d.%m.%Y", time.localtime(u.get("totp_since", 0) or time.time()))
        left = T.backups_left(u)
        return dict(_form([F("code", "Aktueller Code aus der App", "code",
                             hint="Zum Ausschalten oder für neue Notfallcodes – so kann niemand mit deinem entsperrten "
                                  "Handy einfach Zwei-Faktor abschalten.")],
                          {"code": ""}, title="🔐 Zwei-Faktor-Anmeldung",
                          hint=f"✅ An seit {since}. Bei jeder neuen Anmeldung fragt {app.app_name} nach PIN und Code.\n"
                               f"Noch {left} von {T.BACKUP_COUNT} Notfallcodes übrig"
                               + (" – bitte bald neue erstellen ⚠" if left <= 2 else "") + ".\n\n"
                               "Handy weg? Mit einem Notfallcode anmelden – oder ein Admin setzt es unter "
                               "„👥 Handy-Nutzer“ zurück.",
                          buttons=[{"op": "codes", "label": "🔄 Neue Notfallcodes"},
                                   {"op": "off", "label": "Ausschalten"}]), nosave=True)
    secret = T.start(u, app.secret_protect, app.secret_unprotect)
    app.save()
    link = T.uri(secret, u.get("name") or "Hauskern")
    return dict(_form([F("qr", "1. Mit der Authenticator-App scannen", "qr"),
                       F("open", "📱 Auf diesem Handy: direkt in die App übernehmen", "link", hint=link),
                       F("key", "… oder diesen Schlüssel von Hand eintragen", "copy"),
                       F("code", "2. Den 6-stelligen Code aus der App eintippen", "code")],
                      {"qr": qr.svg(qr.encode(link)), "key": T.pretty(secret), "code": ""},
                      title="🔐 Zwei-Faktor einrichten",
                      hint="Zusätzlich zur PIN fragt " + app.app_name + " bei jeder neuen Anmeldung nach einem Code, den "
                           "eine App auf deinem Handy alle 30 Sekunden neu erzeugt. Wer nur deine PIN kennt, kommt so "
                           "nicht rein.\n\nPassende Apps: " + TFA_APPS + "\n\nHandys, die schon angemeldet sind, "
                           "bleiben angemeldet.",
                      buttons=[{"op": "on", "label": "✅ Einschalten"}]), nosave=True)


def post_twofa(app, op, ident, vals, user):
    import totp as T
    u = _me(app, user)
    code = str(vals.get("code") or "").strip()
    if op == "on":
        if T.enabled(u):
            raise Bad("Ist schon an")
        if not code:
            raise Bad("Bitte den Code aus der App eintippen")
        codes = T.confirm(u, code, app.secret_unprotect)
        if not codes:
            raise Bad("Der Code passt nicht. Stimmt die Uhrzeit am Handy? Sonst den QR-Code neu scannen.")
        app.save()
        app.log("App", u.get("name", ""), "Zwei-Faktor-Anmeldung eingeschaltet")
        return True, "🔐 Zwei-Faktor ist an!", {"preview": _codes_text(codes)}
    if not T.enabled(u):
        raise Bad("Zwei-Faktor ist nicht an")
    if not code:
        raise Bad("Bitte den aktuellen Code aus der App eintippen")
    if T.verify(app.secret_unprotect(u["totp"]), code, int(u.get("totp_last", -1))) is None:
        raise Bad("Der Code passt nicht (jeder Code geht nur einmal – kurz auf den nächsten warten)")
    u["totp_last"] = T.now_counter()
    if op == "codes":
        codes = T.new_backups(u)
        app.save()
        app.log("App", u.get("name", ""), "Neue Notfallcodes erstellt")
        return True, "🔄 Neue Notfallcodes – die alten gelten nicht mehr", {"preview": _codes_text(codes)}
    if op == "off":
        T.disable(u)
        app.save()
        app.log("App", u.get("name", ""), "Zwei-Faktor-Anmeldung ausgeschaltet")
        return True, "Zwei-Faktor ist aus – ab jetzt reicht wieder die PIN", {"done": True}
    raise Bad("Unbekannte Aktion")


def _codes_text(codes):
    return ("🆘 DEINE NOTFALLCODES – jetzt aufschreiben oder ein Bildschirmfoto machen!\n"
            "Damit kommst du rein, wenn dein Handy mit der App weg ist. Jeder Code geht genau einmal, "
            "und hier siehst du sie nur dieses eine Mal.\n\n" + "\n".join(codes))


# --- Router ---------------------------------------------------------------- #
def get_router(app, ident):
    c = app.cfg
    rk = app.router_kind() or "fritz"
    fields = [F("kind", "Router", "choice", [("fritz", "FRITZ!Box"), ("generic", "Anderer Router"),
                                             ("none", "Ohne Router")]),
              F("host", "Adresse der FRITZ!Box", hint="meist fritz.box"),
              F("user", "FRITZ!Box-Benutzer", hint="System → FRITZ!Box-Benutzer, Rechte „FRITZ!Box Einstellungen“ "
                                                    "und „Smart Home“"),
              F("password", "Passwort", "password", hint="leer lassen = bleibt")]
    status = ""
    if app.client:
        status = f"✅ verbunden: {app.main_info.get('model', '')}"
    elif rk != "none":
        status = "noch nicht verbunden"
    return _form(fields, {"kind": rk, "host": c.get("host") or "fritz.box", "user": c.get("user", ""), "password": ""},
                 hint=status + "\nAnderer Router (Speedport, Vodafone Station, TP-Link …): keine Zugangsdaten nötig. "
                               "WLAN schalten, Gast-WLAN und FRITZ!DECT gibt es nur mit FRITZ!Box.",
                 buttons=[{"op": "detect", "label": "🔎 Router erkennen"}], title="Router")


def post_router(app, op, ident, vals, user):
    if op == "detect":
        import router
        res = router.detect()
        name = res.get("name") or "Router"
        txt = {"fritz": f"Erkannt: {name} – „FRITZ!Box“ wählen und anmelden",
               "generic": f"Erkannt: {name} – „Anderer Router“ passt"}.get(res["kind"], "Kein Router erkannt")
        return True, txt, {"set": {"kind": res["kind"] if res["kind"] != "none" else "fritz",
                                   **({"host": res["host"]} if res["kind"] == "fritz" else {})}}
    if op != "save":
        raise Bad("Unbekannte Aktion")
    kind = _choice(vals.get("kind"), [("fritz", ""), ("generic", ""), ("none", "")], "Router")
    if kind == "generic":
        app.apply_generic_router(app.cfg.get("router_name", ""))
        return True, "Anderer Router eingerichtet – Geräte erscheinen in den nächsten Minuten", {}
    if kind == "none":
        app.set_no_router()
        return True, "Ohne Router gespeichert", {}
    host = _need(vals, "host", "die Adresse")
    usr = _need(vals, "user", "den FRITZ!Box-Benutzer")
    pw = vals.get("password") or (app.password if usr == app.cfg.get("user") else "")
    if not pw:
        raise Bad("Bitte das Passwort eintragen")
    import fritz_api
    try:
        model = fritz_api.test_login(host, usr, pw)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        raise Bad("Anmeldung fehlgeschlagen: " + msg[:120] + (" – Benutzer/Passwort prüfen" if "401" in msg else ""))
    app.cfg["router"] = "fritz"
    app.apply_fritz_login(host, usr, pw)
    return True, f"✅ Verbunden mit {model}", {}


# --- Personen & Gegenstände ------------------------------------------------- #
def get_persons(app, ident):
    ps = app.cfg.get("persons", [])
    if ident is None:
        snap = app.presence_snapshot() if hasattr(app, "presence_snapshot") else {}
        items = [{"id": p["id"], "title": ("🔑 " if p.get("kind") == "item" else "🙂 ") + p["name"],
                  "sub": f"{len(p.get('macs', []))} Gerät(e) · " + {"home": "zu Hause" if p.get("kind") != "item"
                                                                       else "ist da", "away": "unterwegs"
                                                                       if p.get("kind") != "item" else "ist weg"}
                  .get(snap.get(p["id"]), "–")} for p in ps]
        return _list(items, hint="Handys von Personen oder Gegenstände im WLAN (z. B. Auto, Uhr). "
                                 "iPhone: „Private WLAN-Adresse“ für dein WLAN auf „Fest“ stellen.",
                     title="Personen & Gegenstände")
    p = next((x for x in ps if x["id"] == ident), None) or {}
    fields = [F("name", "Name"),
              F("kind", "Art", "choice", [("person", "Person (Handy)"), ("item", "Gegenstand (Auto, Uhr …)")]),
              F("macs", "Geräte", "multi", host_opts(app, keep=p.get("macs", [])),
                hint="Mehrere möglich, z. B. Handy + Uhr – da ist, sobald eins davon im WLAN ist"),
              F("delay", "Als „weg“ nach (Min.)", "number")]
    vals = {"name": p.get("name", ""), "kind": p.get("kind", "person"), "macs": p.get("macs", []),
            "delay": p.get("delay", 10)}
    if not p:  # Vorschlag: erstes freies Handy
        for mac, _ in host_opts(app, only_free=True):
            d, _ = app.describe_host(mac)
            if d and d.get("role") == "person":
                vals["macs"] = [mac]
                break
    return _form(fields, vals, title=p.get("name") or "Neu")


def post_persons(app, op, ident, vals, user):
    if op == "delete":
        app.remove_person(ident)
        return True, "Entfernt", {}
    name = _need(vals, "name", "einen Namen")
    macs = [str(m).upper() for m in vals.get("macs") or [] if m]
    if not macs:
        raise Bad("Bitte mindestens ein Gerät auswählen")
    kind = "item" if vals.get("kind") == "item" else "person"
    app.save_person({"id": ident or f"p{int(time.time() * 1000) % 10**9}", "name": name, "macs": macs,
                     "delay": int(_num(vals.get("delay", 10), "Wartezeit", 1, 240)), "kind": kind})
    return True, f"„{name}“ gespeichert", {}


# --- Geräte (eigene Kacheln) ------------------------------------------------ #
def get_devices(app, ident):
    devs = app.cfg.get("devices", [])
    if ident is None:
        st = {d["id"]: d for d in app.device_states()}
        items = [{"id": d["id"], "title": d["name"], "sub": (d.get("category") or "") + " · " +
                  ("online" if st.get(d["id"], {}).get("online") else "offline")} for d in devs]
        return _list(items, hint="Geräte aus deinem Netz mit eigener Kachel (online/offline, verbundene Geräte).",
                     title="Geräte")
    from pages import DEVICE_CATEGORIES
    d = next((x for x in devs if x["id"] == ident), None) or {}
    fields = [F("mac", "Gerät im Netz", "choice", host_opts(app, only_free=not d, keep=[d.get("mac", "")])),
              F("name", "Name", hint="leer = so wie erkannt"),
              F("category", "Art", "choice", [("", "automatisch erkennen")] + [(c, c) for c in DEVICE_CATEGORIES]),
              F("children", "Verbundene Geräte im Netz", "multi", host_opts(app, keep=d.get("children", [])),
                hint="z. B. Sky-Q-Mini zur Sky-Box – beim Neuanlegen schlägt Hauskern Passendes vor"),
              F("radio", "Per Funk verbunden (nicht im WLAN)", hint="Namen mit Komma, z. B. Flur, Küche")]
    return _form(fields, {"mac": d.get("mac", ""), "name": d.get("name", ""), "category": d.get("category", ""),
                          "children": d.get("children", []), "radio": ", ".join(d.get("radio", []))},
                 title=d.get("name") or "Neues Gerät")


def post_devices(app, op, ident, vals, user):
    if op == "delete":
        app.remove_device(ident)
        return True, "Entfernt", {}
    import discover
    mac = _need(vals, "mac", "ein Gerät").upper()
    target, allh = app.describe_host(mac)
    target = target or {"mac": mac, "name": mac, "category": ""}
    children = [str(m).upper() for m in vals.get("children") or [] if m and str(m).upper() != mac]
    if not ident and not children:  # neu: passende Geräte automatisch dazu
        children = [o["mac"] for o, _ in discover.related(target, [h for h in allh if h["mac"] != mac])]
    names = {h["mac"]: h["name"] for h in allh}
    cat = vals.get("category") or target.get("category") or "Sonstiges"
    name = str(vals.get("name") or "").strip() or (target.get("name") if target.get("name") != mac else cat)
    app.save_device({"id": ident or f"d{int(time.time() * 1000) % 10**9}", "name": name, "category": cat, "mac": mac,
                     "children": children, "child_names": {m: names.get(m, m) for m in children},
                     "radio": [x.strip() for x in re.split(r"[,;\n]", str(vals.get("radio") or "")) if x.strip()]})
    return True, f"„{name}“ gespeichert" + (f" mit {len(children)} verbundenen Geräten" if children else ""), {}


# --- 3D-Drucker ------------------------------------------------------------- #
def get_printers(app, ident):
    ps = app.cfg.get("printers", [])
    if ident is None:
        items = [{"id": p["serial"], "title": p["name"], "sub": f"{p['ip']} · {p['serial']}"} for p in ps]
        return _list(items, hint="Bambu Lab im LAN-Modus. Den Zugangscode und die Seriennummer findest du am "
                                 "Drucker-Display unter Einstellungen → WLAN/LAN.", title="3D-Drucker")
    p = next((x for x in ps if x["serial"] == ident), None) or {}
    plugs = [(d["ain"], d["name"]) for d in app.data.get("smart", []) if d.get("power_w") is not None
             and not d.get("is_group")]
    fields = [F("name", "Name"), F("ip", "IP-Adresse"), F("serial", "Seriennummer"),
              F("code", "LAN-Zugangscode", "password", hint="leer lassen = bleibt" if p else ""),
              F("plug_ain", "Messsteckdose (optional)", "choice", [("", "– keine –")] + plugs)]
    vals = {"name": p.get("name", "3D-Drucker"), "ip": p.get("ip", ""), "serial": p.get("serial", ""), "code": "",
            "plug_ain": p.get("plug_ain", "")}
    if not p:  # Vorschlag aus der Gerätesuche
        d = next((x for x in app.disc_results if x.get("category") == "3D-Drucker"
                  and x["ip"] not in {q["ip"] for q in ps}), None)
        if d:
            vals.update(name=d.get("model") or d["name"] or "3D-Drucker", ip=d["ip"])
    return _form(fields, vals, title=p.get("name") or "Neuer Drucker")


def post_printers(app, op, ident, vals, user):
    if op == "delete":
        app.remove_printer(ident)
        return True, "Entfernt", {}
    name = str(vals.get("name") or "3D-Drucker").strip()
    ip, serial = _need(vals, "ip", "die IP-Adresse"), _need(vals, "serial", "die Seriennummer")
    code = str(vals.get("code") or "").strip()
    old = next((p for p in app.cfg.get("printers", []) if p["serial"].strip() == (ident or serial)), None)
    if not code:
        if not old:
            raise Bad("Bitte den LAN-Zugangscode eintragen")
        code = app.printer_code(old)
    app.save_printer({"name": name, "ip": ip, "serial": serial, "code": code, "plug_ain": vals.get("plug_ain", "")},
                     old_serial=ident)
    return True, f"„{name}“ gespeichert – verbindet …", {}


# --- Kameras ---------------------------------------------------------------- #
def get_cameras(app, ident):
    from pages import BRANDS
    cs = app.cfg.get("cameras", [])
    if ident is None:
        items = [{"id": c["id"], "title": c["name"], "sub": f"{c.get('host') or 'RTSP'} · "
                                                            + next((n for k, n, _ in BRANDS if k == c.get('brand')), "")}
                 for c in cs]
        return _list(items, hint="Bei Tapo: in der Tapo-App → Kamera → Einstellungen → Erweiterte Einstellungen → "
                                 "Kamerakonto anlegen und hier eintragen.", title="Kameras")
    c = next((x for x in cs if x["id"] == ident), None) or {}
    fields = [F("name", "Name"), F("brand", "Hersteller", "choice", [(k, n) for k, n, _ in BRANDS]),
              F("host", "IP-Adresse"), F("user", "Kamerakonto – Benutzer"),
              F("pw", "Kamerakonto – Passwort", "password", hint="leer lassen = bleibt" if c else ""),
              F("rtsp", "RTSP-Adresse (nur bei „Nur RTSP“)")]
    vals = {"name": c.get("name", "Kamera"), "brand": c.get("brand") or "tapo", "host": c.get("host", ""),
            "user": c.get("user", ""), "pw": "", "rtsp": c.get("rtsp", "")}
    if not c:
        d = next((x for x in app.disc_results if (x.get("category") or "").startswith("Kamera")
                  and x["ip"] not in {q.get("host") for q in cs}), None)
        if d:
            vals.update(name=d["name"] or "Kamera", host=d["ip"])
    return _form(fields, vals, title=c.get("name") or "Neue Kamera")


def post_cameras(app, op, ident, vals, user):
    from pages import BRANDS
    if op == "delete":
        app.cfg["cameras"] = [x for x in app.cfg.get("cameras", []) if x["id"] != ident]
        app.save()
        app.start_cameras()
        return True, "Entfernt", {}
    brand = _choice(vals.get("brand"), [(k, n) for k, n, _ in BRANDS], "Hersteller")
    host, rtsp = str(vals.get("host") or "").strip(), str(vals.get("rtsp") or "").strip()
    if not host and not rtsp:
        raise Bad("Bitte IP-Adresse oder RTSP-Adresse eintragen")
    old = next((x for x in app.cfg.get("cameras", []) if x["id"] == ident), None) or {}
    pw = str(vals.get("pw") or "") or app.camera_password(old)
    port = int(old.get("port") or next(p for k, _, p in BRANDS if k == brand) or 80)
    if old and old.get("brand") != brand:
        port = next(p for k, _, p in BRANDS if k == brand) or 80
    app.save_camera({"id": ident or f"c{int(time.time() * 1000) % 10**9}", "name": str(vals.get("name") or "Kamera"),
                     "host": host, "port": port, "user": str(vals.get("user") or "").strip(), "pw": pw, "rtsp": rtsp,
                     "brand": brand, "motion": old.get("motion", {})})
    return True, "Kamera gespeichert – verbindet …", {}


# --- Handy-Nutzer ----------------------------------------------------------- #
def get_users(app, ident):
    us = app.cfg.get("web_users") or []
    if ident is None:
        items = [{"id": u["id"], "title": ("👑 " if u.get("admin") else "") + u["name"] + (" 🔐" if u.get("totp") else ""),
                  "sub": U.describe(u) + f" · {U.count_sessions(app.cfg, u)} Handy(s)"
                         + (" · Zwei-Faktor an" if u.get("totp") else "")} for u in us]
        return _list(items, hint="Jeder hat eine eigene PIN. Admins dürfen alles – auch diese Einstellungen.",
                     title="Handy-Nutzer")
    u = next((x for x in us if x["id"] == ident), None) or {}
    fields = [F("name", "Name"), F("pin", "PIN (4–12 Ziffern)", "password", hint="leer lassen = bleibt" if u else ""),
              F("admin", "👑 Admin (darf alles)", "bool"),
              F("preset", "Vorlage", "choice", [("", "– selbst einstellen –")] + [(n, n) for n, _ in U.PRESETS])]
    vals = {"name": u.get("name", ""), "pin": "", "admin": bool(u.get("admin")), "preset": ""}
    rights = u.get("rights") or dict(U.PRESETS[1][1])
    for k, label, mx, hint in U.AREAS:
        fields.append(F("r_" + k, label, "choice", [(str(x), U.LEVEL_TEXT[x]) for x in range(mx + 1)], hint))
        vals["r_" + k] = str(min(int(rights.get(k, 0)), mx))
    btns = [{"op": "kick", "label": "Überall abmelden"}] if u else []
    if u.get("totp"):
        btns.append({"op": "reset2fa", "label": "🔐 Zwei-Faktor zurücksetzen"})
    return _form(fields, vals, buttons=btns, title=u.get("name") or "Neuer Nutzer")


def post_users(app, op, ident, vals, user):
    import secrets
    from webui import check_pin, hash_pin
    us = [dict(x) for x in app.cfg.get("web_users") or []]
    cur = next((x for x in us if x["id"] == ident), None)
    if op == "kick":
        if not cur:
            raise Bad("Nutzer nicht gefunden")
        app.save_users([dict(x, rev=secrets.token_hex(4)) if x["id"] == ident else x for x in us])
        return True, f"„{cur['name']}“ auf allen Handys abgemeldet", {}
    if op == "reset2fa":
        import totp as T
        if not cur:
            raise Bad("Nutzer nicht gefunden")
        T.disable(cur)
        app.save_users([cur if x["id"] == ident else x for x in us])
        app.log("App", (user or {}).get("name", ""), f"Zwei-Faktor von „{cur['name']}“ zurückgesetzt")
        return True, f"Zwei-Faktor von „{cur['name']}“ ist aus – jetzt reicht die PIN, danach neu einrichten", {}
    if op == "delete":
        rest = [x for x in us if x["id"] != ident]
        if not any(x.get("admin") for x in rest):
            raise Bad("Der letzte Admin kann nicht entfernt werden")
        app.save_users(rest)
        return True, "Entfernt", {}
    name = _need(vals, "name", "einen Namen")
    pin = str(vals.get("pin") or "").strip()
    if not cur and not pin:
        raise Bad("Bitte eine PIN festlegen")
    if pin and not (pin.isdigit() and 4 <= len(pin) <= 12):
        raise Bad("Die PIN muss aus 4 bis 12 Ziffern bestehen")
    if pin and any(check_pin(pin, x.get("pin", "")) for x in us if x["id"] != ident):
        raise Bad("Diese PIN benutzt schon jemand anderes")
    preset = dict(U.PRESETS).get(vals.get("preset") or "")
    rights = dict(preset) if preset else {k: int(vals.get("r_" + k) or 0) for k in U.AREA_KEYS}
    admin = bool(vals.get("admin"))
    if cur:
        r = dict(cur, name=name, admin=admin, rights=rights)
        if pin:
            r["pin"], r["rev"] = hash_pin(pin), secrets.token_hex(4)
        lst = [r if x["id"] == ident else x for x in us]
    else:
        r = U.new_user(name, hash_pin(pin), rights, admin)
        lst = us + [r]
    if not any(x.get("admin") for x in lst):
        raise Bad("Mindestens ein Nutzer muss Admin bleiben")
    app.save_users(lst)
    return True, f"„{name}“ gespeichert", {}


# --- Push ------------------------------------------------------------------- #
def get_push(app, ident):
    import push as P
    c = app.push_cfg()
    ks = app.notify_kinds()
    fields = [F("enabled", "Push-Nachrichten senden", "bool"),
              F("service", "Dienst", "choice", list(P.SERVICES.items())),
              F("ntfy_topic", "ntfy: Thema", hint="In der ntfy-App dasselbe Thema abonnieren – am besten "
                                                  "etwas Langes, Zufälliges"),
              F("ntfy_server", "ntfy: Server"),
              F("pushover_token", "Pushover: API-Token", "password"), F("pushover_user", "Pushover: User-Key", "password"),
              F("telegram_token", "Telegram: Bot-Token", "password"), F("telegram_chat_id", "Telegram: Chat-ID"),
              F("kinds", "Worüber benachrichtigen?", "multi", list(ks.items()) + [("automation", "Automationen")]),
              F("quiet", "Ruhezeit (nur Wichtiges)", "bool"), F("quiet_from", "Ruhezeit von", "time"),
              F("quiet_to", "Ruhezeit bis", "time"),
              F("weekly", "📅 Wochenrückblick sonntags um 18 Uhr", "bool",
                hint="Strom, Drucke, Filament, Batterien, Heizung, Anrufe – kurz zusammengefasst. Standardmäßig aus.")]
    vals = {"enabled": bool(c.get("enabled")), "service": c.get("service", "ntfy"),
            "ntfy_topic": c.get("ntfy", {}).get("topic", ""), "ntfy_server": c.get("ntfy", {}).get("server", "https://ntfy.sh"),
            "pushover_token": "", "pushover_user": c.get("pushover", {}).get("user", ""),
            "telegram_token": "", "telegram_chat_id": c.get("telegram", {}).get("chat_id", ""),
            "kinds": [k for k in list(ks) + ["automation"] if c.get("kinds", {}).get(k, True)],
            "quiet": bool((c.get("quiet") or {}).get("enabled")), "quiet_from": (c.get("quiet") or {}).get("from", "22:00"),
            "quiet_to": (c.get("quiet") or {}).get("to", "07:00"),
            "weekly": bool((app.cfg.get("weekly") or {}).get("enabled"))}
    return _form(fields, vals, hint="Tokens bleiben gespeichert, wenn die Felder leer bleiben.",
                 buttons=[{"op": "test", "label": "Test senden"}, {"op": "random", "label": "Zufälliges ntfy-Thema"},
                          {"op": "tgchat", "label": "Telegram: Chat-ID ermitteln"}],
                 title="Push-Nachrichten")


def _push_from(app, vals):
    import push as P
    c = app.push_cfg()
    svc = _choice(vals.get("service"), list(P.SERVICES.items()), "Dienst")
    c.update(enabled=bool(vals.get("enabled")), service=svc)
    c.setdefault("ntfy", {}).update(topic=str(vals.get("ntfy_topic") or "").strip(),
                                    server=str(vals.get("ntfy_server") or "https://ntfy.sh").strip())
    for svc_, key in (("pushover", "token"), ("pushover", "user"), ("telegram", "token"), ("telegram", "chat_id")):
        v = str(vals.get(f"{svc_}_{key}") or "").strip()
        if v:
            c.setdefault(svc_, {})[key] = v
    sel = set(vals.get("kinds") or [])
    c["kinds"] = {k: k in sel for k in list(app.notify_kinds()) + ["automation"]}
    for k in ("quiet_from", "quiet_to"):
        import automations as A
        if vals.get("quiet") and A._hm(vals.get(k)) is None:
            raise Bad("Ruhezeit bitte als HH:MM")
    c["quiet"] = {"enabled": bool(vals.get("quiet")), "from": vals.get("quiet_from") or "22:00",
                  "to": vals.get("quiet_to") or "07:00"}
    if c["enabled"] and svc == "ntfy" and not c["ntfy"]["topic"]:
        raise Bad("Bitte ein ntfy-Thema eintragen (oder „Zufälliges ntfy-Thema“)")
    return c


def post_push(app, op, ident, vals, user):
    if op == "random":
        import secrets
        return True, "Neues Thema – jetzt in der ntfy-App abonnieren und speichern", {
            "set": {"ntfy_topic": "hauskern-" + secrets.token_hex(6), "service": "ntfy"}}
    c = _push_from(app, vals)
    if op == "tgchat":
        import push as P
        try:
            cid, name = P.telegram_find_chat(c.get("telegram", {}).get("token", ""))
        except Exception as e:  # noqa: BLE001
            raise Bad(str(e)) from e
        return True, f"✓ Chat gefunden: {name} – jetzt speichern", {"set": {"telegram_chat_id": cid,
                                                                            "service": "telegram"}}
    if op == "test":
        try:
            app.pusher.send_now(c, "Hauskern 🏠", "Test erfolgreich – Push-Nachrichten funktionieren! 🎉",
                                click=app.web_click_url())
        except Exception as e:  # noqa: BLE001
            raise Bad(f"Senden fehlgeschlagen: {e}")
        return True, "✓ Gesendet – schau aufs Handy 📱", {}
    app.save_push_cfg(c)
    if "weekly" in vals:
        app.set_weekly(bool(vals.get("weekly")))
    return True, "Gespeichert", {}


# --- Strom ------------------------------------------------------------------ #
def get_power(app, ident):
    import power as P
    ps = dict(P.DEFAULTS, **(app.cfg.get("power") or {}))
    fields = [F("price", "Arbeitspreis (ct/kWh)", "number", hint="steht auf der Stromrechnung"),
              F("budget", "Monatsbudget (€)", "number", hint="0 = kein Budget"),
              F("report", "Monatsbericht am 1.", "bool"), F("outliers", "Warnen bei ungewöhnlich hohem Verbrauch", "bool"),
              F("budget_warn", "Warnen, wenn das Budget überschritten wird", "bool")]
    return _form(fields, {"price": app.cfg.get("price_ct", 35.0), "budget": ps["budget_eur"], "report": ps["report"],
                          "outliers": ps["outliers"], "budget_warn": ps["budget_warn"]},
                 hint="Gemessen wird alles an FRITZ!DECT-Messsteckdosen.", title="Strompreis & Warnungen")


def post_power(app, op, ident, vals, user):
    price = _num(vals.get("price"), "Arbeitspreis", 1, 300)
    ps = dict(app.cfg.get("power") or {})
    ps.update(budget_eur=max(0.0, _num(vals.get("budget") or 0, "Budget", 0, 100000)), report=bool(vals.get("report")),
              outliers=bool(vals.get("outliers")), budget_warn=bool(vals.get("budget_warn")))
    app.cfg["price_ct"], app.cfg["power"] = price, ps
    app.save()
    app.analyze_power()
    return True, "Gespeichert", {}


# --- Wetter ----------------------------------------------------------------- #
ALERT_PUSH = [("1", "🟡 jede Warnung"), ("2", "🟠 markantes Wetter (empfohlen)"), ("3", "🔴 Unwetter"),
              ("4", "🟣 nur extremes Unwetter")]


def get_weather(app, ident):
    c = app.weather_cfg()
    fields = [F("enabled", "Wetter anzeigen", "bool"),
              F("mode", "Ort", "choice", [("auto", "automatisch (über die Internetverbindung)"), ("manual", "fester Ort")]),
              F("place", "Fester Ort", hint="z. B. Solingen – wird beim Speichern gesucht"),
              F("alerts", "⛈ Unwetterwarnungen vom Deutschen Wetterdienst", "bool"),
              F("alert_push", "Push-Nachricht ab", "choice", ALERT_PUSH,
                hint="In der App siehst du immer alle Warnungen – Push nur ab dieser Stufe")]
    return _form(fields, {"enabled": c.get("enabled", True), "mode": c.get("mode", "auto"), "place": c.get("name", ""),
                          "alerts": c.get("alerts", True), "alert_push": str(c.get("alert_push", 2))},
                 hint=f"Aktuell: {c.get('name') or 'automatisch'}", title="Wetter")


def post_weather(app, op, ident, vals, user):
    c = dict(app.weather_cfg())
    c["enabled"] = bool(vals.get("enabled"))
    c["mode"] = "manual" if vals.get("mode") == "manual" else "auto"
    c["alerts"] = bool(vals.get("alerts", True))
    c["alert_push"] = int(_choice(str(vals.get("alert_push") or "2"), ALERT_PUSH, "Push-Stufe"))
    if c["enabled"] and c["mode"] == "manual":
        import weather
        place = _need(vals, "place", "einen Ort")
        try:
            res = weather.search(place, 1)
        except Exception as e:  # noqa: BLE001
            raise Bad(f"Ortssuche nicht erreichbar: {e}")
        if not res:
            raise Bad(f"„{place}“ nicht gefunden")
        r = res[0]
        c.update(name=f"{r['name']}" + (f", {r['region'].split(',')[0]}" if r.get("region") else ""), lat=r["lat"],
                 lon=r["lon"])
    app.save_weather_cfg(c)
    return True, "Gespeichert" + (f" – {c['name']}" if c["mode"] == "manual" else ""), {}



# --- Alexa & Google Home ---------------------------------------------------- #
def get_voice(app, ident):
    import voice as V
    v = app.cfg.get("voice") or {}
    sel = V.selection(app)
    fields = [F("enabled", "Für Alexa & Google Home freigeben", "bool")]
    vals = {"enabled": bool(v.get("enabled"))}
    for c in V.candidates(app):
        s = sel.get(c["id"], {})
        fields.append(F("on:" + c["id"], f"{V.TYPE_TEXT[c['type']]}: {c['label']}", "bool"))
        fields.append(F("name:" + c["id"], "heißt dort", hint=""))
        vals["on:" + c["id"]] = bool(s.get("on"))
        vals["name:" + c["id"]] = s.get("name") or c["default"]
    seen = getattr(app, "voice_seen", 0)
    st = (f"✅ Matter-Bridge verbunden (zuletzt {time.strftime('%H:%M', time.localtime(seen))} Uhr)"
          if seen and time.time() - seen < 300 else "Matter-Bridge noch nicht verbunden – kommt mit dem Heimserver.")
    return _form(fields, vals, hint=st + "\nNur Freigegebenes ist für Alexa/Google sichtbar. Kurze, eindeutige Namen "
                                         "funktionieren am besten.", title="Alexa & Google Home")


def post_voice(app, op, ident, vals, user):
    import voice as V
    items = {}
    for k, v in vals.items():
        if k.startswith("on:"):
            cid = k[3:]
            items[cid] = {"on": bool(v), "name": vals.get("name:" + cid, "")}
    try:
        n = V.save_selection(app, vals.get("enabled"), items)
    except ValueError as e:
        raise Bad(str(e))
    return True, f"Gespeichert – {n} Geräte freigegeben" if vals.get("enabled") else "Gespeichert (aus)", {}



# --- Sicherung -------------------------------------------------------------- #
def get_backup(app, ident):
    import backup as B
    c = app.backup_cfg()
    bs = app.list_backups()
    if ident is None:
        last = c.get("last")
        st = (("✅ " if c.get("last_ok", True) else "⚠ ") + f"Letzte Sicherung: {B.fmt_ts(last)}"
              + ("" if c.get("last_ok", True) else f" – fehlgeschlagen: {c.get('last_err', '')}")) if last else \
            "Noch keine Sicherung."
        where = f"Ort: {app.backups_dir}" + (f" + Kopie nach {c['extra_dir']}" if c.get("extra_dir") else "")
        n = app.nas_cfg()
        if n.get("enabled"):
            where += (f"\nNetzwerkspeicher ({n.get('host')}): " + (("✅ zuletzt " + B.fmt_ts(c.get("nas_last")))
                      if c.get("nas_ok") else ("⚠ " + c.get("nas_err", "")) if c.get("nas_last") else "noch nichts hochgeladen"))
        h = app.health or {}
        if (h.get("storage") or {}).get("kind") == "SD-Karte":
            where += "\n⚠ Die Daten liegen auf der SD-Karte – am besten zusätzlich eine Kopie auf den PC oder einen USB-Stick."
        items = [{"id": b["name"], "title": B.fmt_ts(b["ts"]) + (" · manuell" if b["manual"] else ""),
                  "sub": f"{B.fmt_size(b['size'])} · Version {b['meta'].get('version', '?')}"} for b in bs]
        return dict(_list(items, hint=st + "\n" + where + "\nGesichert: Einstellungen, Verlauf, Druck- und Stromdaten.",
                          title="Sicherung"), new_label="⚙ Zeitpunkt & Ziel / Jetzt sichern",
                    empty="Noch keine Sicherung – die erste kommt heute Nacht, oder gleich über „Jetzt sichern“.")
    if ident == "":
        fields = [F("enabled", "Jede Nacht automatisch sichern", "bool"), F("time", "Uhrzeit", "time"),
                  F("extra_dir", "Zusätzlich kopieren nach (optional)",
                    hint="Ordner auf einem anderen Laufwerk, z. B. /media/usb oder ein NAS-Ordner. Leer = nur hier."),
                  F("pi_pull", "PC holt sich jeden Tag eine Kopie vom Heimserver", "bool"),
                  F("nas_enabled", "Zusätzlich auf Netzwerkspeicher (z. B. SSD an der FRITZ!Box)", "bool"),
                  F("nas_host", "Netzwerkspeicher – Adresse", hint="bei der FRITZ!Box: fritz.box"),
                  F("nas_user", "Benutzer", hint="FRITZ!Box-Benutzer mit dem Recht „Zugang zu NAS-Inhalten“ (lesen und "
                                                 "schreiben)"),
                  F("nas_pw", "Passwort", "password", hint="leer lassen = bleibt"),
                  F("nas_dir", "Ordner", hint="z. B. „Samsung-SSD/Hauskern-Sicherung“ – „Testen“ zeigt die Laufwerke"),
                  F("nas_keep", "Dort behalten (Anzahl)", "number")]
        n = app.nas_cfg()
        return _form(fields, {"enabled": c.get("enabled", True), "time": c.get("time", "03:30"),
                              "extra_dir": c.get("extra_dir", ""), "pi_pull": c.get("pi_pull", True),
                              "nas_enabled": n.get("enabled"), "nas_host": n.get("host") or "fritz.box",
                              "nas_user": n.get("user", ""), "nas_pw": "", "nas_dir": n.get("dir", ""),
                              "nas_keep": n.get("keep", 14)},
                     hint="Behalten werden die letzten 7 Tage, 4 Wochen und 5 manuelle Sicherungen.\nFRITZ!Box als "
                          "Netzwerkspeicher: Heimnetz → USB/Speicher → Speicher (NAS) aktivieren und den Zugriff über FTP "
                          "einschalten.",
                     buttons=[{"op": "now", "label": "💾 Jetzt sichern"}, {"op": "nas_test", "label": "🔌 Netzwerkspeicher testen"}],
                     title="Sicherung einstellen")
    b = next((x for x in bs if x["name"] == ident), None)
    if not b:
        raise Bad("Sicherung nicht gefunden")
    m = b["meta"]
    fields = [F("info", "Inhalt", "info", hint=", ".join(m.get("files", [])) or "?"),
              F("dl", "⬇ Herunterladen", "link", hint="/api/backup/download?name=" + b["name"])]
    warn = ""
    if m.get("os") and m.get("os") != __import__("os").name:
        warn = "\n⚠ Diese Sicherung stammt von einem anderen Rechner – gespeicherte Passwörter musst du danach neu eingeben."
    return _form(fields, {}, hint=f"Sicherung vom {B.fmt_ts(b['ts'])} · {B.fmt_size(b['size'])} · Version "
                                 f"{m.get('version', '?')} · {m.get('host', '')}{warn}\nBeim Wiederherstellen werden die jetzigen "
                                 "Daten vorher beiseitegelegt – nichts geht verloren. Hauskern startet dabei neu.",
                 buttons=[{"op": "restore", "label": "↩ Wiederherstellen"}], title=B.fmt_ts(b["ts"]))


def post_backup(app, op, ident, vals, user):
    import backup as B
    if op == "now":
        if not app.run_backup("manual"):
            raise Bad("Es läuft gerade schon eine Sicherung")
        return True, "Sicherung läuft – in ein paar Sekunden in der Liste", {}
    if op == "restore":
        b = next((x for x in app.list_backups() if x["name"] == ident), None)
        if not b:
            raise Bad("Sicherung nicht gefunden")
        try:
            app.restore_backup(b["path"])
        except B.BackupError as e:
            raise Bad(str(e))
        return True, "Wird eingespielt – Hauskern startet neu, gleich ist alles wieder da", {}
    if op == "delete":
        b = next((x for x in app.list_backups() if x["name"] == ident), None)
        if b:
            __import__("os").remove(b["path"])
        return True, "Gelöscht", {}
    n = app.nas_cfg()
    n.update(enabled=bool(vals.get("nas_enabled")), host=str(vals.get("nas_host") or "fritz.box").strip(),
             user=str(vals.get("nas_user") or "").strip(), dir=str(vals.get("nas_dir") or "").strip(),
             keep=int(_num(vals.get("nas_keep") or 14, "Anzahl", 1, 100)))
    if vals.get("nas_pw"):
        n["pw"] = str(vals["nas_pw"])
    if op == "nas_test":
        if not n["user"] or not n["pw"]:
            raise Bad("Bitte Benutzer und Passwort für den Netzwerkspeicher eintragen")
        try:
            tls, cnt, roots = B.nas_test(n["host"], n["user"], n["pw"], n["dir"])
        except B.BackupError as e:
            raise Bad(str(e))
        app.save_nas(n)
        return True, (f"✅ Verbunden ({'verschlüsselt' if tls else 'unverschlüsselt im Heimnetz'}) · {cnt} Sicherungen "
                      f"im Ordner" + (f" · Laufwerke: {', '.join(roots[:4])}" if roots else "")), {}
    import automations as A
    if A._hm(vals.get("time")) is None:
        raise Bad("Uhrzeit bitte als HH:MM")
    if n["enabled"] and (not n["user"] or not n["pw"]):
        raise Bad("Für den Netzwerkspeicher bitte Benutzer und Passwort eintragen")
    extra = str(vals.get("extra_dir") or "").strip()
    if extra:
        import os
        if not os.path.isdir(extra):
            raise Bad(f"Ordner „{extra}“ gibt es nicht (USB-Stick eingesteckt?)")
        if not os.access(extra, os.W_OK):
            raise Bad(f"In „{extra}“ darf Hauskern nicht schreiben")
    c = dict(app.cfg.get("backup") or {})
    c.update(enabled=bool(vals.get("enabled")), time=vals.get("time"), extra_dir=extra, pi_pull=bool(vals.get("pi_pull")))
    app.cfg["backup"] = c
    app.save_nas(n)
    return True, "Gespeichert", {}



# --- Zeitraffer ------------------------------------------------------------- #
def get_timelapse(app, ident):
    import timelapse as T
    c = app.tl_cfg()
    cams = [("printer", "Kamera im Drucker")] + [(x["id"], f"Kamera „{x['name']}“") for x in app.cfg.get("cameras", [])]
    fields, vals = [], {}
    for serial, pr in app.printers.items():
        fields.append(F("mode:" + serial, f"🖨️ {pr.name}: aufnehmen", "choice", T.MODES))
        fields.append(F("src:" + serial, f"🖨️ {pr.name}: Bilder von", "choice", cams))
        vals["mode:" + serial] = app.tl_mode(serial)
        vals["src:" + serial] = ((app.cfg.get("timelapse") or {}).get("sources") or {}).get(serial, "printer")
    if not app.printers:
        fields.append(F("info", "Noch kein 3D-Drucker eingerichtet", "info"))
    fields += [F("interval", "Ohne Schichtangabe: alle … Sekunden ein Bild", "number"),
               F("width", "Videobreite", "choice", [(1280, "1280 px (spart Platz)"), (1920, "1920 px (Full HD)")]),
               F("max_gb", "Höchstens so viel Speicher (GB)", "number", hint="darüber werden die ältesten Videos gelöscht"),
               F("keep_days", "Videos löschen nach (Tagen)", "number", hint="0 = nie")]
    vals.update(interval=c["interval"], width=int(c.get("width", 1280)), max_gb=c["max_gb"], keep_days=c["keep_days"])
    used = T.fmt_size(app.tl.usage())
    return _form(fields, vals, hint=f"Standardmäßig aus. „Nur wenn ich ihn einschalte“: Am Drucker auf dem Handy tippst "
                                    f"du „🎬 Zeitraffer“ – gilt dann für diesen einen Druck.\nEin Bild pro Schicht, die "
                                    f"Einzelbilder werden nach dem Video gelöscht.\nBelegt: {used} · Ordner: "
                                    f"{app.tl.out_dir}", title="Zeitraffer")


def post_timelapse(app, op, ident, vals, user):
    import timelapse as T
    c = dict(app.cfg.get("timelapse") or {})
    modes, sources = dict(c.get("modes") or {}), dict(c.get("sources") or {})
    valid_src = ["printer"] + [x["id"] for x in app.cfg.get("cameras", [])]
    for serial in app.printers:
        if "mode:" + serial in vals:
            modes[serial] = _choice(vals["mode:" + serial], T.MODES, "Aufnehmen")
        if "src:" + serial in vals:
            sources[serial] = vals["src:" + serial] if vals["src:" + serial] in valid_src else "printer"
    c.update(modes=modes, sources=sources, interval=int(_num(vals.get("interval") or 20, "Sekunden", 5, 600)),
             width=1920 if str(vals.get("width")) == "1920" else 1280,
             max_gb=_num(vals.get("max_gb") or 5, "Speicher", 0.2, 2000),
             keep_days=int(_num(vals.get("keep_days") if vals.get("keep_days") not in (None, "") else 60, "Tage", 0, 3650)))
    app.save_tl_cfg(c)
    on = [app.printers[s_].name for s_, m in modes.items() if m != "off" and s_ in app.printers]
    return True, "Gespeichert" + (f" – Zeitraffer für {', '.join(on)}" if on else " – Zeitraffer ist aus"), {}



# --- Kamera-Aufnahmen --------------------------------------------------------- #
def get_clips(app, ident):
    import timelapse as T
    c = app.clips_cfg()
    fields = [F("max_gb", "Höchstens so viel Speicher (GB)", "number", hint="darüber werden die ältesten Aufnahmen gelöscht"),
              F("keep_days", "Aufnahmen löschen nach (Tagen)", "number", hint="0 = nie")]
    vals = {"max_gb": c["max_gb"], "keep_days": c["keep_days"]}
    return _form(fields, vals, hint=f"Gilt für Aufnahmen aus dem Kamerafenster und aus Abläufen („Kamera-Aufnahme“). "
                                    f"Ansehen: Kameras → 🎥 Aufnahmen.\nBelegt: {T.fmt_size(app.clips_usage())} · Ordner: "
                                    f"{app.clips_dir()}", title="Kamera-Aufnahmen")


def post_clips(app, op, ident, vals, user):
    c = {"max_gb": _num(vals.get("max_gb") or 10, "Speicher", 0.2, 2000),
         "keep_days": int(_num(vals.get("keep_days") if vals.get("keep_days") not in (None, "") else 30, "Tage", 0, 3650))}
    app.save_clips_cfg(c)
    return True, "Gespeichert", {}


# --- Weitere Hersteller -------------------------------------------------------- #
def get_integrations(app, ident):
    import providers as PR
    lst = app.cfg.get("integrations", [])
    if ident is None:
        items = [{"id": c["id"], "title": f"{PR.TYPES.get(c['type'], (c['type'],))[0]}: {app.integration_title(c)}",
                  "sub": app.integration_status(c["id"])} for c in lst]
        sug = app.integration_suggestions()
        hint = ("Geräte anderer Marken einbinden – alles läuft lokal im Heimnetz, ohne Hersteller-Cloud. "
                "Danach funktionieren Kacheln, Abläufe, Urlaubsmodus, Waschmaschine & Co. und Siri auch mit ihnen.")
        if sug:
            hint += "\n\n🔎 In deinem Netz gefunden: " + ", ".join(f"{n} ({PR.TYPES[t][0]}, {ip})" for ip, n, t in sug[:6])
        return _list(items, hint=hint, title="Weitere Hersteller")
    c = next((x for x in lst if x["id"] == ident), None) or {"type": "shelly", "host": "", "enabled": True}
    sug = app.integration_suggestions()
    typ_opts = [(k, f"{v[0]} – {v[1]}") for k, v in PR.TYPES.items()]
    fields = [F("type", "Hersteller", "choice", typ_opts),
              F("host", "Adresse im Heimnetz (IP)", hint="z. B. 192.168.178.50" + (
                  " · gefunden: " + ", ".join(f"{ip} ({n})" for ip, n, _ in sug[:4]) if sug else "")),
              F("name", "Name (optional)", hint="leer = Name vom Gerät")]
    t = c.get("type")
    fields += [F("user", "Benutzer (nur falls am Gerät ein Passwort gesetzt ist)",
                 hint="Shelly Gen2+: leer lassen · Hue: nicht nötig"),
               F("pw", "Passwort (nur falls gesetzt)", "password", hint="leer lassen = bleibt" if ident else ""),
               F("port", "MQTT-Port (nur Zigbee)", "number"),
               F("base", "MQTT-Thema (nur Zigbee)", hint="Standard: zigbee2mqtt"),
               F("mac", "MAC-Adresse (nur Fernseher, optional)",
                 hint="zum Einschalten aus dem echten Aus · steht in der FRITZ!Box unter Heimnetz → Netzwerk"),
               F("enabled", "Eingeschaltet", "bool")]
    vals = {"type": t, "host": c.get("host", ""), "name": c.get("name", ""), "user": c.get("user", ""), "pw": "",
            "port": c.get("port", 1883), "base": c.get("base", "zigbee2mqtt"), "mac": c.get("mac", ""),
            "enabled": c.get("enabled", True)}
    btns = [{"op": "detect", "label": "🔎 Erkennen"}, {"op": "pair", "label": "🔗 Hue Bridge koppeln"}]
    hint = ("Tipp: Adresse eintragen und „Erkennen“ tippen – Hauskern findet selbst heraus, was es ist.\n"
            "Philips Hue: erst den runden Knopf auf der Bridge drücken, dann innerhalb von 30 Sekunden "
            "„Hue Bridge koppeln“." + (" ✔ Bereits gekoppelt." if c.get("key") else "") + "\n"
            "Zigbee: Adresse des MQTT-Servers (Mosquitto) auf dem Heimserver, an dem Zigbee2MQTT hängt.\n"
            "Fernseher (Hanseatic & Co.): im TV-Menü „Virtuelle Fernbedienung“ und – falls vorhanden – "
            "„Wake on LAN/WLAN“ einschalten.")
    if ident:
        hint = (hint + "\n" if hint else "") + "Status: " + app.integration_status(ident)
    return _form(fields, vals, hint=hint, buttons=btns, title=c.get("name") or "Neuer Hersteller")


def post_integrations(app, op, ident, vals, user):
    import providers as PR
    old = next((x for x in app.cfg.get("integrations", []) if x["id"] == ident), None) or {}
    if op == "delete":
        app.remove_integration(ident)
        return True, "Entfernt", {}
    typ = _choice(vals.get("type") or "shelly", [(k, k) for k in PR.TYPES], "Hersteller")
    host = str(vals.get("host") or "").strip()
    if not re.match(r"^[A-Za-z0-9.\-:\[\]]{3,80}$", host):
        raise Bad("Bitte die Adresse (IP) des Geräts eintragen")
    if op == "detect":
        found, name = PR.detect_type(host)
        if not found:
            raise Bad(name + ". Tipp: Hersteller oben von Hand wählen und „Speichern“ – Hauskern zeigt dann, "
                             "woran es hakt.")
        return True, f"Gefunden: {PR.TYPES[found][0]} „{name}“", {"set": {"type": found, "name": vals.get("name") or name}}
    c = {"id": ident or f"i{int(time.time() * 1000) % 10**9:x}", "type": typ, "host": host,
         "name": str(vals.get("name") or "").strip(), "enabled": bool(vals.get("enabled", True)),
         "user": str(vals.get("user") or "").strip()}
    pw = str(vals.get("pw") or "")
    if pw:
        c["pw_new"] = pw  # die App verschlüsselt es beim Speichern
    else:
        c["pw"] = old.get("pw", "") if typ == old.get("type") and host == old.get("host") else ""
    if typ == "z2m":
        c["port"] = int(_num(vals.get("port") or 1883, "Port", 1, 65535))
        c["base"] = str(vals.get("base") or "zigbee2mqtt").strip().strip("/") or "zigbee2mqtt"
    if typ == "vestel":
        mac = str(vals.get("mac") or "").strip().upper().replace("-", ":")
        if mac and not re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", mac):
            raise Bad("MAC-Adresse bitte in der Form AA:BB:CC:DD:EE:FF")
        if not mac:  # aus der Geräteliste der FRITZ!Box nehmen
            h = next((x for x in (app.data.get("hosts") or []) if x.get("ip") == host and x.get("mac")), None)
            mac = (h["mac"].upper() if h else "")
        c["mac"] = mac
    if op == "pair" and typ != "hue":
        raise Bad("Koppeln gibt es nur bei Philips Hue – bei den anderen einfach „Speichern“")
    if typ == "hue":
        c["key"] = old.get("key", "") if host == old.get("host") else ""
        if op == "pair":
            from prov_hue import pair
            try:
                c["key"] = pair(host)
            except PR.ProviderError as e:
                raise Bad(str(e)) from e
            app.save_integration(c)
            return True, "✔ Mit der Hue Bridge gekoppelt – Lampen erscheinen gleich", {}
        if not c["key"]:
            app.save_integration(c)
            raise Bad("Gespeichert – jetzt noch koppeln: Knopf auf der Bridge drücken, dann „Mit Hue Bridge koppeln“")
    app.save_integration(c)
    return True, f"„{c['name'] or host}“ gespeichert – Geräte erscheinen gleich", {}


# --- Urlaubsmodus ------------------------------------------------------------ #
def _switch_opts(app):
    return [(d["ain"], d["name"]) for d in app.data.get("smart", [])
            if not d.get("is_group") and (d.get("switch") is not None or d.get("simpleonoff") is not None)]


def get_vacation(app, ident):
    import vacation as VA
    c = app.vacation_cfg()
    has_heat = any(d.get("hkr") for d in app.data.get("smart", []))
    fields = [F("active", "🏖 Urlaubsmodus ist an", "bool"),
              F("until", "Rückkehr am", "date", hint="leer = bis du ihn ausschaltest"),
              F("back", "Ankunft ungefähr um", "time"),
              F("plugs", "Licht abends einschalten", "multi", _switch_opts(app),
                hint="Lampen an Steckdosen – jeden Abend zu leicht anderen Zeiten, jede Lampe für sich"),
              F("start", "Licht an", "choice", VA.START_CHOICES),
              F("end", "Licht aus um", "time"),
              F("spread", "Zufall (± Minuten)", "number")]
    if has_heat:
        fields += [F("heat", "Heizung absenken", "bool"),
                   F("heat_temp", "Höchstens (°C)", "number", hint="FRITZ!DECT-Heizkörperregler bleiben darunter"),
                   F("preheat_h", "Vor der Rückkehr wieder aufheizen (Stunden)", "number",
                     hint="braucht ein Rückkehrdatum – dann ist es warm, wenn ihr ankommt")]
    fields += [F("cam_push", "Push mit Bild, wenn jemand an der Kamera ist", "bool",
                 hint="mit Kamera-Erkennung nur bei Personen, sonst bei jeder Bewegung"),
               F("auto_end", "Automatisch aus, wenn jemand nach Hause kommt", "bool",
                 hint="erkannt am Handy im WLAN – Lampen und Heizung gehen dann zurück")]
    vals = {k: c[k] for k in ("active", "until", "back", "plugs", "start", "end", "spread", "heat", "heat_temp",
                              "preheat_h", "cam_push", "auto_end")}
    hint = f"Status: {VA.describe(c)}."
    if c["start"] == "sunset" and not app._location():
        hint += " Standort unbekannt – „bei Sonnenuntergang“ nimmt bis dahin 17:30 Uhr (Wetter-Ort einstellen hilft)."
    if not _switch_opts(app):
        hint += " Noch keine schaltbare FRITZ!DECT-Steckdose gefunden."
    return _form(fields, vals, hint=hint, title="Urlaubsmodus")


def post_vacation(app, op, ident, vals, user):
    import vacation as VA
    c = app.vacation_cfg()
    until = str(vals.get("until") or "").strip()
    if until and not re.match(r"^\d{4}-\d{2}-\d{2}$", until):
        raise Bad("Rückkehr bitte als Datum")
    back = str(vals.get("back") or "18:00").strip()
    end = str(vals.get("end") or "23:00").strip()
    for t, what in ((back, "Ankunft"), (end, "Licht aus")):
        if not re.match(r"^\d{1,2}:\d{2}$", t):
            raise Bad(f"{what} bitte als HH:MM")
    valid = {a for a, _ in _switch_opts(app)} | set(c["plugs"])
    c.update(until=until, back=back, end=end,
             plugs=[a for a in (vals.get("plugs") or []) if a in valid],
             start=_choice(vals.get("start") or "sunset", VA.START_CHOICES, "Licht an"),
             spread=int(_num(vals.get("spread") if vals.get("spread") not in (None, "") else 30, "Zufall", 0, 120)),
             cam_push=bool(vals.get("cam_push")), auto_end=bool(vals.get("auto_end")))
    if "heat" in vals:
        c.update(heat=bool(vals.get("heat")),
                 heat_temp=_num(vals.get("heat_temp") or 16, "Temperatur", 8, 28),
                 preheat_h=_num(vals.get("preheat_h") if vals.get("preheat_h") not in (None, "") else 3,
                                "Stunden", 0, 48))
    app.save_vacation_cfg(dict(c))
    want = bool(vals.get("active"))
    who = (user or {}).get("name", "")
    if want != c["active"]:
        try:
            msg = app.vacation_set(want, who=who)
        except ValueError as e:
            raise Bad(str(e)) from e
        return True, msg, {}
    return True, "Gespeichert" + (f" – {VA.describe(app.vacation_cfg())}" if c["active"] else ""), {}


# --- Waschmaschine & Co. ------------------------------------------------------ #
def _power_opts(app):
    return [(d["ain"], d["name"]) for d in app.data.get("smart", [])
            if not d.get("is_group") and d.get("power_w") is not None]


def get_appliances(app, ident):
    import appliances as AP
    lst = app.appliances()
    names = dict(_power_opts(app))
    if ident is None:
        now = time.time()
        items = []
        for a in lst:
            state, sub = AP.status(a, app.appl_state.get(a["id"]), now)
            items.append({"id": a["id"], "title": f"{AP.icon(a)} {a['name']}",
                          "sub": f"{names.get(a.get('ain'), 'Steckdose fehlt')} · {state}" + (f" {sub}" if sub else "")})
        return _list(items, hint="Hängt Waschmaschine, Trockner oder Spülmaschine an einer FRITZ!DECT-Messsteckdose, "
                                 "meldet Hauskern per Push, wenn das Programm fertig ist.", title="Waschmaschine & Co.")
    a = next((x for x in lst if x["id"] == ident), None) or AP.norm({"kind": "wash", "name": ""})
    fields = [F("name", "Name", hint="z. B. Waschmaschine Keller"),
              F("kind", "Art", "choice", AP.KIND_CHOICES),
              F("ain", "Messsteckdose", "choice", [("", "– bitte wählen –")] + _power_opts(app)),
              F("remind_min", "Erinnern, wenn nicht ausgeräumt – nach (Minuten)", "number",
                hint="0 = keine Erinnerung. Hauskern merkt nicht, ob schon ausgeräumt wurde – erinnert also einmal."),
              F("info", "Nur ändern, wenn es zu früh oder zu spät meldet:", "info"),
              F("start_w", "Läuft ab (Watt)", "number"),
              F("end_w", "Fertig unter (Watt)", "number"),
              F("end_min", "… für mindestens (Minuten)", "number",
                hint="länger = keine Fehlmeldung bei Pausen (Einweichen, Trocknen)")]
    vals = {k: a.get(k) for k in ("name", "kind", "ain", "remind_min", "start_w", "end_w", "end_min")}
    return _form(fields, vals, title=a.get("name") or "Neues Gerät")


def post_appliances(app, op, ident, vals, user):
    import appliances as AP
    if op == "delete":
        app.remove_appliance(ident)
        return True, "Entfernt", {}
    old = next((x for x in app.cfg.get("appliances", []) if x.get("id") == ident), None) or {}
    kind = _choice(vals.get("kind") or "wash", AP.KIND_CHOICES, "Art")
    ain = str(vals.get("ain") or "")
    if ain not in dict(_power_opts(app)) and ain != old.get("ain"):
        raise Bad("Bitte eine Messsteckdose auswählen")
    name = str(vals.get("name") or "").strip() or AP.KINDS[kind]["name"]
    a = {"id": ident or f"ap{int(time.time() * 1000) % 10**9}", "name": name, "kind": kind, "ain": ain,
         "remind_min": int(_num(vals.get("remind_min") or 0, "Erinnerung", 0, 1440)), "enabled": True}
    olddef = AP.defaults(old.get("kind", "wash"))  # das Formular zeigt beim Neuanlegen die Waschmaschinen-Werte
    for k, lo, hi in (("start_w", 1, 3000), ("end_w", 0, 500), ("end_min", 1, 120)):
        v = vals.get(k)
        if v in (None, ""):
            continue
        v = _num(v, k, lo, hi)
        if v != olddef[k] and v != AP.defaults(kind)[k]:  # nur echte Anpassungen merken – sonst gilt die Art
            a[k] = v
    if a.get("end_w", AP.defaults(kind)["end_w"]) >= a.get("start_w", AP.defaults(kind)["start_w"]):
        raise Bad("„Fertig unter“ muss kleiner sein als „Läuft ab“")
    app.save_appliance(a)
    return True, f"„{name}“ gespeichert – meldet sich, wenn das Programm fertig ist", {}


# --- Filament-Lager ---------------------------------------------------------- #
def get_filament(app, ident):
    import filament as FL
    sp = app.spools()
    lim = app.fil_low_limit()
    if ident is None:
        items = []
        for k, txt, _t in app.fil_unknown():
            items.append({"id": "tray:" + k, "title": "➕ Neu im AMS – übernehmen", "sub": txt})
        for x in sorted(sp, key=lambda q: (not q.get("slot"), float(q.get("left") or 0))):
            where = app.fil_slot_label(x.get("slot")) or x.get("loc") or "im Lager"
            warn = " ⚠ knapp" if float(x.get("left") or 0) < lim else ""
            items.append({"id": x["id"], "title": ("● " if x.get("slot") else "") + x["name"] + warn,
                          "sub": f"{FL.fmt_left(x)} · {x['type']} · {where}"})
        ap = app.ams_low_pct()
        items.append({"id": "_limit", "title": f"⚙ Warnen unter {lim:g} g",
                      "sub": "Push, wenn eine Spule knapp wird · AMS: " + (f"unter {ap} %" if ap else "aus")})
        su = FL.summary(sp, lim)
        hint = (f"{su['count']} Spulen · zusammen {su['grams'] / 1000:.1f} kg".replace(".", ",") if sp else
                "Trag hier deine Spulen ein. Nach jedem Druck zieht Hauskern den Verbrauch automatisch ab – "
                "Bambu-Rollen mit Chip übernimmt es direkt aus dem AMS.")
        return dict(_list(items, title="Filament-Lager", hint=hint), new_label="＋ Spule hinzufügen",
                    empty="Noch keine Spulen.")
    if ident == "_limit":
        return _form([F("limit", "Warnen, wenn weniger übrig ist als (g)", "number"),
                      F("ams", "AMS-Rollen: warnen unter (%)", "number", hint="0 = keine AMS-Warnung")],
                     {"limit": lim, "ams": app.ams_low_pct()},
                     title="Warnung", hint="Die Push-Nachricht kommt nur EINMAL pro Spule bzw. Rolle – erst wieder, "
                                           "wenn eine neue oder aufgefüllte Rolle drin ist.")
    x = next((q for q in sp if q["id"] == ident), None)
    if ident and ident.startswith("tray:"):
        t = next((t for k, _txt, t in app.fil_unknown() if k == ident[5:]), None)
        x = FL.from_tray(ident[5:], t) if t else None
        if x:
            x["id"] = ""
    x = x or FL.new_spool(id="")
    types = list(FL.TYPES) + ([x["type"]] if x["type"] not in FL.TYPES else [])
    fields = [F("name", "Name", hint="leer = automatisch, z. B. „Bambu PLA Rot“"),
              F("type", "Material", "choice", [(t, t) for t in types]),
              F("color", "Farbe", "color"), F("brand", "Hersteller"),
              F("total", "Inhalt einer vollen Spule (g)", "number"), F("left", "Noch drauf (g)", "number"),
              F("slot", "Steckt in", "choice", [("", "– nicht im Drucker –")] + app.fil_slots()),
              F("loc", "Lagerort", hint="z. B. „Trockenbox 1“"),
              F("tare", "Gewicht der leeren Spule (g)", "number", hint="zum Nachwiegen – einmal leere Spule wiegen"),
              F("gross", "Nachgewogen: Spule mit Filament (g)", "number", hint="optional – setzt „noch drauf“ neu"),
              F("minus", "Verbrauch abziehen (g)", "number", hint="optional – z. B. Druck an einem anderen Drucker")]
    vals = {k: x.get(k, "") for k in ("name", "type", "color", "brand", "total", "left", "slot", "loc", "tare")}
    vals["left"] = round(float(x.get("left") or 0))
    vals.update(gross="", minus="")
    hint = ("Bambu-Rolle mit Chip: Die Restmenge kommt automatisch vom AMS." if x.get("uuid") else "")
    if x.get("used"):
        hint = (hint + "\n" if hint else "") + f"Bisher verbraucht: {round(float(x['used']))} g"
    return _form(fields, vals, title=x["name"] if x.get("id") else "Neue Spule", hint=hint)


def post_filament(app, op, ident, vals, user):
    import filament as FL
    if ident == "_limit":
        app.cfg["filament_low"] = _num(vals.get("limit") or 150, "Grenze", 0, 5000)
        if vals.get("ams") not in (None, ""):
            app.cfg["ams_low_pct"] = int(_num(vals.get("ams"), "AMS-Grenze", 0, 50))
        app._fil_after_change()
        return True, "Gespeichert", {}
    if op == "delete":
        return (True, "Spule entfernt", {}) if app.fil_delete(ident) else (False, "nicht gefunden", {})
    total = _num(vals.get("total") or 1000, "Inhalt", 1, 100000)
    left = _num(vals.get("left") if vals.get("left") not in (None, "") else total, "Noch drauf", 0, 100000)
    typ = str(vals.get("type") or "PLA")[:30]
    slot = str(vals.get("slot") or "")
    if slot and slot not in dict(app.fil_slots()):
        slot = ""
    spool = {"type": typ, "color": FL.norm_color(vals.get("color")), "brand": str(vals.get("brand") or "").strip()[:40],
             "total": total, "left": left, "slot": slot, "loc": str(vals.get("loc") or "").strip()[:60],
             "tare": _num(vals.get("tare") or 0, "Leerspule", 0, 5000)}
    spool["name"] = str(vals.get("name") or "").strip()[:60] or FL.suggest_name(spool)
    old = next((q for q in app.spools() if q["id"] == ident), None) if ident and not ident.startswith("tray:") else None
    if old:
        spool["id"] = old["id"]
    elif ident and ident.startswith("tray:"):
        t = next((t for k, _txt, t in app.fil_unknown() if k == ident[5:]), None)
        if t and t.get("rfid"):
            spool["uuid"] = t.get("uuid", "")
    gross = vals.get("gross")
    if gross not in (None, ""):
        gross = _num(gross, "Gewogen", 0, 100000)
        if not spool["tare"]:
            raise Bad("Zum Nachwiegen bitte einmal das Gewicht der leeren Spule eintragen")
    minus = vals.get("minus")
    minus = _num(minus, "Verbrauch", 0, 100000) if minus not in (None, "") else None
    s_ = app.fil_save(spool, gross=gross if gross not in (None, "") else None, minus=minus)
    return True, f"„{s_['name']}“ gespeichert – {FL.fmt_left(s_)}", {}



# --- Siri-Kurzbefehle (für jeden Nutzer: der eigene Schlüssel) -------------- #
SIRI_STEPS = ("So richtest du es ein (einmalig, ca. 2 Minuten):\n"
              "1. App „Kurzbefehle“ öffnen → ＋ → oben den Namen „Hauskern“ geben.\n"
              "2. Aktion „Text diktieren“ hinzufügen.\n"
              "3. Aktion „URL codieren“ hinzufügen (codiert den diktierten Text).\n"
              "4. Aktion „URL“ hinzufügen und die Adresse unten einfügen. Direkt dahinter (nach „t=“) das Ergebnis "
              "von „URL codieren“ als Variable einsetzen.\n"
              "5. Aktion „Inhalte von URL abrufen“ hinzufügen.\n"
              "6. Aktion „Text sprechen“ hinzufügen.\n"
              "Fertig: „Hey Siri, Hauskern“ → Siri fragt nach → „Schreibtisch aus“.\n\n"
              "Tipp: Für einen festen Befehl ohne Nachfrage brauchst du nur die Schritte 4–6 und schreibst den Befehl "
              "direkt hinter „t=“ – Leerzeichen als „+“, z. B. „…t=wie+weit+ist+der+drucker“. Kurzbefehl z. B. "
              "„Drucker“ nennen → „Hey Siri, Drucker“.\n"
              "Unterwegs klappt es, wenn WireGuard an ist. Es gelten deine Rechte in Hauskern.")


def get_siri(app, ident, user=None):
    if not user:
        raise Bad("Bitte neu anmelden")
    url = app.siri_url(user["id"])
    return dict(_form([F("url", "Deine Adresse für den Kurzbefehl", "copy"),
                  F("test", "Ausprobieren – Befehl eintippen", hint="z. B. „wie weit ist der drucker“ – "
                                                                     "Schalten passiert dabei wirklich")],
                 {"url": url, "test": ""}, title="🗣 Siri-Kurzbefehle", hint=SIRI_STEPS,
                 buttons=[{"op": "test", "label": "▶ Ausprobieren"}, {"op": "renew", "label": "Neuer Schlüssel"}]), nosave=True)


def post_siri(app, op, ident, vals, user):
    if op == "renew":
        app.siri_token(user["id"], renew=True)
        return True, "Neuer Schlüssel – bitte im Kurzbefehl die Adresse austauschen", {"set": {"url": app.siri_url(user["id"])}}
    if op == "test":
        t = str(vals.get("test") or "").strip()
        if not t:
            raise Bad("Bitte einen Befehl eintippen")
        return True, "🗣 " + app.siri_command(user, t), {}
    return True, "Nichts zu speichern – die Adresse gehört in den Kurzbefehl", {}



# --- Erweiterungen (nur Admins) -------------------------------------------- #
PLUGIN_WARN = ("Erweiterungen sind kleine Programme und dürfen alles, was Hauskern darf. Nur freigeben, was von dir "
               "oder einer Quelle kommt, der du vertraust. Ändert sich eine Datei, ist sie wieder aus, bis du sie "
               "erneut freigibst.")


def get_plugins(app, ident):
    import plugins as P
    infos = app.plugins.scan()
    if ident is None:
        items = [{"id": i["file"], "title": f"{i['name']}", "sub": f"{P.STATUS_TEXT.get(i['status'], i['status'])}"
                  + (f" · {i['description']}" if i["description"] else "")} for i in infos]
        hint = PLUGIN_WARN + f"\nOrdner: {app.plugins.folder}"
        if app.cfg.get("satellite"):
            hint = "Erweiterungen laufen auf dem Heimserver – dort einschalten.\n" + hint
        return dict(_list(items, title="Erweiterungen", hint=hint), new_label="＋ Beispiele hinzufügen",
                    empty="Noch keine Erweiterungen im Ordner. Mit „Beispiele hinzufügen“ kommen zwei zum Ausprobieren.")
    if ident == "":
        added = app.plugins.add_examples()
        return _form([F("info", ("Hinzugefügt: " + ", ".join(added)) if added else "Die Beispiele sind schon da.",
                        "info")], {}, title="Beispiele",
                     hint="Sie sind noch aus – zurück zur Liste, Erweiterung öffnen und „Aktiv“ einschalten.")
    i = next((x for x in infos if x["file"] == ident), None)
    if not i:
        raise Bad("Erweiterung nicht gefunden")
    fields = [F("info", f"Status: {P.STATUS_TEXT.get(i['status'], i['status'])}", "info"),
              F("enabled", "Aktiv (freigeben und einschalten)", "bool")]
    hint = (i["description"] + "\n" if i["description"] else "") + f"Datei: {i['file']}" + \
        (f" · Version {i['version']}" if i["version"] else "") + (f"\nFehler: {i['error']}" if i["error"] else "") + \
        "\n\n" + PLUGIN_WARN
    return _form(fields, {"enabled": i["status"] == "on"}, title=i["name"], hint=hint)


def post_plugins(app, op, ident, vals, user):
    if op == "delete":
        raise Bad("Löschen bitte im Ordner – hier nur ein- und ausschalten")
    if not ident:
        return True, "OK", {}
    if vals.get("enabled"):
        ok, err = app.plugins.enable(ident)
        app.log("App", ident, f"Erweiterung freigegeben von {user.get('name', '?')}")
        return (True, "Freigegeben und gestartet ✅", {}) if ok else (False, f"Startet nicht: {err}", {})
    app.plugins.disable(ident)
    return True, "Ausgeschaltet", {}



# --- Kamera-Erkennung ------------------------------------------------------- #
VISION_HINT = ("Bei Bewegung schaut Hauskern aufs Kamerabild und erkennt Person, Katze, Hund, Vogel oder Fahrzeug – "
               "komplett bei dir zu Hause, nichts geht ins Internet. In Automationen kannst du dann statt „Bewegung“ "
               "z. B. „sieht eine Person“ wählen (und dir per Push das Foto schicken lassen).\n"
               "Läuft auf dem Heimserver (Raspberry Pi 5 / Mini-PC). Das KI-Modell (YOLOX aus dem OpenCV-Modellzoo, "
               "Apache-2.0, ca. 36 MB) wird nur einmal geladen und mit der offiziellen Prüfsumme geprüft.")


def get_vision(app, ident):
    import vision as V
    c = app.vision_cfg()
    ok, _why = V.available()
    fields = [F("info", "Status: " + app.vision_status(), "info")]
    vals = {}
    if ok:
        fields.append(F("enabled", "Kamera-Erkennung einschalten", "bool"))
        vals["enabled"] = bool(c.get("enabled"))
        fields.append(F("sens", "Empfindlichkeit", "choice", [("niedrig", "niedrig – nur wenn ganz sicher"),
                                                             ("mittel", "mittel"), ("hoch", "hoch – meldet mehr")]))
        vals["sens"] = c.get("sens", "mittel")
        for cam in app.cfg.get("cameras", []):
            fields.append(F("cam:" + cam["id"], f"📷 {cam['name']}", "bool"))
            vals["cam:" + cam["id"]] = bool((c.get("cams") or {}).get(cam["id"], True))
    btns = [{"op": "download", "label": f"⬇ Modell laden ({V.MODEL_MB} MB)"}] if ok and not V.model_ok(
        app.vision.folder) else []
    return _form(fields, vals, title="Kamera-Erkennung", hint=VISION_HINT, buttons=btns)


def post_vision(app, op, ident, vals, user):
    import vision as V
    if op == "download":
        try:
            return True, "⬇ " + app.vision_download() + " – dauert je nach Leitung 1–2 Minuten", {}
        except RuntimeError as e:
            raise Bad(str(e)) from None
    c = dict(app.vision_cfg())
    c["sens"] = _choice(vals.get("sens") or "mittel", [(k, k) for k in V.SENS], "Empfindlichkeit")
    cams = dict(c.get("cams") or {})
    for cam in app.cfg.get("cameras", []):
        if "cam:" + cam["id"] in vals:
            cams[cam["id"]] = bool(vals["cam:" + cam["id"]])
    c["cams"] = cams
    on = bool(vals.get("enabled"))
    if on and not V.model_ok(app.vision.folder):
        c["enabled"] = True
        app.cfg["vision"] = c
        app.save()
        try:
            app.vision_download()
        except RuntimeError as e:
            raise Bad(str(e)) from None
        return True, "Eingeschaltet – das Modell wird jetzt geladen (1–2 Minuten) ⬇", {}
    c["enabled"] = on
    app.cfg["vision"] = c
    app.save()
    app.log("Kamera", "Erkennung", "eingeschaltet" if on else "ausgeschaltet")
    return True, "Gespeichert" + (" – Kamera-Erkennung ist an 🧠" if on else ""), {}



# --- Wand-Display ----------------------------------------------------------- #
def get_wall(app, ident):
    urls = [u + "/wand" for u in app.web_urls()]
    fields = [F("url", "Adresse fürs Tablet", "copy")]
    if urls:
        fields.append(F("open", "Hier ansehen", "link", hint=urls[0]))
    hint = ("So geht's:\n"
            "1. Am besten einen eigenen Handy-Nutzer „Wand“ anlegen (Vorlage „Nur ansehen“, Einkaufsliste „steuern“) – "
            "dann kann das Tablet nichts Wichtiges umschalten.\n"
            "2. Auf dem Tablet die Adresse oben im Browser öffnen und mit der PIN von „Wand“ anmelden.\n"
            "3. Zum Home-Bildschirm hinzufügen (iPad: Teilen → „Zum Home-Bildschirm“, Android: ⋮ → „App installieren“) "
            "und von dort starten – dann ist es Vollbild.\n"
            "4. Bildschirm anlassen: iPad: Einstellungen → Anzeige → Automatische Sperre „Nie“. "
            "Android: Entwickleroptionen → „Aktiv lassen“ (beim Laden).\n"
            "Nachts (22–6 Uhr) wird es dunkel und zeigt nur die Uhr – Antippen weckt es kurz auf. "
            "Die Anmeldung bleibt bestehen, solange das Tablet läuft.")
    return dict(_form(fields, {"url": urls[0] if urls else "", "open": urls[0] if urls else ""},
                      title="Wand-Display", hint=hint), nosave=True)


def post_wall(app, op, ident, vals, user):
    return True, "Nichts zu speichern", {}
