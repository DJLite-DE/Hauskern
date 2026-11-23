# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
providers.py – Geräte anderer Hersteller (Shelly, Philips Hue, Tasmota, ESPHome, Zigbee) einheitlich einbinden.

Grundidee: Jedes Gerät – egal welcher Marke – wird in dasselbe Format gebracht wie eine FRITZ!DECT-Steckdose
(ain, name, switch, power_w, temp, hkr …). Dadurch funktioniert alles, was Hauskern schon kann (Kacheln, Abläufe,
Urlaubsmodus, Waschmaschine & Co., Stromstatistik, Heizungs-Check, Siri) automatisch auch mit anderen Marken.

    new_dev(ain, name, kind, vendor, **werte)  → Gerät im gemeinsamen Format
    Hub(fritz, manager)                         → schaltet: FRITZ-Geräte über die Box, alle anderen über ihren Anbieter
    Manager(cfg_list, on_result)                → fragt alle Anbieter regelmäßig ab (eigener Thread)

Geräte-Kennung (ain) fremder Geräte: „x:<Anbieter-ID>:<Kanal>“ – so findet der Hub den richtigen Anbieter.

Zusätzliche Felder (nur wenn vorhanden):
    level      Helligkeit 0–100 (dimmbare Lampen)
    sensor     {"contact": "open"/"closed", "water": bool, "smoke": bool, "motion": bool, "alarm": bool,
                "button": (Zeitpunkt, Aktion)}
    vendor     „Shelly“, „Philips Hue“ …
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TYPES = {  # Typ → (Name, Beschreibung für die Einrichtung)
    "shelly": ("Shelly", "Steckdosen, Schalter, Dimmer, Tür-/Fensterkontakte, Wasser- und Rauchmelder (WLAN)"),
    "hue": ("Philips Hue", "Lampen mit Helligkeit, Bewegungsmelder, Temperatur und Taster (über die Hue Bridge)"),
    "tasmota": ("Tasmota", "Umgeflashte WLAN-Steckdosen und -Schalter (z. B. Sonoff, Gosund, Nous)"),
    "esphome": ("ESPHome", "Selbstgebaute oder fertige ESPHome-Geräte (Webserver muss aktiv sein)"),
    "z2m": ("Zigbee (Zigbee2MQTT)", "Tausende Zigbee-Geräte: IKEA, Aqara, Sonoff, Hue, Tuya … über einen Zigbee-Stick"),
    "skyq": ("Sky Q", "Sky-Q-Box: an/aus, Pause, Sender, was gerade läuft"),
    "vestel": ("Fernseher (Vestel)", "Fernseher von Hanseatic, Telefunken, Toshiba, JVC, Hitachi, Finlux …"),
}
SENSOR_KINDS = ("Kontakt", "Wassermelder", "Rauchmelder", "Bewegungsmelder", "Taster", "Sensor")
UA = {"User-Agent": "Hauskern"}
TIMEOUT = 4


class ProviderError(Exception):
    pass


def new_dev(ain, name, kind, vendor, **kw):
    d = {"ain": ain, "name": name or ain, "product": kw.pop("product", vendor), "is_group": False, "present": True,
         "switch": None, "simpleonoff": None, "power_w": None, "energy_kwh": None, "temp": None, "humidity": None,
         "hkr": None, "battery": None, "batterylow": False, "kind": kind, "vendor": vendor, "level": None,
         "sensor": None}
    d.update(kw)
    if d["battery"] is not None and not d["batterylow"]:
        try:
            d["batterylow"] = float(d["battery"]) <= 15
        except (TypeError, ValueError):
            pass
    return d


def ext_id(ain):
    """„x:sh1:0“ → „sh1“ (oder None bei FRITZ-Geräten)."""
    if isinstance(ain, str) and ain.startswith("x:"):
        parts = ain.split(":", 2)
        return parts[1] if len(parts) == 3 else None
    return None


