# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
diagnose.py – Selbsttest: prüft alle Verbindungen und Einstellungen und erklärt, was nicht passt.

collect(app)  – im Haupt-Thread: sammelt eine Momentaufnahme (inkl. entschlüsselter Passwörter)
run(snap)     – im Hintergrund: führt die Prüfungen parallel aus, gibt eine Liste von Ergebnissen zurück

Ergebnis: {"group", "name", "status": ok|warn|bad|info, "detail", "hint"}
"""
import copy
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"
ICON = {OK: "✅", WARN: "⚠️", BAD: "❌", INFO: "ℹ️"}
GROUPS = ("System", "Rechner & Sicherung", "Router", "FRITZ!Box", "3D-Drucker", "Kameras", "Kalender", "Push", "Handy-Zugriff", "Ping-Ziele",
          "Personen")


def R(group, name, status, detail="", hint=""):
    return {"group": group, "name": name, "status": status, "detail": detail, "hint": hint}


def port_open(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except socket.timeout:
        return False, "keine Antwort"
    except ConnectionRefusedError:
        return False, "Verbindung abgelehnt"
    except OSError as e:
        return False, getattr(e, "strerror", None) or str(e)


def fw_version(text):
    """'256.08.02' → (8, 2); '7.57' → (7, 57)"""
    nums = re.findall(r"\d+", text or "")
    if len(nums) >= 3:
        nums = nums[-2:]
    try:
        return int(nums[0]), int(nums[1])
    except (IndexError, ValueError):
        return None


def summary(results):
    c = {k: 0 for k in (OK, WARN, BAD, INFO)}
    for r in results:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def summary_text(results):
    c = summary(results)
    return f"{c[OK]} ✅ · {c[WARN]} ⚠️ · {c[BAD]} ❌"


def as_text(results, title="Hauskern – Diagnose"):
    lines = [title, time.strftime("%d.%m.%Y %H:%M"), summary_text(results), ""]
    for g in GROUPS:
        rows = [r for r in results if r["group"] == g]
        if not rows:
            continue
        lines.append(f"== {g} ==")
        for r in rows:
            lines.append(f"{ICON[r['status']]} {r['name']}: {r['detail']}")
            if r["hint"] and r["status"] in (WARN, BAD):
                lines.append(f"     → {r['hint']}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Momentaufnahme (Haupt-Thread)
# --------------------------------------------------------------------------- #
def collect(app):
    cfg = copy.deepcopy(app.cfg)
    snap = {
        "cfg": cfg, "password": app.password, "data_dir": app.data_dir, "version": app.version,
        "headless": app.headless, "probe": getattr(app, "probe", False),
        "log_path": app.log_path(),
        "db_path": getattr(app.store, "path", ""),
        "boxes": [dict(b, _pw=app.box_password(b)) for b in cfg.get("boxes", [])],
        "printers": [{"name": p.name, "ip": p.ip, "connected": p.connected, "error": p.error,
                      "state": p.raw.get("gcode_state", ""), "serial": s}
                     for s, p in app.printers.items()],
        "cams": [{"name": e["cfg"].get("name", "?"), "host": e["cfg"].get("host", ""),
                  "port": e["cfg"].get("port", 80), "brand": e["cfg"].get("brand", ""),
                  "status": e.get("status", ""), "url": e.get("sub") or e.get("main") or "",
                  "ptz": bool(e.get("onvif") and e["onvif"].has_ptz),
                  "motion": app.motion_status(cid) if hasattr(app, "motion_status") else ""}
                 for cid, e in app.camobjs.items()],
        "calendars": [dict(c, _pw=app.cal_password(c)) for c in cfg.get("calendars", [])],
        "push": app.push_cfg(),
        "web_running": bool(app.web and app.web.running),
        "web_urls": app.web_urls() if app.web else [],
        "hosts": [dict(h) for h in app.data.get("hosts", [])],
        "presence": {pid: st.get("state") for pid, st in app.presence.items()},
        "weather": {"cfg": app.weather_cfg(), "ok": bool(app.weather), "err": app._weather_err}
        if hasattr(app, "weather_cfg") else None,
    }
    return snap


# --------------------------------------------------------------------------- #
# Prüfungen (Hintergrund)
# --------------------------------------------------------------------------- #
def check_system(s):
    out = []
    G = "System"
    v = sys.version_info
    out.append(R(G, "Python", OK if v >= (3, 9) else BAD, f"{v.major}.{v.minor}.{v.micro}",
                 "Bitte Python 3.9 oder neuer installieren."))
    mode = "Probelauf (wie auf dem Heimserver)" if s["probe"] else (
        "Hintergrundbetrieb (Heimserver)" if s["headless"] else "mit Fenster")
    if s["cfg"].get("satellite"):
        mode += " · Heimserver übernimmt Automationen"
    out.append(R(G, "Betriebsart", INFO, f"Hauskern {s['version']} · {mode}"))
    d = s["data_dir"]
    try:
        test = os.path.join(d, ".schreibtest")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        free = shutil.disk_usage(d).free / 1e9
        st = OK if free > 0.5 else (WARN if free > 0.1 else BAD)
        out.append(R(G, "Datenordner", st, f"beschreibbar · {free:.1f} GB frei",
                     "Speicherplatz wird knapp – auf dem Heimserver z. B. alte Aufnahmen löschen oder größere SD-Karte."))
    except OSError as e:
        out.append(R(G, "Datenordner", BAD, f"nicht beschreibbar ({e})",
                     "Rechte des Ordners prüfen – auf dem Heimserver darf Hauskern nicht als root laufen."))
    if s["db_path"] and os.path.exists(s["db_path"]):
        try:
            con = sqlite3.connect(s["db_path"], timeout=5)
            res = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
            size = os.path.getsize(s["db_path"]) / 1e6
            out.append(R(G, "Verlaufs-Datenbank", OK if res == "ok" else BAD, f"{'in Ordnung' if res == 'ok' else res} · "
                         f"{size:.1f} MB", "Datenbank beschädigt – Hauskern beenden, verlauf.db umbenennen und neu starten."))
        except sqlite3.Error as e:
            out.append(R(G, "Verlaufs-Datenbank", WARN, str(e)))
    errs, last = log_errors(s["log_path"])
    out.append(R(G, "Fehlerprotokoll (24 h)", OK if not errs else WARN,
                 "keine Fehler" if not errs else f"{errs} Fehler, zuletzt: {last[:120]}",
                 "Logbuch → „Fehlerprotokoll speichern …“ und die ZIP an Claude schicken."))
    try:
        from camera import check_ffmpeg, find_ffmpeg
        exe = find_ffmpeg()
        if not exe:
            st = WARN if s["cams"] or s["printers"] else INFO
            out.append(R(G, "ffmpeg (Livebilder)", st, "nicht installiert",
                         "Am PC: Kamera-Kachel anklicken → Einrichten. Auf dem Heimserver: sudo apt install ffmpeg"))
        else:
            ok, msg = check_ffmpeg(exe)
            out.append(R(G, "ffmpeg (Livebilder)", OK if ok else BAD, msg.split(" Copyright")[0][:60] if ok else msg,
                         "ffmpeg über die Kamera-Kachel neu einrichten."))
    except Exception as e:  # noqa: BLE001
        out.append(R(G, "ffmpeg (Livebilder)", WARN, str(e)))
    return out


def log_errors(path, hours=24):
    if not path or not os.path.exists(path):
        return 0, ""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
    n, last = 0, ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 400_000))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if len(line) > 20 and line[:19] >= cutoff and re.match(r".{19}\s+(ERROR|CRITICAL)", line):
                    n += 1
                    last = line[19:].split("]", 1)[-1].strip()
    except OSError:
        pass
    return n, last


def _fritz_hint(err):
    e = err.lower()
    if "401" in e or "anmeld" in e or "passwort" in e or "unauthor" in e:
        return "Benutzername oder Passwort stimmen nicht – in den Einstellungen prüfen."
    if "timed out" in e or "keine antwort" in e or "nicht erreichbar" in e or "getaddrinfo" in e or "refused" in e:
        return "FRITZ!Box nicht erreichbar – Adresse prüfen (fritz.box oder 192.168.178.1)."
    return "In der FRITZ!Box: Heimnetz → Netzwerk → Netzwerkeinstellungen → „Zugriff für Anwendungen zulassen“."


def check_router(s):
    """Anderer Router (kein FRITZ!Box): Erreichbarkeit, UPnP, Geräteliste."""
    from router import GenericRouter
    G = "Router"
    cfg = s["cfg"]
    try:
        r = GenericRouter(cfg.get("router_host", ""), cfg.get("router_name", ""))
        r.connect()
        ov = r.overview()
    except Exception as e:  # noqa: BLE001
        return [R(G, "Router", BAD, str(e)[:140], "Ist der PC mit dem Heimnetz verbunden?")]
    out = [R(G, "Router", OK, f"{ov.get('model', 'Router')} ({r.host})")]
    out.append(R(G, "UPnP", OK if ov.get("upnp") else WARN, "aktiv – Internet-Daten verfügbar" if ov.get("upnp")
                 else "aus oder nicht unterstützt",
                 "Im Router „UPnP“ einschalten, dann zeigt Hauskern auch externe IP und Datenrate."))
    out.append(R(G, "Internet", OK if ov.get("online") else BAD, "verbunden" if ov.get("online") else "keine Verbindung",
                 "Router und Anschluss prüfen."))
    try:
        hosts = r.hosts()
        n = sum(1 for h in hosts if h["active"])
        out.append(R(G, "Geräteliste", OK if n > 1 else WARN, f"{n} Geräte gefunden",
                     "Die Liste füllt sich in den ersten Minuten – Geräte im Energiesparmodus tauchen später auf."))
    except Exception as e:  # noqa: BLE001
        out.append(R(G, "Geräteliste", WARN, str(e)[:120], "Die Netzwerkliste des PCs konnte nicht gelesen werden."))
    out.append(R(G, "Nur mit FRITZ!Box", INFO, "WLAN schalten, Gast-WLAN, FRITZ!DECT, Strom-Statistik",
                 "Mit anderem Router nicht verfügbar – alles andere funktioniert."))
    return out


def check_fritz(s):
    from fritz_api import FritzClient
    G = "FRITZ!Box"
    cfg = s["cfg"]
    if cfg.get("router") == "generic":
        return check_router(s)
    if not (cfg.get("user") and s["password"]):
        return [R(G, "Zugangsdaten", BAD, "fehlen", "In den Einstellungen Benutzer und Passwort eintragen.")]
    out = []
    try:
        cl = FritzClient(cfg["host"], cfg["user"], s["password"])
        cl.connect()
        ov = cl.overview()
    except Exception as e:  # noqa: BLE001
        return [R(G, f"Anmeldung ({cfg['host']})", BAD, str(e)[:140], _fritz_hint(str(e)))]
    out.append(R(G, f"Anmeldung ({cfg['host']})", OK, f"{ov.get('model', 'FRITZ!Box')} angemeldet als {cfg['user']}"))
    fw = fw_version(ov.get("firmware", ""))
    if fw:
        old = fw < (7, 59)
        out.append(R(G, "FRITZ!OS", WARN if old else OK, f"Version {fw[0]}.{fw[1]:02d}",
                     "Update empfohlen (u. a. WireGuard-Fehler in 7.58): System → Update."))
    online = ov.get("online")
    out.append(R(G, "Internet", OK if online else BAD, "verbunden" if online else "keine Verbindung",
                 "Kabelmodem/Anschluss prüfen."))
    try:
        hosts = cl.hosts()
        out.append(R(G, "Geräteliste", OK, f"{sum(1 for h in hosts if h['active'])} von {len(hosts)} Geräten online"))
    except Exception as e:  # noqa: BLE001
        out.append(R(G, "Geräteliste", WARN, str(e)[:120], _fritz_hint(str(e))))
    try:
        devs = cl.smarthome()
        off = [d["name"] for d in devs if not d.get("present", True)]
        out.append(R(G, "Smart Home (FRITZ!DECT)", WARN if off else OK,
                     f"{len(devs)} Geräte" + (f" · nicht erreichbar: {', '.join(off[:4])}" if off else ""),
                     "Gerät näher an die Box/den Repeater oder Batterie prüfen."))
    except Exception as e:  # noqa: BLE001
        out.append(R(G, "Smart Home (FRITZ!DECT)", BAD, str(e)[:120],
                     "Der FRITZ!Box-Benutzer braucht das Recht „Smart Home“ (System → FRITZ!Box-Benutzer)."))
    try:
        boxes, _ = cl.mesh()
        out.append(R(G, "Mesh", OK, ", ".join(b.get("model", "?") for b in boxes) or "nur eine Box"))
    except Exception as e:  # noqa: BLE001
        out.append(R(G, "Mesh", INFO, f"nicht verfügbar ({str(e)[:80]})",
                     "Nur für die Zuordnung der Geräte zu den Boxen nötig."))
    return out


def check_box(s, b):
    from fritz_api import FritzClient
    G = "FRITZ!Box"
    name = b.get("name") or b.get("host")
    try:
        cl = FritzClient(b["host"], b.get("user", ""), b["_pw"])
        cl.connect()
        ov = cl.overview()
        fw = fw_version(ov.get("firmware", ""))
        return [R(G, f"Zweite Box: {name}", OK, f"{ov.get('model', '?')}" + (f" · FRITZ!OS {fw[0]}.{fw[1]:02d}" if fw else ""))]
    except Exception as e:  # noqa: BLE001
        return [R(G, f"Zweite Box: {name}", BAD, str(e)[:120], _fritz_hint(str(e)))]


def check_printer(s, p):
    G = "3D-Drucker"
    out = []
    if p["connected"]:
        out.append(R(G, p["name"], OK, f"verbunden ({p['ip']})" + (f" · {p['state']}" if p["state"] else "")))
    else:
        ok, why = port_open(p["ip"], 8883)
        hint = ("Drucker an? Am Drucker „LAN-Modus“/Entwicklermodus und den Zugangscode prüfen."
                if ok else "Drucker aus oder IP geändert? Am besten in der FRITZ!Box eine feste IP vergeben.")
        out.append(R(G, p["name"], BAD, p["error"] or f"nicht verbunden ({why})", hint))
    ok, why = port_open(p["ip"], 322)
    out.append(R(G, f"{p['name']} – Kamera", OK if ok else WARN, "erreichbar" if ok else f"Port 322: {why}",
                 "Am Drucker „LAN Only Liveview“ einschalten."))
    return out


def check_camera(s, c):
    G = "Kameras"
    out = []
    if c["host"]:
        ok, why = port_open(c["host"], c["port"])
        status = c["status"] or ""
        if ok:
            out.append(R(G, f"{c['name']} – Steuerung", OK if not status.startswith("⚠") else WARN,
                         f"{c['host']}:{c['port']} erreichbar" + (" · schwenkbar" if c["ptz"] else "") +
                         (f" · {status}" if status.startswith("⚠") else ""),
                         "Kamera-Konto prüfen (Tapo-App → Kamera → Erweitert → Kamerakonto)."))
        else:
            out.append(R(G, f"{c['name']} – Steuerung", BAD, f"{c['host']}:{c['port']} {why}",
                         "Kamera an und im WLAN? Feste IP in der FRITZ!Box vergeben."))
    if c.get("motion"):
        bad = "⚠" in c["motion"]
        out.append(R(G, f"{c['name']} – Bewegung", WARN if bad else OK, c["motion"].replace("Bewegung: ", ""),
                     "Bei Tapo: Bewegungserkennung in der Tapo-App einschalten – oder in Hauskern „Bildvergleich“ wählen."))
    if not c["url"]:
        out.append(R(G, f"{c['name']} – Bild", BAD, "keine Stream-Adresse", "Kamera bearbeiten und Zugangsdaten prüfen."))
        return out
    from camera import find_ffmpeg, grab_frame
    if not find_ffmpeg():
        out.append(R(G, f"{c['name']} – Bild", INFO, "nicht geprüft (ffmpeg fehlt)"))
        return out
    t = time.time()
    jpg = grab_frame(c["url"], timeout=20, width=320)
    if jpg:
        out.append(R(G, f"{c['name']} – Bild", OK, f"Bild empfangen in {time.time() - t:.1f} s"))
    else:
        out.append(R(G, f"{c['name']} – Bild", BAD, "kein Bild empfangen",
                     "Benutzer/Passwort des Kamerakontos prüfen; bei Tapo darf das Konto nicht das TP-Link-Konto sein."))
    return out


def check_calendar(s, src):
    import datetime as dt
    import calendar_sync
    G = "Kalender"
    name = src.get("name") or "Kalender"
    now = dt.datetime.now()
    try:
        evs = calendar_sync.load_source(src, src["_pw"], now - dt.timedelta(days=1), now + dt.timedelta(days=30))
        return [R(G, name, OK, f"{len(evs)} Termine in den nächsten 30 Tagen")]
    except Exception as e:  # noqa: BLE001
        hint = ("App-spezifisches Passwort unter account.apple.com neu erstellen."
                if src.get("type") != "ics" else "Privatadresse im iCal-Format neu kopieren.")
        return [R(G, name, BAD, str(e)[:140], hint)]


def check_push(s):
    G = "Push"
    p = s["push"]
    if not p.get("enabled"):
        return [R(G, "Push-Nachrichten", INFO, "ausgeschaltet")]
    svc = p.get("service", "ntfy")
    need = {"ntfy": [("ntfy", "topic")], "pushover": [("pushover", "token"), ("pushover", "user")],
            "telegram": [("telegram", "token"), ("telegram", "chat_id")]}.get(svc, [])
    missing = [f for sv, f in need if not str(p.get(sv, {}).get(f, "")).strip()]
    if missing:
        return [R(G, f"Push über {svc}", BAD, "unvollständig: " + ", ".join(missing),
                  "Links auf „✉ Push-Nachrichten“ und die Felder ausfüllen.")]
    out = [R(G, f"Push über {svc}", OK, "eingerichtet", "")]
    if s["probe"]:
        out.append(R(G, "Absender", INFO, "Im Probelauf werden keine Nachrichten verschickt"))
    elif s["cfg"].get("satellite"):
        out.append(R(G, "Absender", INFO, "Dieser PC schickt nichts – das macht der Heimserver"))
    host = {"ntfy": (p.get("ntfy", {}).get("server") or "https://ntfy.sh"), "pushover": "https://api.pushover.net",
            "telegram": "https://api.telegram.org"}[svc]
    h = re.sub(r"^\w+://", "", host).split("/")[0]
    hostname, _, port = h.partition(":")
    ok, why = port_open(hostname, int(port or (443 if host.startswith("https") else 80)), 5)
    out.append(R(G, "Server erreichbar", OK if ok else BAD, hostname + ("" if ok else f" – {why}"),
                 "Internetverbindung prüfen. Echter Test: „Test-Nachricht senden“ im Push-Dialog."))
    return out


def check_weather(s):
    w = s.get("weather")
    if not w or not w["cfg"].get("enabled"):
        return [R("System", "Wetter", INFO, "ausgeschaltet")]
    c = w["cfg"]
    where = c.get("name") or "?"
    mode = "automatisch" if c.get("mode") == "auto" else "selbst gewählt"
    if w["ok"]:
        return [R("System", "Wetter", OK, f"{where} ({mode})")]
    return [R("System", "Wetter", WARN, w["err"] or "noch nicht geladen",
              "Internet prüfen – oder im Wetter-Fenster einen Ort selbst wählen.")]


def check_web(s):
    G = "Handy-Zugriff"
    cfg = s["cfg"]
    if not (cfg.get("web_enabled") or s["headless"]):
        return [R(G, "Handy-Oberfläche", INFO, "ausgeschaltet")]
    if not cfg.get("web_pin"):
        return [R(G, "Handy-Oberfläche", BAD, "keine PIN festgelegt", "In den Einstellungen eine PIN festlegen.")]
    if not s["web_running"]:
        return [R(G, "Handy-Oberfläche", BAD, f"läuft nicht (Port {cfg.get('web_port', 8770)} belegt?)",
                  "Anderen Port in den Einstellungen wählen.")]
    urls = s["web_urls"]
    out = [R(G, "Handy-Oberfläche", OK, " · ".join(urls[:2]) or f"Port {cfg.get('web_port')}")]
    ok, why = port_open("127.0.0.1", cfg.get("web_port", 8770), 2)
    out.append(R(G, "Antwortet", OK if ok else BAD, "ja" if ok else why,
                 "Hauskern neu starten; unter Windows die Firewall für „Private Netzwerke“ erlauben."))
    return out


def check_pings(s):
    from fritz_api import ping
    G = "Ping-Ziele"
    out = []
    targets = s["cfg"].get("ping_targets", [])
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda t: (t, ping(t["host"])), targets))
    for t, (ok, ms) in res:
        out.append(R(G, t.get("name") or t["host"], OK if ok else WARN, (ms or "erreichbar") if ok else "keine Antwort",
                     "Gerät aus? Manche Geräte (z. B. Handys im Ruhezustand) antworten nicht auf Ping."))
    return out


def check_persons(s):
    G = "Personen"
    known = {(h.get("mac") or "").upper(): h for h in s["hosts"]}
    out = []
    for p in s["cfg"].get("persons", []):
        macs = [m.upper() for m in p.get("macs", [])]
        missing = [m for m in macs if m not in known]
        state = {"home": "zu Hause", "away": "unterwegs"}.get(s["presence"].get(p["id"]), "noch unbekannt")
        if not macs:
            out.append(R(G, p["name"], WARN, "kein Gerät zugeordnet", "Unter Zuhause → Personen ein Handy auswählen."))
        elif missing and s["hosts"]:
            out.append(R(G, p["name"], WARN, f"{state} · {len(missing)} Gerät(e) nicht in der FRITZ!Box-Liste",
                         "Am iPhone: Einstellungen → WLAN → (i) → „Private WLAN-Adresse“ auf „Fest“ oder „Aus“ "
                         "stellen, dann das Gerät neu zuordnen."))
        else:
            out.append(R(G, p["name"], OK, state))
    return out


def check_health(s):
    import backup
    import health
    G = "Rechner & Sicherung"
    out = []
    h = health.snapshot(s["data_dir"])
    st = h.get("storage") or {}
    if h.get("model"):
        out.append(R(G, "Raspberry Pi" if h.get("pi") else "Rechner", INFO, h.get("model", "")))
    if h.get("temp") is not None:
        t = h["temp"]
        tw, tb = (health.TEMP_WARN, health.TEMP_BAD) if h.get("pi") else (health.TEMP_WARN_PC, health.TEMP_BAD_PC)
        out.append(R(G, "Temperatur", OK if t < tw else (WARN if t < tb else BAD),
                     f"{t:.0f} °C", "Gehäuse belüften, Kühlkörper/Lüfter prüfen."))
    th = h.get("throttle")
    if th is not None:
        bad = th["under_now"] or th["under_ever"]
        out.append(R(G, "Stromversorgung", BAD if bad else (WARN if th["throttled_ever"] else OK),
                     ", ".join(th["flags"]) if th["flags"] else "ausreichend",
                     "Offizielles Netzteil verwenden – mit USB-SSD erst recht."))
    if os.name != "nt":
        tzn, off = health.timezone()
        if health.tz_suspicious(tzn):
            out.append(R(G, "Zeitzone", WARN, f"{tzn} (UTC{off:+g})",
                         "Automationen, Heizzeiten und Anrufzeiten liegen sonst eine Stunde daneben. Auf dem Heimserver: "
                         "sudo timedatectl set-timezone Europe/Berlin – danach Hauskern neu starten."))
        else:
            out.append(R(G, "Zeitzone", OK, tzn))
    if st.get("total"):
        kind = st.get("kind", "")
        out.append(R(G, "Speicher", OK if (st["free"] / 1e9) >= 3 else (WARN if st["free"] / 1e9 >= 1 else BAD),
                     (f"{kind + ' · ' if kind else ''}{st['free'] / 1e9:.1f} GB frei "
                      f"({st.get('used_pct', 0):.0f} % belegt)").replace(".", ","),
                     "Alte Aufnahmen oder Sicherungen löschen."))
        if kind == "SD-Karte":
            out.append(R(G, "Speicherart", INFO, "SD-Karte", "SD-Karten nutzen sich ab – eine USB-/NVMe-SSD ist "
                                                              "deutlich robuster. Die Sicherung schützt bis dahin."))
    d = h.get("disk")
    probs = {k: (lv, t, x) for k, lv, t, x in health.problems(h) if k in ("ssd", "ssd_ro")}
    if probs:
        for lv, t, x in probs.values():
            out.append(R(G, "SSD-Zustand", BAD if lv == "bad" else WARN, t.replace("💽 ", ""), x))
    elif d:
        out.append(R(G, "SSD-Zustand", OK, health.disk_text(d)))
    elif os.name != "nt" and st.get("kind") not in ("SD-Karte", "virtuelle Festplatte", "System", None):
        out.append(R(G, "SSD-Zustand", INFO, "wird nicht geprüft",
                     "Auf dem Heimserver einmal install.sh ausführen (wie bei der Einrichtung) – dann liest Hauskern "
                     "alle 6 Stunden die Selbstdiagnose der SSD."))
    b = dict({"enabled": True}, **(s["cfg"].get("backup") or {}))
    last = b.get("last")
    if not b.get("enabled"):
        out.append(R(G, "Sicherung", WARN, "automatische Sicherung ist aus", "💾 Sicherung → einschalten."))
    elif not last:
        out.append(R(G, "Sicherung", INFO, "noch keine Sicherung – die erste kommt heute Nacht",
                     "Oder gleich: 💾 Sicherung → Jetzt sichern."))
    else:
        age = (time.time() - last) / 3600
        ok = b.get("last_ok", True)
        out.append(R(G, "Sicherung", (OK if age < 30 else WARN) if ok else BAD,
                     f"zuletzt {backup.fmt_ts(last)}" + ("" if ok else f" – fehlgeschlagen: {b.get('last_err', '')}"),
                     "💾 Sicherung öffnen und „Jetzt sichern“ probieren; Speicherplatz prüfen."))
        if not b.get("extra_dir") and not s["cfg"].get("pi_host") and s["headless"]:
            out.append(R(G, "Zweiter Speicherort", WARN, "Sicherungen liegen nur auf diesem Gerät",
                         "Am PC unter ⋯ Mehr → „Heimserver“ verbinden (der PC holt sich dann täglich eine Kopie) oder "
                         "einen zusätzlichen Ordner (USB-Stick/NAS) eintragen."))
    return out


def run(snap, progress=None):
    """Führt alle Prüfungen aus. progress(text) wird (aus Threads!) mit Zwischenständen aufgerufen."""
    jobs = [("System", lambda: check_system(snap)), ("Rechner", lambda: check_health(snap)),
            ("FRITZ!Box", lambda: check_fritz(snap))]
    jobs += [(f"Box {b.get('host')}", lambda b=b: check_box(snap, b)) for b in snap["boxes"]]
    jobs += [(f"Drucker {p['name']}", lambda p=p: check_printer(snap, p)) for p in snap["printers"]]
    jobs += [(f"Kamera {c['name']}", lambda c=c: check_camera(snap, c)) for c in snap["cams"]]
    jobs += [(f"Kalender {c.get('name')}", lambda c=c: check_calendar(snap, c)) for c in snap["calendars"]]
    jobs += [("Wetter", lambda: check_weather(snap)), ("Push", lambda: check_push(snap)), ("Handy", lambda: check_web(snap)),
             ("Ping", lambda: check_pings(snap)), ("Personen", lambda: check_persons(snap))]
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [(name, ex.submit(fn)) for name, fn in jobs]
        for name, f in futs:
            try:
                results += f.result(timeout=90)
            except Exception as e:  # noqa: BLE001
                results.append(R("System", f"Prüfung „{name}“", WARN, f"abgebrochen: {type(e).__name__}: {e}"))
            done += 1
            if progress:
                progress(f"{done} / {len(jobs)} geprüft …")
    order = {g: i for i, g in enumerate(GROUPS)}
    results.sort(key=lambda r: order.get(r["group"], 99))  # stabil: Reihenfolge innerhalb der Gruppe bleibt
    return results
