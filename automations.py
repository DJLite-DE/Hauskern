# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
automations.py – Einfache Regel-Engine: Auslöser → (Bedingung) → Aktionen.

Eine Automation (dict, wird in der Konfiguration gespeichert):
{
  "id": "a1", "name": "Drucker aus nach Druck", "enabled": true,
  "trigger": {"type": "printer", "serial": "...", "to": "FINISH"},
  "condition": {"from": "22:00", "to": "06:00", "days": [0,1,2,3,4,5,6]},   # optional
  "actions": [{"type": "delay", "minutes": 10}, {"type": "switch", "ain": "...", "mode": "off"}]
}

Auslöser:   manual (Szene) · time · switch · power · temp · printer · ping · internet
Aktionen:   switch · thermostat · wlan · notify · printer_light · camera_preset · scene · delay

Der aktuelle Zustand kommt als dict von der App (snapshot), die Engine erkennt Änderungen selbst.
"""
import threading
import time
import uuid

TRIGGER_TYPES = {
    "manual": "Manuell (Szene / Knopf)",
    "time": "Uhrzeit",
    "switch": "Steckdose/Schalter wird an/aus",
    "power": "Leistung über/unter Wert",
    "temp": "Temperatur über/unter Wert",
    "printer": "3D-Drucker-Status",
    "ping": "Gerät (Ping) erreichbar/weg",
    "internet": "Internet weg/wieder da",
    "calendar": "Kalendertermin",
    "presence": "Anwesenheit (kommt/geht)",
    "motion": "Kamera erkennt Bewegung",
    "weather": "Wetter (Regen beginnt/hört auf)",
    "sensor": "Melder / Taster (Tür, Fenster, Wasser, Rauch, Bewegung, Tastendruck)",
}
SENSOR_EVENTS = [("open", "wird geöffnet"), ("closed", "wird geschlossen"), ("motion", "erkennt Bewegung"),
                 ("motion_end", "Bewegung vorbei"), ("press", "Taster wird gedrückt"), ("water", "meldet Wasser"),
                 ("smoke", "meldet Rauch"), ("alarm", "meldet Alarm")]
ACTION_TYPES = {
    "switch": "Steckdose schalten",
    "level": "Lampe dimmen",
    "media": "Fernseher / Sky bedienen",
    "thermostat": "Heizung einstellen",
    "wlan": "WLAN an/aus",
    "notify": "Benachrichtigung zeigen",
    "printer_light": "Drucker-Licht",
    "camera_preset": "Kamera zu Position",
    "record": "Kamera-Aufnahme (Video)",
    "wol": "Gerät aufwecken (Wake-on-LAN)",
    "scene": "Andere Szene ausführen",
    "delay": "Warten",
}
PRINTER_STATES = {"RUNNING": "Druckt", "FINISH": "Fertig", "FAILED": "Fehlgeschlagen", "PAUSE": "Pausiert",
                  "IDLE": "Bereit"}
DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def new_automation(name="Neue Automation", trigger_type="manual"):
    return {"id": uuid.uuid4().hex[:8], "name": name, "enabled": True,
            "trigger": {"type": trigger_type}, "condition": {}, "actions": []}


def _hm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def presence_ok(pc, snap):
    """pc = {"who": "_any" | Personen-ID, "state": "home" | "away"}"""
    if not pc or not pc.get("who"):
        return True
    cur = (snap or {}).get("presence", {}).get(pc["who"])
    return cur == pc.get("state", "away")


def device_ok(dc, snap):
    """dc = {"ain": Gerät, "state": "on" | "off"} – z. B. „nur wenn Sky aus ist“."""
    if not dc or not dc.get("ain"):
        return True
    cur = (snap or {}).get("switch", {}).get(dc["ain"])
    return bool(cur) == (dc.get("state", "off") == "on")


def condition_ok(cond, now=None, snap=None):
    if not cond:
        return True
    if not presence_ok(cond.get("presence"), snap):
        return False
    if not device_ok(cond.get("device"), snap):
        return False
    lt = time.localtime(now or time.time())
    days = cond.get("days")
    if days is not None and len(days) and lt.tm_wday not in days:
        return False
    a, b = _hm(cond.get("from")), _hm(cond.get("to"))
    if a is not None and b is not None and a != b:
        cur = lt.tm_hour * 60 + lt.tm_min
        inside = a <= cur < b if a < b else (cur >= a or cur < b)  # über Mitternacht
        if not inside:
            return False
    return True


class Engine:
    """api: Objekt mit Methoden run_action(action) -> str (Beschreibung) und log_automation(text)."""

    def __init__(self, api, get_rules):
        self.api = api
        self.get_rules = get_rules
        self.prev = None
        self._since = {}        # (rule_id) -> Zeitpunkt seit dem Leistungs-/Temperaturbedingung gilt
        self._fired_cond = set()  # Regeln, deren Schwellwert-Bedingung bereits ausgelöst hat
        self._last_minute = {}
        self._stop = threading.Event()
        self.running = {}       # rule_id -> Thread
        self._last_fire = {}    # rule_id -> Zeitpunkt (Pause bei Bewegung)

    def stop(self):
        self._stop.set()

    # ---------------- Auslöser prüfen ----------------
    def update(self, snap, now=None):
        """snap = {"switch": {ain: bool}, "power": {ain: W}, "temp": {ain: °C},
                   "printer": {serial: state}, "ping": {host: "up"/"down"}, "internet": "up"/"down"/None}"""
        now = now or time.time()
        prev = self.prev
        self.prev = {k: (dict(v) if isinstance(v, dict) else v) for k, v in snap.items()}
        for rule in self.get_rules():
            if not rule.get("enabled", True):
                continue
            t = rule.get("trigger", {})
            typ = t.get("type")
            fire = False
            if typ in ("power", "temp"):
                src = snap.get("power" if typ == "power" else "temp", {})
                val = src.get(t.get("ain"))
                if val is None:
                    continue
                limit = float(t.get("value", 0))
                cond = val < limit if t.get("op", "<") == "<" else val > limit
                rid = rule["id"]
                if cond:
                    since = self._since.setdefault(rid, now)
                    if rid not in self._fired_cond and now - since >= float(t.get("minutes", 0)) * 60:
                        self._fired_cond.add(rid)
                        fire = True
                else:
                    self._since.pop(rid, None)
                    self._fired_cond.discard(rid)
            elif prev is not None:
                if typ == "switch":
                    a, b = prev.get("switch", {}).get(t.get("ain")), snap.get("switch", {}).get(t.get("ain"))
                    fire = a is not None and b is not None and a != b and b == (t.get("to", "on") == "on")
                elif typ == "printer":
                    a, b = prev.get("printer", {}).get(t.get("serial")), snap.get("printer", {}).get(t.get("serial"))
                    fire = bool(a) and a != b and b == t.get("to")
                elif typ == "ping":
                    a, b = prev.get("ping", {}).get(t.get("host")), snap.get("ping", {}).get(t.get("host"))
                    fire = a is not None and a != b and b == t.get("to", "down")
                elif typ == "internet":
                    a, b = prev.get("internet"), snap.get("internet")
                    fire = a is not None and b is not None and a != b and b == t.get("to", "down")
                elif typ == "presence":
                    who = t.get("person", "_any")
                    a, b = prev.get("presence", {}).get(who), snap.get("presence", {}).get(who)
                    fire = a is not None and b is not None and a != b and b == t.get("to", "home")
                elif typ == "sensor":
                    to = t.get("to", "open")
                    a = (prev.get("sensor") or {}).get(t.get("ain")) or {}
                    b = (snap.get("sensor") or {}).get(t.get("ain")) or {}
                    if to in ("open", "closed"):
                        fire = a.get("contact") is not None and a.get("contact") != b.get("contact") and \
                            b.get("contact") == to
                    elif to == "press":
                        fire = bool(b.get("button")) and bool(a) and a.get("button") != b.get("button")
                    elif to == "motion_end":
                        fire = a.get("motion") is True and b.get("motion") is False
                    else:
                        fire = bool(b.get(to)) and not a.get(to) and bool(a or to != "motion")
                elif typ == "weather":
                    what = t.get("to", "rain_start")
                    field = "rain_soon" if what == "rain_soon" else "raining"
                    a, b = prev.get("weather", {}).get(field), snap.get("weather", {}).get(field)
                    want = what != "rain_stop"
                    fire = a is not None and b is not None and a != b and b == want
                elif typ == "motion":
                    what = t.get("what") or "any"
                    if what == "any":
                        a, b = prev.get("motion", {}).get(t.get("cam")), snap.get("motion", {}).get(t.get("cam"))
                        fire = b is True and a is not True
                    else:  # Kamera-Erkennung: nur wenn das Gewünschte neu erkannt wurde
                        import vision
                        a, b = prev.get("seen", {}).get(t.get("cam")), snap.get("seen", {}).get(t.get("cam"))
                        fire = bool(b) and (not a or a[0] != b[0]) and vision.matches(what, set(b[1]))
                    if fire and now - self._last_fire.get(rule["id"], 0) < float(t.get("cooldown", 5) or 0) * 60:
                        fire = False  # gerade erst gemeldet – Pause, damit nicht jede Bewegung eine Nachricht auslöst
            if fire and condition_ok(rule.get("condition"), now, snap):
                self._last_fire[rule["id"]] = now
                self.trigger(rule, reason=TRIGGER_TYPES.get(typ, typ))

    def calendar_check(self, events, now=None):
        """Prüft Kalender-Auslöser. events: [{title, start, end, uid}] mit naiven Ortszeiten."""
        import datetime as _dt
        now = now or time.time()
        last = getattr(self, "_cal_last", None)
        self._cal_last = now
        if last is None:
            return  # beim Start nichts nachholen
        fired = self.__dict__.setdefault("_cal_fired", set())
        for rule in self.get_rules():
            t = rule.get("trigger", {})
            if not rule.get("enabled", True) or t.get("type") != "calendar":
                continue
            match = (t.get("match") or "").strip().lower()
            off = float(t.get("offset", 0) or 0) * 60
            for e in events:
                if match and match not in e["title"].lower():
                    continue
                at = (e["start"] if t.get("when", "start") == "start" else e["end"]).timestamp() - off
                key = (rule["id"], e.get("uid"), e["start"].isoformat(), t.get("when"))
                if last < at <= now and key not in fired:
                    fired.add(key)
                    if condition_ok(rule.get("condition"), now, self.prev):
                        self.trigger(rule, reason=f"Termin „{e['title']}“")

    def tick(self, now=None):
        """Jede ~20 s aufrufen: prüft Uhrzeit-Auslöser."""
        now = now or time.time()
        lt = time.localtime(now)
        minute_key = (lt.tm_yday, lt.tm_hour, lt.tm_min)
        for rule in self.get_rules():
            t = rule.get("trigger", {})
            if not rule.get("enabled", True) or t.get("type") != "time":
                continue
            at = _hm(t.get("at"))
            days = t.get("days")
            if at is None or (days and lt.tm_wday not in days):
                continue
            if lt.tm_hour * 60 + lt.tm_min == at and self._last_minute.get(rule["id"]) != minute_key:
                self._last_minute[rule["id"]] = minute_key
                if condition_ok(rule.get("condition"), now, self.prev):
                    self.trigger(rule, reason="Uhrzeit")

    # ---------------- Ausführen ----------------
    def trigger(self, rule, reason="manuell", depth=0):
        if depth > 3:
            return
        th = self.running.get(rule["id"])
        if th and th.is_alive():
            return  # läuft schon
        th = threading.Thread(target=self._run, args=(rule, reason, depth), daemon=True)
        self.running[rule["id"]] = th
        th.start()

    def _run(self, rule, reason, depth):
        done = []
        for act in rule.get("actions", []):
            if self._stop.is_set():
                return
            try:
                if act.get("type") == "delay":
                    if self._stop.wait(float(act.get("minutes", 0)) * 60):
                        return
                    continue
                if act.get("type") == "scene":
                    target = next((r for r in self.get_rules() if r["id"] == act.get("id")), None)
                    if target and target["id"] != rule["id"]:
                        self.trigger(target, reason=f"aus „{rule['name']}“", depth=depth + 1)
                        done.append(f"Szene „{target['name']}“")
                    continue
                desc = self.api.run_action(act)
                if desc:
                    done.append(desc)
            except Exception as e:
                done.append(f"Fehler: {e}")
        self.api.log_automation(f"„{rule['name']}“ ausgeführt ({reason})" + (": " + ", ".join(done) if done else ""))