def sub_id(ain):
    return ain.split(":", 2)[2]


# --------------------------------------------------------------------------- #
# HTTP-Helfer (mit Basic- und Digest-Anmeldung – Shelly Gen2 nutzt SHA-256-Digest)
# --------------------------------------------------------------------------- #
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Heimnetz: nie über einen Proxy


def _digest_header(www, method, path, user, pw):
    p = dict(re.findall(r'(\w+)="?([^",]+)"?', www))
    algo = p.get("algorithm", "MD5").upper()
    h = (lambda s: hashlib.sha256(s.encode()).hexdigest()) if "SHA-256" in algo else (
        lambda s: hashlib.md5(s.encode()).hexdigest())  # noqa: S324 – so verlangt es das Gerät
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    cnonce, nc = os.urandom(8).hex(), "00000001"
    ha1, ha2 = h(f"{user}:{realm}:{pw}"), h(f"{method}:{path}")
    qop = "auth" if "auth" in p.get("qop", "auth").split(",") else ""
    resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}") if qop else h(f"{ha1}:{nonce}:{ha2}")
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", algorithm={algo}, '
            f'response="{resp}"' + (f', qop={qop}, nc={nc}, cnonce="{cnonce}"' if qop else ""))


def http(host, path, method="GET", body=None, user="", pw="", timeout=TIMEOUT, raw=False):
    """JSON holen/senden. Wirft ProviderError mit verständlicher Meldung."""
    url = f"http://{host}{path}"
    data = json.dumps(body).encode() if body is not None else (b"" if method in ("POST", "PUT") else None)
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    if user or pw:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                b = r.read(2_000_000)
                return b if raw else (json.loads(b.decode("utf-8", "replace")) if b.strip() else {})
        except urllib.error.HTTPError as e:
            www = e.headers.get("WWW-Authenticate", "") if e.headers else ""
            if e.code == 401 and attempt == 0 and www.lower().startswith("digest") and (user or pw):
                headers["Authorization"] = _digest_header(www, method, path, user or "admin", pw)
                continue
            if e.code == 401:
                raise ProviderError("Passwort falsch oder fehlt") from e
            raise ProviderError(f"Gerät meldet Fehler {e.code}") from e
        except (OSError, ValueError) as e:
            raise ProviderError(f"nicht erreichbar ({host})") from e
    raise ProviderError("Anmeldung fehlgeschlagen")


# --------------------------------------------------------------------------- #
# Basis für Anbieter
# --------------------------------------------------------------------------- #
class Provider:
    """Ein eingerichteter Anbieter (z. B. ein Shelly, eine Hue Bridge, ein Zigbee2MQTT)."""
    interval = 5  # Sekunden zwischen Abfragen
    push = False  # True = meldet sich selbst (MQTT), poll() liefert nur den letzten Stand

    def __init__(self, c, on_change=None):
        self.c, self.id = c, c["id"]
        self.on_change = on_change or (lambda: None)
        self.devs, self.error, self.ok_once = [], "", False

    def ain(self, sub):
        return f"x:{self.id}:{sub}"

    def name(self, default):
        return self.c.get("name") or default

    # muss jeder Anbieter liefern
    def poll(self):
        raise NotImplementedError

    def set_switch(self, dev, on):
        raise ProviderError("Dieses Gerät kann nicht geschaltet werden")

    def set_level(self, dev, pct):
        raise ProviderError("Dieses Gerät kann nicht gedimmt werden")

    def set_thermostat(self, dev, value):
        raise ProviderError("Dieses Gerät ist kein Heizkörperregler")

    def press(self, dev, key, value=None):
        raise ProviderError("Dieses Gerät hat keine Fernbedienung")

    def toggle(self, dev):
        on = dev["switch"] if dev.get("switch") is not None else dev.get("simpleonoff")
        self.set_switch(dev, not on)

    def start(self):
        pass

    def stop(self):
        pass


