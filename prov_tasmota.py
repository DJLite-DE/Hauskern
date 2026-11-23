# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
prov_tasmota.py – Tasmota- und ESPHome-Geräte im Heimnetz.

Tasmota:  /cm?cmnd=Status%200 (Stand), /cm?cmnd=Power1%20On (schalten), Dimmer%2050 (dimmen).
ESPHome:  Webserver-Komponente: /events (Stand als Ereignis-Strom), POST /switch/<id>/turn_on usw.
"""
import json
import re
import time
import urllib.parse
import urllib.request

from providers import UA, Provider, ProviderError, http, new_dev, sub_id


def _f(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Tasmota
# --------------------------------------------------------------------------- #
class Tasmota(Provider):
    interval = 4

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host, self.user, self.pw = c["host"].strip(), c.get("user", "") or "", c.get("pw", "") or ""

    def _cmd(self, cmnd):
        q = {"cmnd": cmnd}
        if self.pw:
            q.update(user=self.user or "admin", password=self.pw)
        return http(self.host, "/cm?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote))

    def poll(self):
        j = self._cmd("Status 0")
        if "Command" in j and j.get("Command") == "Unknown":
            raise ProviderError("Tasmota versteht den Befehl nicht")
        stt, sns = j.get("StatusSTS") or {}, j.get("StatusSNS") or {}
        status = j.get("Status") or {}
        names = status.get("FriendlyName") or []
        base = self.name(status.get("DeviceName") or (names[0] if names else "Tasmota"))
        powers = sorted(k for k in stt if re.fullmatch(r"POWER\d*", k))
        en = sns.get("ENERGY") or {}
        temp = hum = None
        for k, v in sns.items():
            if isinstance(v, dict):
                if temp is None and v.get("Temperature") is not None:
                    temp = _f(v["Temperature"])
                if hum is None and v.get("Humidity") is not None:
                    hum = _f(v["Humidity"])
        out = []
        for i, k in enumerate(powers):
            n = k[5:] or "1"
            label = (names[i] if i < len(names) and names[i] else None) if len(powers) > 1 else None
            single = len(powers) == 1
            dim = "Dimmer" in stt and single
            out.append(new_dev(self.ain(n), (label or (base if single else f"{base} {n}")),
                               "Lampe" if dim else ("Steckdose" if en.get("Power") is not None and single else "Schalter"),
                               "Tasmota", product=status.get("DeviceName") or "Tasmota",
                               switch=str(stt[k]).upper() == "ON",
                               level=int(stt["Dimmer"]) if dim else None,
                               power_w=_f(en.get("Power")) if single else None,
                               energy_kwh=_f(en.get("Total")) if single else None,
                               temp=temp if single else None, humidity=hum if single else None))
        if not out and (temp is not None or hum is not None):
            out.append(new_dev(self.ain("s"), base, "Sensor", "Tasmota", temp=temp, humidity=hum))
        if not out:
            raise ProviderError("Tasmota-Gerät ohne Schalter oder Sensoren")
        return out

    def set_switch(self, dev, on):
        self._cmd(f"Power{sub_id(dev['ain'])} {'On' if on else 'Off'}")

    def toggle(self, dev):
        self._cmd(f"Power{sub_id(dev['ain'])} Toggle")

    def set_level(self, dev, pct):
        self._cmd(f"Dimmer {max(1, min(100, int(pct)))}")


# --------------------------------------------------------------------------- #
# ESPHome (Webserver-Komponente)
# --------------------------------------------------------------------------- #
BIN_KINDS = [(r"t(ü|ue)r|fenster|door|window|contact|kontakt", "Kontakt", "contact"),
             (r"wasser|water|leak|flood", "Wassermelder", "water"),
             (r"rauch|smoke", "Rauchmelder", "smoke"),
             (r"bewegung|motion|pir|presence|occupancy", "Bewegungsmelder", "motion")]


def read_events(host, seconds=2.0, user="", pw=""):
    """Ereignis-Strom kurz mitlesen → {id: daten} (jede Entität meldet beim Verbinden ihren Stand)."""
    req = urllib.request.Request(f"http://{host}/events", headers=dict(UA, Accept="text/event-stream"))
    if pw:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    states, ev, end = {}, None, time.time() + seconds
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=4) as r:
            while time.time() < end:
                try:
                    line = r.readline()
                except OSError:
                    break
                if not line:
                    break
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:") and ev == "state":
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("id"):
                        states[d["id"]] = d
                elif line.startswith("data:") and ev == "ping" and states:
                    break  # Stand ist komplett
    except OSError as e:
        raise ProviderError(f"nicht erreichbar ({host})") from e
    return states


def _split(eid):
    """„switch-relay“ (alt) oder „switch/Relay“ (neu) → (Bereich, Objekt)."""
    if "/" in eid:
        dom, _, obj = eid.partition("/")
    else:
        dom, _, obj = eid.partition("-")
    return dom, obj


class ESPHome(Provider):
    interval = 6

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host, self.user, self.pw = c["host"].strip(), c.get("user", "") or "", c.get("pw", "") or ""

    def poll(self):
        st = read_events(self.host, user=self.user, pw=self.pw)
        if not st:
            raise ProviderError("ESPHome antwortet nicht – ist der Webserver (web_server:) aktiviert?")
        base = self.name("ESPHome")
        out, temp, hum, power, energy = [], None, None, None, None
        for eid, d in st.items():
            dom, obj = _split(eid)
            label = d.get("name") or obj.replace("_", " ").replace("-", " ").strip().capitalize()
            if dom == "sensor":
                v, txt = d.get("value"), f"{label} {d.get('state', '')}".lower()
                if not isinstance(v, (int, float)):
                    continue
                if temp is None and ("°c" in txt or "temp" in txt):
                    temp = round(v, 1)
                elif hum is None and ("%" in txt and ("hum" in txt or "feucht" in txt)):
                    hum = round(v)
                elif power is None and (" w" in txt and "wh" not in txt or "power" in txt or "leistung" in txt):
                    power = round(v, 1)
                elif energy is None and "kwh" in txt:
                    energy = round(v, 3)
            elif dom == "binary_sensor":
                kind, key = "Sensor", "alarm"
                for pat, k, sk in BIN_KINDS:
                    if re.search(pat, label.lower()):
                        kind, key = k, sk
                        break
                val = bool(d.get("value")) if d.get("value") is not None else str(d.get("state")).upper() == "ON"
                out.append(new_dev(self.ain(eid), f"{label}", kind, "ESPHome", product="ESPHome",
                                   sensor={key: ("open" if val else "closed") if key == "contact" else val}))
            elif dom in ("switch", "light", "fan"):
                on = d.get("value") if isinstance(d.get("value"), bool) else str(d.get("state")).upper() == "ON"
                lvl = d.get("brightness")
                out.append(new_dev(self.ain(eid), label if label.lower() not in ("relay", "switch") else base,
                                   "Lampe" if dom == "light" else "Schalter", "ESPHome", product="ESPHome",
                                   switch=bool(on), level=round(int(lvl) / 255 * 100) if lvl is not None else None))
        ctrl = [d for d in out if d["switch"] is not None]
        if len(ctrl) == 1:  # Messwerte zum einzigen Schalter dazu (z. B. Steckdose mit Strommessung)
            ctrl[0].update(power_w=power, energy_kwh=energy, temp=temp, humidity=hum)
            if power is not None:
                ctrl[0]["kind"] = "Steckdose"
        elif temp is not None or hum is not None or power is not None:
            out.append(new_dev(self.ain("sensors"), base, "Sensor", "ESPHome", temp=temp, humidity=hum, power_w=power,
                               energy_kwh=energy))
        return out

    def _post(self, eid, action, query=""):
        dom, obj = _split(eid)
        path = f"/{dom}/{urllib.parse.quote(obj)}/{action}" + (f"?{query}" if query else "")
        http(self.host, path, method="POST", body=None, user=self.user, pw=self.pw, raw=True)

    def set_switch(self, dev, on):
        self._post(sub_id(dev["ain"]), "turn_on" if on else "turn_off")

    def toggle(self, dev):
        self._post(sub_id(dev["ain"]), "toggle")

    def set_level(self, dev, pct):
        eid = sub_id(dev["ain"])
        if not eid.startswith("light"):
            raise ProviderError("Dieses Gerät kann nicht gedimmt werden")
        self._post(eid, "turn_on", f"brightness={round(max(1, min(100, int(pct))) / 100 * 255)}")
