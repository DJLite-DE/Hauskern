# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
plugins.py – Erweiterungen für Hauskern.

Eine Erweiterung ist eine .py-Datei im Ordner „erweiterungen“ (im Hauskern-Datenordner).
Sie bringt eine Funktion setup(hk) mit und meldet darüber an, was sie kann:

    NAME = "Sonnenzeiten"
    DESCRIPTION = "Zeigt Sonnenaufgang und -untergang"
    VERSION = "1.0"

    def setup(hk):
        hk.tile("sonne", "🌅 Sonne", "07:58 – 16:32")
        hk.every(600, lambda: ...)
        hk.on("print_finished", lambda ev: ...)
        hk.siri(["sonnenuntergang"], lambda text: "Um 16:32 Uhr.")

Sicherheit: Eine Erweiterung ist ein kleines Programm und darf alles, was Hauskern darf. Deshalb läuft
eine neue oder geänderte Datei erst, wenn sie freigegeben wurde (Prüfsumme wird gespeichert).
Ändert sich die Datei danach, ist sie wieder aus, bis sie erneut freigegeben wird.
"""
import concurrent.futures as cf
import hashlib
import importlib.util
import logging
import os
import re
import shutil
import threading
import time
import traceback

log = logging.getLogger("fm")

EVENTS = {
    "person_home": "Person kommt nach Hause (ev: name)",
    "person_away": "Person geht weg (ev: name)",
    "print_started": "Druck startet (ev: printer, file)",
    "print_finished": "Druck fertig (ev: printer, file)",
    "print_failed": "Druck fehlgeschlagen/abgebrochen (ev: printer, file)",
    "print_problem": "KI im Drucker meldet möglichen Fehldruck (ev: printer, paused)",
    "internet_down": "Internet weg",
    "internet_up": "Internet wieder da",
    "switch_changed": "Steckdose an/aus (ev: name, on)",
    "new_device": "Neues Gerät im Netzwerk (ev: name, ip, mac)",
    "call_missed": "Verpasster Anruf (ev: name, number)",
}
MIN_EVERY = 10          # Sekunden – kürzere Takte bremsen nur
CALL_TIMEOUT = 15       # Sekunden für Siri/Antippen


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_meta(path):
    """NAME/DESCRIPTION/VERSION lesen, ohne die Datei auszuführen."""
    meta = {"name": os.path.splitext(os.path.basename(path))[0].replace("_", " ").title(), "description": "",
            "version": ""}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(6000)
    except OSError:
        return meta
    for key, field in (("NAME", "name"), ("DESCRIPTION", "description"), ("VERSION", "version")):
        m = re.search(rf'^{key}\s*=\s*(["\'])(.*?)\1\s*$', head, re.M)
        if m:
            meta[field] = m.group(2)
    return meta


def norm(t):
    t = (t or "").lower().replace("ß", "ss")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


class HK:
    """Das bekommt eine Erweiterung in setup(hk) – ihre Schnittstelle zu Hauskern."""

    def __init__(self, mgr, plug):
        self._m, self._p = mgr, plug
        self.name = plug["name"]
        self.folder = mgr.folder

    # --- Ausgabe ---
    def log(self, text):
        self._m.post("log", (self.name, str(text)[:300]))

    def notify(self, title, text=""):
        self._m.post("notify", (str(title)[:120], str(text)[:400]))

    def tile(self, key, title, value="", sub="", icon="🧩", on_tap=None, color=""):
        """Kachel anlegen oder aktualisieren (PC + Handy, Bereich „Erweiterungen“)."""
        k = f"plug:{self._p['file']}:{key}"
        self._m.tiles[k] = {"key": k, "title": str(title)[:40], "value": str(value)[:60], "sub": str(sub)[:120],
                            "icon": str(icon)[:4], "tap": bool(on_tap), "color": str(color)[:20],
                            "plugin": self._p["file"]}
        if on_tap:
            self._m.taps[k] = (self._p, on_tap)
        else:
            self._m.taps.pop(k, None)
        self._m.post("tiles", None)

    def remove_tile(self, key):
        k = f"plug:{self._p['file']}:{key}"
        self._m.tiles.pop(k, None)
        self._m.taps.pop(k, None)
        self._m.post("tiles", None)

    # --- Auslöser ---
    def every(self, seconds, fn, run_now=True):
        sec = max(MIN_EVERY, float(seconds))
        self._m.timers.append({"plug": self._p, "every": sec, "fn": fn, "due": 0 if run_now else time.time() + sec})

    def on(self, event, fn):
        if event not in EVENTS:
            raise ValueError(f"Unbekanntes Ereignis „{event}“ – möglich: {', '.join(EVENTS)}")
        self._m.handlers.setdefault(event, []).append((self._p, fn))

    def siri(self, words, fn):
        """Siri-Befehl: Alle Wörter müssen im Satz vorkommen. fn(text) → Antwort (Text)."""
        ws = [norm(w) for w in ([words] if isinstance(words, str) else words) if norm(w)]
        if ws:
            self._m.siri_cmds.append((self._p, ws, fn))

    # --- Lesen ---
    def devices(self):
        out = []
        for d in self._m.app.data.get("smart") or []:
            on = d.get("switch") if d.get("switch") is not None else d.get("simpleonoff")
            h = d.get("hkr") or {}
            out.append({"name": d["name"], "ain": d["ain"], "on": on, "power_w": d.get("power_w"),
                        "temp": h.get("ist") if isinstance(h.get("ist"), (int, float)) else d.get("temp"),
                        "target": h.get("soll") if isinstance(h.get("soll"), (int, float)) else None,
                        "battery": d.get("battery"), "present": d.get("present", True)})
        return out

    def persons(self):
        snap = self._m.app.presence_snapshot()
        return [{"name": p["name"], "home": {"home": True, "away": False}.get(snap.get(p["id"]))}
                for p in self._m.app.cfg.get("persons", []) if p.get("kind") != "item"]

    def printers(self):
        out = []
        for p in getattr(self._m.app, "printers", {}).values():
            s = p.summary()
            out.append({"name": p.name, "state": s.get("state"), "percent": s.get("percent"),
                        "remaining_min": s.get("remaining_min"), "file": s.get("file"), "connected": s.get("connected")})
        return out

    def prints(self, days=30):
        """Drucke der letzten Tage: [{printer, file, start, end, result, grams}]"""
        now = time.time()
        rows = self._m.app.store.prints_between(now - days * 86400, now)
        return [{"printer": r[0], "file": r[1], "start": r[2], "end": r[3], "result": r[6], "grams": r[8]}
                for r in rows]

    def location(self):
        """(Breite, Länge) aus den Wetter-Einstellungen – oder None."""
        w = self._m.app.cfg.get("weather") or {}
        try:
            return float(w["lat"]), float(w["lon"])
        except (KeyError, TypeError, ValueError):
            return None

    # --- Schalten (gilt als Aktion der Erweiterung, steht im Logbuch) ---
    def switch(self, name, on):
        d = self._find(name)
        self._m.app.run_action({"type": "switch", "ain": d["ain"], "mode": "on" if on else "off"})
        self.log(f"{d['name']} {'an' if on else 'aus'}")

    def heat(self, name, temp):
        d = self._find(name)
        t = max(8.0, min(28.0, round(float(temp) * 2) / 2))
        self._m.app.run_action({"type": "thermostat", "ain": d["ain"], "value": t})
        self.log(f"{d['name']} auf {t:g} °C")

    def _find(self, name):
        n = norm(name)
        for d in self._m.app.data.get("smart") or []:
            if norm(d["name"]) == n or d["ain"] == name:
                return d
        raise ValueError(f"Gerät „{name}“ nicht gefunden")

    # --- Gedächtnis (bleibt über Neustarts) ---
    def get(self, key, default=None):
        return self._m.data(self._p["file"]).get(key, default)

    def set(self, key, value):
        self._m.data(self._p["file"])[key] = value
        self._m.post("save", None)


class Manager:
    """Lädt freigegebene Erweiterungen und führt ihre Aufgaben in eigenen Threads aus (nie im Fenster-Thread)."""

    def __init__(self, app, folder, examples_dir, post):
        self.app, self.folder, self.examples_dir = app, folder, examples_dir
        self.post = post  # post(kind, payload) → im Hauptthread erledigen
        self.plugs = {}   # file → {file, path, name, …, status, error, module}
        self.tiles, self.taps, self.handlers, self.timers, self.siri_cmds = {}, {}, {}, [], []
        self.pool = cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="Erweiterung")
        self._stop = threading.Event()
        self._last = {}
        self.active = False
        os.makedirs(folder, exist_ok=True)

    # ---------- Einstellungen ----------
    def cfg(self):
        return self.app.cfg.setdefault("plugins", {})

    def data(self, file):
        return self.cfg().setdefault(file, {}).setdefault("data", {})

    # ---------- Übersicht ----------
    def scan(self):
        """Alle Dateien im Ordner mit Status: aktiv, aus, neu, geändert, Fehler."""
        out = []
        try:
            names = sorted(n for n in os.listdir(self.folder) if n.endswith(".py") and not n.startswith("_"))
        except OSError:
            names = []
        for n in names:
            path = os.path.join(self.folder, n)
            meta = read_meta(path)
            c = self.cfg().get(n, {})
            try:
                h = file_hash(path)
            except OSError:
                continue
            p = self.plugs.get(n)
            if p and p.get("status") == "error":
                status = "error"
            elif not c.get("hash"):
                status = "new"
            elif c["hash"] != h:
                status = "changed"
            elif c.get("enabled"):
                status = "on" if p and p.get("status") == "on" else ("off" if not self.active else "error")
            else:
                status = "off"
            out.append(dict(meta, file=n, path=path, hash=h, status=status,
                            error=(p or {}).get("error", ""), approved=c.get("hash") == h))
        return out

    # ---------- Laden / Entladen ----------
    def start(self):
        """Beim Start: alle freigegebenen, unveränderten Erweiterungen laden."""
        self.active = True
        for info in self.scan():
            if info["approved"] and self.cfg().get(info["file"], {}).get("enabled"):
                self._load(info)
        threading.Thread(target=self._loop, daemon=True, name="Erweiterungen-Takt").start()

    def stop(self):
        self._stop.set()
        self.active = False
        for f in list(self.plugs):
            self._unload(f)

    def enable(self, file):
        """Freigeben (aktuelle Prüfsumme merken) und einschalten."""
        info = next((i for i in self.scan() if i["file"] == file), None)
        if not info:
            raise ValueError("Erweiterung nicht gefunden")
        c = self.cfg().setdefault(file, {})
        c.update(hash=info["hash"], enabled=True, approved_at=int(time.time()))
        self.post("save", None)
        self._unload(file)
        if self.active:
            self._load(info)
        p = self.plugs.get(file) or {}
        return p.get("status") == "on" or not self.active, p.get("error", "")

    def disable(self, file):
        c = self.cfg().setdefault(file, {})
        c["enabled"] = False
        self.post("save", None)
        self._unload(file)

    def reload(self, file):
        info = next((i for i in self.scan() if i["file"] == file), None)
        self._unload(file)
        if info and info["approved"] and self.cfg().get(file, {}).get("enabled") and self.active:
            self._load(info)

    def _load(self, info):
        p = {"file": info["file"], "path": info["path"], "name": info["name"], "status": "loading", "error": ""}
        self.plugs[info["file"]] = p
        try:
            spec = importlib.util.spec_from_file_location("hauskern_ext_" + re.sub(r"\W", "_", info["file"][:-3]),
                                                          info["path"])
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not callable(getattr(mod, "setup", None)):
                raise ValueError("Die Datei hat keine Funktion setup(hk)")
            p["module"] = mod
            mod.setup(HK(self, p))
            p["status"] = "on"
            self.post("log", (p["name"], "Erweiterung gestartet"))
        except Exception as e:  # noqa: BLE001 – eine kaputte Erweiterung darf Hauskern nicht stören
            p["status"], p["error"] = "error", f"{type(e).__name__}: {e}"
            log.warning("Erweiterung %s: %s\n%s", info["file"], e, traceback.format_exc())
            self._remove_registrations(info["file"])
            self.post("log", (p["name"], f"Erweiterung konnte nicht starten: {p['error']}"))

    def _unload(self, file):
        self._remove_registrations(file)
        p = self.plugs.pop(file, None)
        if p and p.get("module") is not None and callable(getattr(p["module"], "teardown", None)):
            try:
                p["module"].teardown()
            except Exception:  # noqa: BLE001
                log.exception("Erweiterung %s: teardown", file)
        self.post("tiles", None)

    def _remove_registrations(self, file):
        for k in [k for k, t in self.tiles.items() if t["plugin"] == file]:
            self.tiles.pop(k, None)
            self.taps.pop(k, None)
        self.timers = [t for t in self.timers if t["plug"]["file"] != file]
        self.siri_cmds = [s for s in self.siri_cmds if s[0]["file"] != file]
        for ev in self.handlers:
            self.handlers[ev] = [h for h in self.handlers[ev] if h[0]["file"] != file]

    # ---------- Ausführen ----------
    def _run(self, plug, fn, *args):
        def job():
            if plug.get("status") != "on":
                return None
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                log.warning("Erweiterung %s: %s\n%s", plug["file"], msg, traceback.format_exc())
                plug["error"] = msg
                self.post("log", (plug["name"], f"Fehler: {msg}"))
                return None
        return self.pool.submit(job)

    def _loop(self):
        while not self._stop.wait(1.0):
            now = time.time()
            for t in list(self.timers):
                if now >= t["due"]:
                    t["due"] = now + t["every"]
                    self._run(t["plug"], t["fn"])

    def emit(self, event, **data):
        for plug, fn in list(self.handlers.get(event, [])):
            self._run(plug, fn, dict(data, event=event))

    def siri(self, text):
        """Passt ein Siri-Befehl einer Erweiterung? → Antwort oder None."""
        t = set(norm(text).split())
        best = None
        for plug, words, fn in self.siri_cmds:
            if all(set(w.split()) <= t for w in words):
                n = sum(len(w) for w in words)
                if not best or n > best[0]:
                    best = (n, plug, fn)
        if not best:
            return None
        try:
            res = self._run(best[1], best[2], text).result(timeout=CALL_TIMEOUT)
        except cf.TimeoutError:
            return f"{best[1]['name']} antwortet gerade nicht."
        return str(res) if res not in (None, "") else "Erledigt."

    def tap(self, key):
        hit = self.taps.get(key)
        if not hit:
            return False, "Diese Kachel kann man nicht antippen"
        try:
            res = self._run(hit[0], hit[1]).result(timeout=CALL_TIMEOUT)
        except cf.TimeoutError:
            return False, "Die Erweiterung antwortet nicht"
        return True, str(res) if res not in (None, "") else "Erledigt"

    # ---------- Ereignisse aus dem Zustand ableiten ----------
    def on_snapshot(self, snap, person_names):
        """Vergleicht mit dem letzten Stand und meldet Änderungen als Ereignisse."""
        last, self._last = self._last, snap
        if not last or not self.handlers:
            return
        for pid, st in (snap.get("presence") or {}).items():
            old = (last.get("presence") or {}).get(pid)
            if old and st != old and st in ("home", "away"):
                self.emit("person_home" if st == "home" else "person_away", name=person_names.get(pid, pid))
        if last.get("internet") and snap.get("internet") and snap["internet"] != last["internet"]:
            self.emit("internet_up" if snap["internet"] == "up" else "internet_down")
        for serial, st in (snap.get("printer") or {}).items():
            old = (last.get("printer") or {}).get(serial)
            if old == st or old is None:
                continue
            pr = getattr(self.app, "printers", {}).get(serial)
            name = pr.name if pr else serial
            file = (pr.summary().get("file") if pr else "") or ""
            if st == "RUNNING" and old not in ("RUNNING", "PAUSE"):
                self.emit("print_started", printer=name, file=file)
            elif st == "FINISH":
                self.emit("print_finished", printer=name, file=file)
            elif st == "FAILED" or (st == "IDLE" and old in ("RUNNING", "PAUSE")):
                self.emit("print_failed", printer=name, file=file)
        names = {d["ain"]: d["name"] for d in self.app.data.get("smart") or []}
        for ain, on in (snap.get("switch") or {}).items():
            old = (last.get("switch") or {}).get(ain)
            if old is not None and bool(on) != bool(old):
                self.emit("switch_changed", name=names.get(ain, ain), on=bool(on))

    # ---------- Beispiele ----------
    def add_examples(self):
        added = []
        try:
            for n in sorted(os.listdir(self.examples_dir)):
                if n.endswith(".py") and not os.path.exists(os.path.join(self.folder, n)):
                    shutil.copy2(os.path.join(self.examples_dir, n), os.path.join(self.folder, n))
                    added.append(n)
        except OSError as e:
            raise ValueError(f"Beispiele nicht gefunden: {e}") from None
        return added


STATUS_TEXT = {"on": "✅ aktiv", "off": "⏸ aus", "new": "🆕 neu – noch nicht freigegeben",
               "changed": "⚠ geändert – bitte neu freigeben", "error": "❌ Fehler", "loading": "lädt …"}