def make(c, on_change=None):
    typ = c.get("type")
    if typ == "shelly":
        from prov_shelly import Shelly
        return Shelly(c, on_change)
    if typ == "hue":
        from prov_hue import Hue
        return Hue(c, on_change)
    if typ == "tasmota":
        from prov_tasmota import Tasmota
        return Tasmota(c, on_change)
    if typ == "esphome":
        from prov_tasmota import ESPHome
        return ESPHome(c, on_change)
    if typ == "z2m":
        from prov_z2m import Zigbee2MQTT
        return Zigbee2MQTT(c, on_change)
    if typ == "skyq":
        from prov_media import SkyQ
        return SkyQ(c, on_change)
    if typ == "vestel":
        from prov_media import VestelTV
        return VestelTV(c, on_change)
    raise ProviderError(f"Unbekannter Anbieter „{typ}“")


# --------------------------------------------------------------------------- #
# Verwaltung: alle Anbieter abfragen, Befehle verteilen
# --------------------------------------------------------------------------- #
class Manager:
    def __init__(self, get_cfg, on_result):
        """get_cfg() → Liste der eingerichteten Anbieter; on_result(pid, devs, error, urgent) aus dem Thread."""
        self.get_cfg, self.on_result = get_cfg, on_result
        self.provs, self._next = {}, {}
        self._stop, self._wake = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._urgent = set()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="Geräte-Anbieter")
            self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        for p in list(self.provs.values()):
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass

    def reload(self):
        """Einstellungen geändert → Anbieter neu aufbauen."""
        want = {c["id"]: c for c in self.get_cfg() if c.get("enabled", True)}
        with self._lock:
            for pid in list(self.provs):
                if pid not in want or self.provs[pid].c != want[pid]:
                    self.provs.pop(pid).stop()
                    self._next.pop(pid, None)
                    self.on_result(pid, None, "", True)  # Geräte dieses Anbieters entfernen
            for pid, c in want.items():
                if pid not in self.provs:
                    try:
                        p = make(c, on_change=lambda pid=pid: self.poke(pid))
                        p.start()
                        self.provs[pid] = p
                        self._next[pid] = 0
                    except Exception as e:  # noqa: BLE001
                        self.on_result(pid, [], str(e), True)
        self._wake.set()

    def poke(self, pid, urgent=True):
        with self._lock:
            self._next[pid] = 0
            if urgent:
                self._urgent.add(pid)
        self._wake.set()

    def provider(self, ain):
        return self.provs.get(ext_id(ain))

    def _run(self):
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                due = [(pid, p) for pid, p in self.provs.items() if self._next.get(pid, 0) <= now]
            for pid, p in due:
                try:
                    devs, err = p.poll(), ""
                    p.ok_once = True
                except Exception as e:  # noqa: BLE001
                    devs, err = None, str(e)
                p.error = err
                with self._lock:
                    if pid not in self.provs:
                        continue
                    urgent = pid in self._urgent
                    self._urgent.discard(pid)
                    self._next[pid] = time.time() + (p.interval if not err else max(15, p.interval))
                if devs is not None:
                    p.devs = devs
                self.on_result(pid, devs if devs is not None else [dict(d, present=False) for d in p.devs],
                               err, urgent)
            with self._lock:
                nxt = min(self._next.values(), default=now + 5)
            self._wake.wait(max(0.2, min(5, nxt - time.time())))
            self._wake.clear()


