# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
siri.py – Siri-Kurzbefehle (und alles andere, das Text schickt).

Ein iPhone-Kurzbefehl diktiert einen Satz und schickt ihn an /api/siri?k=<Schlüssel>&t=<Text>.
Hauskern versteht einfache deutsche Befehle und antwortet mit einem Satz, den Siri vorliest.

Beispiele:
  „Schreibtisch aus“ · „Schalte die Stehlampe an“ · „Wohnzimmer auf 21 Grad“ · „Bad Heizung aus“
  „Wie weit ist der Drucker?“ · „Wer ist zu Hause?“ · „Ist Anna da?“ · „Wie warm ist es im Wohnzimmer?“
  „Starte Feierabend“ · „Gast-WLAN an“ · „Wie viel Strom verbrauchen wir?“ · „Batterien“ · „Filament“

Jeder Schlüssel gehört zu einem Handy-Nutzer – es gelten dessen Rechte.
"""
import re
import secrets
import time

ON = r"\b(an|ein|einschalten|anschalten|anmachen|aktivieren|start|starten|starte|los)\b"
OFF = r"\b(aus|ausschalten|ausmachen|abschalten|abdrehen|deaktivieren|stopp|stop)\b"
FILLER = {"der", "die", "das", "den", "dem", "im", "in", "ist", "es", "bitte", "mal", "schalte", "schalt", "mach",
          "mache", "stell", "stelle", "auf", "grad", "zu", "hause", "hauskern", "hey", "siri", "und", "von", "vom"}
GENERIC = {"heizung", "heizkörper", "heizkoerper", "thermostat", "steckdose", "lampe", "licht", "schalter", "sensor",
           "regler", "gerät", "geraet"}

HELP = ("Ich kann zum Beispiel: Schreibtisch aus, Wohnzimmer auf 21 Grad, wie weit ist der Drucker, "
        "wer ist zu Hause, wer hat angerufen, Milch auf die Liste, wie warm ist es im Bad, starte Feierabend, Gast WLAN an, Stromverbrauch, Batterien, "
        "Filament, Urlaubsmodus an, ist die Waschmaschine fertig, Sky Pause, Fernseher leiser oder Sky auf Sender 102.")


def new_token():
    return secrets.token_urlsafe(18)


def norm(t):
    t = (t or "").lower().replace("ß", "ss")
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"[^\w\s,.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def speak_num(v, d=1):
    if v is None:
        return "unbekannt"
    v = round(float(v), d)
    return (f"{v:.{d}f}".rstrip("0").rstrip(".") if d else str(int(v))).replace(".", ",")


def find_target(text, targets):
    """Längster Name, der im Satz vorkommt. targets: [(name, obj)] → (obj, name) oder (None, None)."""
    t = " " + norm(text) + " "
    best = None
    for name, obj in targets:
        n = norm(name)
        if not n:
            continue
        if f" {n} " in t or (len(n) > 4 and n in t):
            if not best or len(n) > len(best[1]):
                best = (obj, n)
    if best:
        return best
    # zweiter Versuch: alle wichtigen Wörter des Namens kommen vor („Heizung Bad“ ↔ „Bad Heizung“)
    words = set(t.split())
    for name, obj in targets:
        ws = [w for w in norm(name).split() if w not in FILLER and len(w) > 2]
        if ws and all(w in words for w in ws):
            if not best or len(" ".join(ws)) > len(best[1]):
                best = (obj, " ".join(ws))
    if best:
        return best
    # dritter Versuch: ein markantes Wort des Namens reicht („Wohnzimmer“ ↔ „Wohnzimmer Heizung“) – wenn eindeutig
    scored = []
    for name, obj in targets:
        ws = [w for w in norm(name).split() if w not in FILLER and w not in GENERIC and len(w) > 2]
        hit = [w for w in ws if w in words]
        if hit:
            scored.append((len(" ".join(hit)), obj, " ".join(hit)))
    if scored:
        scored.sort(key=lambda x: -x[0])
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            return scored[0][1], scored[0][2]
    return None, None


def number(text):
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:grad|°)?", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def media_intent(t, question=False):
    """Fernbedienungs-Wunsch im Satz → (Taste, Zahl) oder (None, None)."""
    n = number(t)
    if re.search(r"\b(was laeuft|was läuft|welcher sender|was kommt)\b", t):
        return "now", None
    if question:
        return None, None
    if re.search(r"\b(stumm|ton aus|ton an|ton wieder an|mute)\b", t):
        return "mute", None
    if re.search(r"\blauter\b", t):
        return "vol_up", n
    if re.search(r"\bleiser\b", t):
        return "vol_down", n
    if re.search(r"\b(naechster|nächster|naechsten|nächsten) (sender|kanal)\b|\bsender (hoch|weiter|plus)\b", t):
        return "ch_up", None
    if re.search(r"\b(vorheriger|vorherigen|letzter|letzten) (sender|kanal)\b|\bsender (runter|zurueck|zurück|minus)\b", t):
        return "ch_down", None
    m = re.search(r"\b(?:sender|kanal|programm)\D{0,12}(\d{1,4})\b", t)
    if m:
        return "channel", int(m.group(1))
    if re.search(r"\b(pause|pausier\w*|anhalten|halt \w+ an)\b", t):
        return "pause", None
    if re.search(r"\b(fortsetzen|weiterlaufen|weiter abspielen|abspielen|play|mach weiter|weiter)\b", t):
        return "play", None
    if re.search(r"\b(vorspulen)\b", t):
        return "ffw", None
    if re.search(r"\b(zurueckspulen|zurückspulen)\b", t):
        return "rew", None
    return None, None


def media_answer(app, dev, name, k, val, media, can):
    if k == "now":
        if not can("smart", 1):
            return "Das darfst du leider nicht."
        if not dev.get("switch"):
            return f"{name} ist aus."
        now = (dev.get("media") or {}).get("now")
        return f"Auf {name} läuft {now}." if now else f"{name} ist an, was genau läuft, weiß ich nicht."
    if not can("smart", 2):
        return "Das darfst du leider nicht."
    keys = (dev.get("media") or {}).get("keys", [])
    if k not in keys and k != "channel":  # z. B. „Sky lauter“ → das macht der Fernseher
        other = next((d for d in media if k in (d.get("media") or {}).get("keys", [])), None)
        if not other:
            return f"Das kann {name} leider nicht."
        dev, name = other, other["name"]
    times = 1
    if k in ("vol_up", "vol_down"):
        times = max(1, min(15, int(val))) if val else 3
    for _ in range(times):
        app.run_action({"type": "media", "ain": dev["ain"], "key": k, "value": val if k == "channel" else ""})
    app.log("Sprache", name, f"Taste {k}" + (f" {val}" if k == "channel" else "") + " (Siri)")
    return {"pause": f"{name} pausiert.", "play": f"{name} läuft weiter.", "mute": "Ton umgeschaltet.",
            "vol_up": "Lauter.", "vol_down": "Leiser.", "ch_up": "Nächster Sender.", "ch_down": "Vorheriger Sender.",
            "ffw": "Spule vor.", "rew": "Spule zurück."}.get(k, f"Sender {val}." if k == "channel" else "Okay.")


def handle(app, user, text, can):
    """→ Antworttext. can(area, level) prüft die Rechte des Nutzers."""
    raw = (text or "").strip()
    t = norm(raw)
    if not t or re.search(r"\b(hilfe|was kannst du|befehle)\b", t):
        return HELP
    # --- Erweiterungen (eigene Siri-Befehle) ---
    if getattr(app, "plugins", None) and app.plugins.active and can("smart", 1):
        ans = app.plugins.siri(raw)
        if ans is not None:
            return ans
    has_on, has_off = bool(re.search(ON, t)), bool(re.search(OFF, t))
    # „Ist die Stehlampe an?“ ist eine Frage – nicht schalten, nur antworten
    question = raw.rstrip().endswith("?") or bool(re.match(r"^(ist|sind|läuft|laeuft|wie|was|wann|ob|hat|haben)\b", t))
    if question:
        has_on = has_off = False
    smart = [d for d in app.data.get("smart", []) or [] if not (d.get("is_group") and d.get("switch") is None)]
    names = app.entity_names() if hasattr(app, "entity_names") else {}

    def dname(d):
        return names.get(d["ain"]) or d["name"]

    # --- Gast-WLAN ---
    if re.search(r"\bgast", t) and re.search(r"\b(wlan|w lan|netz|wifi)\b", t):
        if has_on or has_off:
            if not can("net", 2):
                return "Das darfst du leider nicht."
            try:
                app.set_guest(has_on and not has_off, source="Siri")
            except Exception as e:  # noqa: BLE001
                return f"Das hat nicht geklappt: {e}"
            return "Gast WLAN ist jetzt " + ("an." if has_on and not has_off else "aus.")
        w = app.guest_wlan() if hasattr(app, "guest_wlan") else None
        return ("Das Gast WLAN ist " + ("an." if w["enabled"] else "aus.")) if w else "Kein Gast WLAN gefunden."

    # --- Unwetterwarnungen ---
    if re.search(r"(unwetter|wetterwarnung|warnung|gewitter|sturm)", t) and hasattr(app, "web_wxalerts"):
        al = app.web_wxalerts()
        if not app.weather_cfg().get("alerts", True):
            return "Die Unwetterwarnungen sind ausgeschaltet."
        if not al:
            return "Gerade gibt es keine Wetterwarnung für deinen Ort."
        return " ".join(a["headline"] + (f", {a['when']} Uhr" if a["when"] else "") + "." for a in al[:2])

    # --- Urlaubsmodus ---
    if re.search(r"urlaub", t) and hasattr(app, "vacation_cfg"):
        import vacation as VA
        if not can("smart", 1):
            return "Das darfst du leider nicht."
        if has_on or has_off:
            if not can("smart", 2):
                return "Das darfst du leider nicht."
            try:
                msg = app.vacation_set(has_on and not has_off, who=user.get("name", "") or "Siri")
            except Exception as e:  # noqa: BLE001
                return f"Das hat nicht geklappt: {e}"
            return re.sub(r"^\W+", "", msg) + "."
        c = app.vacation_cfg()
        if not c["active"]:
            return "Der Urlaubsmodus ist aus."
        return "Der Urlaubsmodus ist " + VA.describe(c) + (" Uhr." if VA.until_ts(c) else ".")

    # --- Waschmaschine & Co. ---
    if hasattr(app, "appliances") and re.search(r"(wasch|wäsche|waesche|trockner|spül|spuel|geschirr|fertig|läuft)", t):
        import appliances as AP
        hits = []
        for a in app.appliances():
            kw = {"wash": r"(wasch|wäsche|waesche)", "dry": r"trockner", "dish": r"(spül|spuel|geschirr)"}.get(a["kind"])
            if (kw and re.search(kw, t)) or (norm(a["name"]) and norm(a["name"]) in t):
                hits.append(a)
        named = {k for k, kw in (("wash", r"(wasch|wäsche|waesche)"), ("dry", r"trockner"),
                                 ("dish", r"(spül|spuel|geschirr)")) if re.search(kw, t)}
        if not hits and named:
            return "Die habe ich noch nicht eingerichtet. Das geht in Hauskern unter Waschmaschine und Co."
        if hits:
            if not can("smart", 1):
                return "Das darfst du leider nicht."
            now = time.time()
            out = []
            for a in hits:
                state, sub = AP.status(a, app.appl_state.get(a["id"]), now)
                out.append(f"{a['name']}: {'ist fertig' if state == 'fertig' else 'läuft noch' if state == 'läuft' else 'ist aus'}"
                           + (f", {sub.replace('Std.', 'Stunden').replace('Min.', 'Minuten')}" if sub else ""))
            return ". ".join(out) + "."

    # --- Einkaufsliste & Aufgaben ---
    if re.search(r"(liste|einkauf|aufgabe|gekauft|erledigt|abgehakt|brauchen wir|to ?do)", t) and "anruf" not in t:
        import lists as LS
        if can("lists", 1):
            ans = LS.siri(app.cfg, raw, user.get("name", ""), can("lists", 2))
            if ans is not None:
                app.save()
                if hasattr(app, "_lists_tile"):
                    app._lists_tile()
                return ans
        elif re.search(r"(einkaufsliste|auf die liste|aufgabenliste)", t):
            return "Das darfst du leider nicht."

    # --- Anrufe ---
    if re.search(r"(angerufen|anrufe|anruf\b|anrufliste|verpasst)", t):
        if not can("calls", 1):
            return "Das darfst du leider nicht."
        import calls as CL
        data = getattr(app, "call_data", None) or []
        if not data:
            return "Ich habe gerade keine Anrufliste." + (" " + app.call_err if getattr(app, "call_err", "") else "")
        t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        miss = [c for c in data if CL.is_missed(c) and c["ts"] >= t0 - 86400]
        if not miss:
            c = data[0]
            return f"Seit gestern hast du nichts verpasst. Zuletzt: {CL.who(c)}, {CL.when(c['ts'])}."
        parts = [f"{CL.who(c)} {CL.when(c['ts'])}" + (" mit Nachricht" if c["message"] else "") for c in miss[:4]]
        return (f"{len(miss)} verpasste Anrufe" if len(miss) > 1 else "Ein verpasster Anruf") + ": " + ", ".join(parts) + "."

    # --- Anwesenheit ---
    if re.search(r"\b(wer|ist\s+\w+\s+(da|zu hause|daheim)|jemand)\b", t) and re.search(r"\b(da|zu hause|daheim|weg)\b", t):
        if not can("home", 1):
            return "Das darfst du leider nicht."
        snap = app.presence_snapshot()
        persons = [p for p in app.cfg.get("persons", []) if p.get("kind") != "item"]
        p, _n = find_target(t, [(x["name"], x) for x in persons])
        if p:
            st = snap.get(p["id"])
            return f"{p['name']} ist " + ("zu Hause." if st == "home" else "nicht zu Hause." if st == "away" else
                                          "gerade unbekannt.")
        home = [x["name"] for x in persons if snap.get(x["id"]) == "home"]
        if not home:
            return "Gerade ist niemand zu Hause."
        return (" und ".join([", ".join(home[:-1]), home[-1]]) if len(home) > 1 else home[0]) + \
            (" ist zu Hause." if len(home) == 1 else " sind zu Hause.")

    # --- 3D-Drucker ---
    printers = getattr(app, "printers", {}) or {}
    pr_target, _ = find_target(t, [(p.name, p) for p in printers.values()])
    plug, plug_n = find_target(t, [(dname(d), d) for d in smart])
    asks_print = re.search(r"\b(wie weit|fertig|status|prozent|druckt|drucken)\b", t)
    if plug and not pr_target and not asks_print:  # z. B. Steckdose „Drucker 3D“ – die ist gemeint
        printers_hit = False
    else:
        printers_hit = bool(pr_target or re.search(r"\b(drucker|druck|drucken|druckt)\b", t))
    if printers and printers_hit and not has_on and not has_off:
        if not can("printers", 1):
            return "Das darfst du leider nicht."
        out = []
        for p in ([pr_target] if pr_target else list(printers.values())):
            s = p.summary()
            if not s.get("connected"):
                out.append(f"{p.name} ist nicht erreichbar.")
            elif s.get("state") in ("RUNNING", "PREPARE"):
                rem = s.get("remaining_min")
                eta = time.strftime("%H:%M", time.localtime(time.time() + int(rem) * 60)) if rem else None
                out.append(f"{p.name} ist bei {s.get('percent') or 0} Prozent" +
                           (f", fertig gegen {eta} Uhr." if eta else "."))
            elif s.get("state") == "PAUSE":
                out.append(f"{p.name} ist pausiert, bei {s.get('percent') or 0} Prozent.")
            elif s.get("state") == "FINISH":
                out.append(f"{p.name} ist fertig.")
            elif s.get("state") == "FAILED":
                out.append(f"{p.name}: Der Druck ist fehlgeschlagen.")
            else:
                out.append(f"{p.name} druckt gerade nicht.")
        return " ".join(out)

    # --- Strom ---
    if re.search(r"(strom|verbrauch|\bwatt\b)", t):
        if not can("power", 1) and not can("smart", 1):
            return "Das darfst du leider nicht."
        plugs = [d for d in smart if d.get("power_w") is not None and not d.get("is_group")]
        w = sum(d["power_w"] or 0 for d in plugs)
        rep = (getattr(app, "power_rep", None) or {}).get("total") or {}
        msg = f"Gerade {speak_num(w, 0)} Watt" + (" an einer Messsteckdose." if len(plugs) == 1 else
                                                     f" an {len(plugs)} Messsteckdosen.")
        if rep.get("month_eur") is not None and can("power", 1):
            msg += f" Diesen Monat bisher {speak_num(rep['month_eur'], 2)} Euro."
        return msg

    # --- Batterien ---
    if re.search(r"\bbatter", t):
        if not can("smart", 1):
            return "Das darfst du leider nicht."
        items = app.battery_items()
        if not items:
            return "Ich kenne keine Geräte mit Batterie."
        low = [b for b in items if b["low"]]
        if low:
            return "Batterie schwach bei " + ", ".join(b["name"] for b in low) + "."
        w = items[0]
        return f"Alles gut. Am niedrigsten ist {w['name']} mit {w['pct']} Prozent, {w['left_text']}."

    # --- Filament ---
    if re.search(r"\bfilament", t):
        if not can("printers", 1):
            return "Das darfst du leider nicht."
        import filament as FL
        su = FL.summary(app.spools(), app.fil_low_limit())
        if not su["count"]:
            return "Im Filament Lager ist noch nichts eingetragen."
        msg = f"{su['count']} Spulen mit zusammen {speak_num(su['grams'] / 1000, 1)} Kilo."
        if su["low"]:
            msg += " Knapp: " + ", ".join(su["low"][:4]) + "."
        return msg

    # --- Szenen ---
    scenes = [r for r in app.cfg.get("automations", []) if r.get("trigger", {}).get("type") == "manual"]
    sc, _ = find_target(t, [(r["name"], r) for r in scenes])
    dev, _ = find_target(t, [(dname(d), d) for d in smart])
    if sc and (not dev or len(norm(sc["name"])) >= len(norm(dname(dev)))) and not has_off:
        if not can("smart", 2) and not can("automations", 1):
            return "Das darfst du leider nicht."
        app.engine.trigger(sc, reason="Siri")
        return f"{sc['name']} gestartet."

    # --- Temperatur abfragen ---
    if re.search(r"\b(wie warm|wie kalt|temperatur|wie viel grad|wieviel grad|grad ist es)\b", t) and number(t) is None:
        if not can("smart", 1):
            return "Das darfst du leider nicht."
        sensors = [d for d in smart if d.get("hkr") or (d.get("temp") is not None and d.get("power_w") is None)]
        d, _ = find_target(t, [(dname(x), x) for x in sensors])
        if d:
            h = d.get("hkr") or {}
            ist = h.get("ist") if isinstance(h.get("ist"), (int, float)) else d.get("temp")
            soll = h.get("soll")
            msg = f"{dname(d)}: {speak_num(ist)} Grad"
            if isinstance(soll, (int, float)):
                msg += f", eingestellt auf {speak_num(soll)}"
            return msg + "."
        parts = []
        for x in sensors[:6]:
            if not x.get("present", True):
                continue
            h = x.get("hkr") or {}
            ist = h.get("ist") if isinstance(h.get("ist"), (int, float)) else x.get("temp")
            if ist is not None:
                parts.append(f"{dname(x)} {speak_num(ist)} Grad")
        return (", ".join(parts) + ".") if parts else "Ich habe gerade keine Temperaturen."

    # --- Fernseher / Sky ---
    mk, mval = media_intent(t, question)
    media = [d for d in smart if d.get("media")]
    if mk and media and not (dev and dev.get("media")):
        on = [d for d in media if d.get("switch")]
        if not dev or mk in ("now",):
            dev = (on or media)[0]
    if dev and dev.get("media") and mk:
        return media_answer(app, dev, dname(dev), mk, mval, media, can)

    # --- Geräte schalten / Heizung stellen ---
    if dev:
        if dev.get("sensor") and dev.get("switch") is None and dev.get("simpleonoff") is None and not dev.get("hkr"):
            if not dev.get("present", True):
                return f"{dname(dev)} ist gerade nicht erreichbar."
            from providers import sensor_text
            txt, _ = sensor_text(dev["sensor"])
            say = {"offen": "ist offen", "zu": "ist zu", "alles ok": "ist in Ordnung", "ruhig": "meldet keine Bewegung",
                   "Bewegung": "erkennt gerade Bewegung", "💧 WASSER!": "meldet Wasser! Bitte sofort nachsehen",
                   "🔥 RAUCH!": "meldet Rauch! Bitte sofort nachsehen", "🚨 Alarm": "meldet Alarm"}.get(txt)
            if say is None and txt.startswith("gedrückt"):
                say = "wurde zuletzt um " + txt.split()[-1] + " Uhr gedrückt"
            return f"{dname(dev)} {say}." if say else f"{dname(dev)} meldet nichts Besonderes."
        if dev.get("level") is not None and not has_off:
            val = number(t)
            if val is not None:
                if not can("smart", 2):
                    return "Das darfst du leider nicht."
                val = max(0, min(100, int(round(val))))
                app.run_action({"type": "level", "ain": dev["ain"], "value": val})
                app.log("Sprache", dname(dev), f"auf {val} % (Siri)")
                return f"{dname(dev)} auf {val} Prozent."
        if dev.get("hkr"):
            val = number(t)
            if val is not None or has_on or has_off:
                if not can("smart", 2):
                    return "Das darfst du leider nicht."
                if not dev.get("present", True):
                    return f"{dname(dev)} ist gerade nicht erreichbar."
                if has_off and val is None:
                    app.jobs.put(("action", lambda c, a=dev["ain"]: c.set_thermostat(a, "aus")))  # Wort, keine Zahl
                    app.log("Sprache", dname(dev), "Heizung aus (Siri)")
                    return f"{dname(dev)} ist aus."
                val = 21.0 if val is None else max(8.0, min(28.0, round(val * 2) / 2))
                app.run_action({"type": "thermostat", "ain": dev["ain"], "value": val})
                app.log("Sprache", dname(dev), f"auf {speak_num(val)} °C (Siri)")
                return f"{dname(dev)} auf {speak_num(val)} Grad."
            h = dev["hkr"]
            ist = h.get("ist") if isinstance(h.get("ist"), (int, float)) else None
            soll = h.get("soll")
            return f"{dname(dev)} hat {speak_num(ist)} Grad" + (
                f", eingestellt auf {speak_num(soll)}." if isinstance(soll, (int, float)) else ".")
        on_now = dev.get("switch") if dev.get("switch") is not None else dev.get("simpleonoff")
        if has_on or has_off:
            if not can("smart", 2):
                return "Das darfst du leider nicht."
            if not dev.get("present", True):
                return f"{dname(dev)} ist gerade nicht erreichbar."
            want_on = has_on and not has_off
            app.run_action({"type": "switch", "ain": dev["ain"], "mode": "on" if want_on else "off"})
            app.log("Sprache", dname(dev), ("an" if want_on else "aus") + " (Siri)")
            return f"{dname(dev)} ist " + ("an." if want_on else "aus.")
        if on_now is None:
            return f"{dname(dev)} kann ich nicht schalten."
        msg = f"{dname(dev)} ist " + ("an" if on_now else "aus")
        if dev.get("power_w") is not None and on_now:
            msg += f" und braucht {speak_num(dev['power_w'], 0)} Watt"
        return msg + "."
    if sc:
        return f"Szenen kann man nur starten. Sag zum Beispiel: Starte {sc['name']}."
    return "Das habe ich nicht verstanden. Sag zum Beispiel: Schreibtisch aus, oder: Wie weit ist der Drucker."