class Hub:
    """Wird den Schalt-Befehlen übergeben (statt nur des FRITZ-Clients). Leitet jedes Gerät an seinen Anbieter."""

    def __init__(self, fritz, manager):
        self.fritz, self.manager = fritz, manager

    def _route(self, ain):
        p = self.manager.provider(ain) if self.manager else None
        if ext_id(ain) and not p:
            raise ProviderError("Anbieter für dieses Gerät ist nicht eingerichtet")
        return p

    def _fritz(self):
        if not self.fritz:
            from fritz_api import FritzError
            raise FritzError("Nicht verbunden")
        return self.fritz

    def _after(self, p):
        self.manager.poke(p.id)

    def set_switch(self, dev, on):
        p = self._route(dev["ain"])
        if p:
            p.set_switch(dev, on)
            return self._after(p)
        return self._fritz().set_switch(dev, on)

    def toggle(self, dev):
        p = self._route(dev["ain"])
        if p:
            p.toggle(dev)
            return self._after(p)
        return self._fritz().toggle(dev)

    def set_level(self, dev, pct):
        p = self._route(dev["ain"])
        if p:
            p.set_level(dev, pct)
            return self._after(p)
        return self._fritz().set_level(dev["ain"], pct)

    def press(self, dev, key, value=None):
        """Fernbedienungstaste (Fernseher, TV-Box)."""
        p = self._route(dev["ain"])
        if not p:
            raise ProviderError("Dieses Gerät hat keine Fernbedienung")
        p.press(dev, key, value)
        return self._after(p)

    def set_thermostat(self, ain, value):
        p = self._route(ain)
        if p:
            dev = next((d for d in p.devs if d["ain"] == ain), {"ain": ain})
            p.set_thermostat(dev, value)
            return self._after(p)
        return self._fritz().set_thermostat(ain, value)

    def __getattr__(self, k):  # alles andere (WLAN, Neustart …) geht an die FRITZ!Box
        return getattr(self._fritz(), k)


def detect_type(host):
    """Was steht unter dieser Adresse? → (Typ, Name) oder (None, Grund). Für die Einrichtung."""
    try:
        j = http(host, "/shelly", timeout=3)
        if isinstance(j, dict) and (j.get("type") or j.get("gen") or j.get("model")):
            return "shelly", j.get("name") or j.get("app") or j.get("type") or "Shelly"
    except ProviderError:
        pass
    try:
        j = http(host, "/api/config", timeout=3)
        if isinstance(j, dict) and j.get("bridgeid"):
            return "hue", j.get("name") or "Hue Bridge"
    except ProviderError:
        pass
    try:
        j = http(host, "/cm?cmnd=Status", timeout=3)
        if isinstance(j, dict) and isinstance(j.get("Status"), dict):
            fn = j["Status"].get("FriendlyName")
            return "tasmota", (fn[0] if isinstance(fn, list) and fn else j["Status"].get("DeviceName") or "Tasmota")
    except ProviderError:
        pass
    try:
        b = http(host, "/", timeout=3, raw=True)
        if b"esphome" in b.lower() or b"esp-app" in b.lower():
            return "esphome", "ESPHome-Gerät"
    except ProviderError:
        pass
    try:
        from prov_media import detect
        t, n = detect(host)
        if t:
            return t, n
    except Exception:  # noqa: BLE001
        pass
    return None, "Unter dieser Adresse habe ich kein bekanntes Gerät gefunden"



def sensor_text(s):
    """Melder-Zustand → (Kurztext, Stimmung) – Stimmung: "bad", "warn", "ok", "mut"."""
    if not s:
        return "", "mut"
    if s.get("smoke"):
        return "🔥 RAUCH!", "bad"
    if s.get("water"):
        return "💧 WASSER!", "bad"
    if s.get("alarm"):
        return "🚨 Alarm", "bad"
    if s.get("contact") == "open":
        return "offen", "warn"
    if s.get("contact") == "closed":
        return "zu", "ok"
    if "smoke" in s or "water" in s or "alarm" in s:
        return "alles ok", "ok"
    if s.get("motion"):
        return "Bewegung", "warn"
    if "motion" in s:
        return "ruhig", "mut"
    if s.get("button") and s["button"][0]:
        return "gedrückt " + time.strftime("%H:%M", time.localtime(s["button"][0])), "mut"
    return "", "mut"
