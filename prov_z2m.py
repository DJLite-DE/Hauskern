# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
prov_z2m.py – Zigbee-Geräte über Zigbee2MQTT (läuft auf dem Heimserver, Zigbee-USB-Stick).

Hauskern hört auf „zigbee2mqtt/#“:
    zigbee2mqtt/bridge/devices        Liste aller Geräte (mit Hersteller, Modell, Fähigkeiten)
    zigbee2mqtt/<Name>                aktueller Stand als JSON (state, brightness, power, temperature, contact …)
    zigbee2mqtt/<Name>/availability   online/offline
Schalten: zigbee2mqtt/<Name>/set  {"state": "ON"} / {"brightness": 128} / {"current_heating_setpoint": 21}
"""
import json
import re
import threading
import time

from providers import Provider, ProviderError, new_dev, sub_id


def _exposes(defn):
    """Alle Fähigkeiten flach: {"light", "switch", "climate", "contact", "state", "brightness", …}."""
    out = set()

    def walk(items):
        for e in items or []:
            if e.get("type") in ("light", "switch", "climate", "lock", "cover", "fan"):
                out.add(e["type"])
            if e.get("property"):
                out.add(e["property"])
            walk(e.get("features"))
    walk((defn or {}).get("exposes"))
    return out


def _f(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


class Zigbee2MQTT(Provider):
    interval = 30   # nur zur Sicherheit – Änderungen kommen sofort per MQTT
    push = True

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.base = (c.get("base") or "zigbee2mqtt").strip("/")
        self.info, self.state, self.avail, self.btn = {}, {}, {}, {}
        self._lock = threading.Lock()
        from mqtt import Client
        self.mq = Client(c["host"].strip(), c.get("port") or 1883, c.get("user", ""), c.get("pw", ""),
                         on_message=self._msg, subs=[f"{self.base}/#"])

    def start(self):
        self.mq.start()

    def stop(self):
        self.mq.stop()

    # ---------------- Nachrichten ----------------
    def _msg(self, topic, payload):
        if not topic.startswith(self.base + "/"):
            return
        rest = topic[len(self.base) + 1:]
        if rest == "bridge/devices":
            devs = json.loads(payload.decode("utf-8", "replace") or "[]")
            with self._lock:
                self.info = {d["friendly_name"]: d for d in devs if d.get("type") != "Coordinator"
                             and d.get("friendly_name")}
            self.on_change()
            return
        if rest.startswith("bridge/") or rest.endswith(("/set", "/get")):
            return
        if rest.endswith("/availability"):
            fn = rest[:-len("/availability")]
            txt = payload.decode("utf-8", "replace").strip()
            try:
                txt = json.loads(txt).get("state", txt) if txt.startswith("{") else txt
            except ValueError:
                pass
            with self._lock:
                self.avail[fn] = txt == "online"
            self.on_change()
            return
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            st = self.state.setdefault(rest, {})
            st.update(data)
            if data.get("action"):
                self.btn[rest] = (time.time(), str(data["action"]))
        self.on_change()

    # ---------------- Geräte bauen ----------------
    def poll(self):
        if not self.mq.connected and not self.info:
            raise ProviderError(self.mq.error or "verbinde mit dem MQTT-Server …")
        out = []
        with self._lock:
            items = [(fn, dict(self.state.get(fn) or {}), d) for fn, d in self.info.items()]
            avail, btn = dict(self.avail), dict(self.btn)
        for fn, st, d in sorted(items, key=lambda x: x[0].lower()):
            defn = d.get("definition") or {}
            ex = _exposes(defn)
            ieee = d.get("ieee_address") or fn
            vendor, model = defn.get("vendor") or d.get("manufacturer") or "Zigbee", defn.get("model") or d.get("model_id")
            product = f"{vendor} {model}".strip() if model else vendor
            present = avail.get(fn, True) and not d.get("disabled")
            common = dict(product=product, present=present, battery=st.get("battery"),
                          temp=_f(st.get("temperature")) if "climate" not in ex else None,
                          humidity=_f(st.get("humidity")))
            subs = sorted(k for k in st if re.fullmatch(r"state(_[a-z0-9]+)?", k) and str(st[k]).upper() in ("ON", "OFF"))
            if "climate" in ex:
                soll = st.get("current_heating_setpoint", st.get("occupied_heating_setpoint"))
                out.append(new_dev(self.ain(ieee), fn, "Heizkörperregler", vendor, **dict(common, hkr={
                    "ist": _f(st.get("local_temperature")), "soll": _f(soll),
                    "window_open": bool(st.get("window_open") or str(st.get("window", "")).lower() == "open")})))
                continue
            if subs and ("light" in ex or "switch" in ex or not ex):
                light = "light" in ex
                for k in subs:
                    suffix = k[5:]
                    out.append(new_dev(self.ain(ieee + (":" + suffix[1:] if suffix else "")),
                                       fn if not suffix else f"{fn} {suffix[1:].upper()}",
                                       "Lampe" if light else ("Steckdose" if st.get("power") is not None else "Schalter"),
                                       vendor, **dict(common, switch=str(st[k]).upper() == "ON",
                                                      level=round(st["brightness"] / 254 * 100)
                                                      if light and st.get("brightness") is not None and not suffix else None,
                                                      power_w=_f(st.get("power")) if not suffix else None,
                                                      energy_kwh=_f(st.get("energy")) if not suffix else None)))
                continue
            sensor, kind = {}, "Sensor"
            if "contact" in ex or "contact" in st:
                sensor["contact"] = None if st.get("contact") is None else ("closed" if st["contact"] else "open")
                kind = "Kontakt"
            if "water_leak" in ex or "water_leak" in st:
                sensor["water"], kind = bool(st.get("water_leak")), "Wassermelder"
            if "smoke" in ex or "smoke" in st:
                sensor["smoke"], kind = bool(st.get("smoke")), "Rauchmelder"
            if "occupancy" in ex or "occupancy" in st:
                sensor["motion"] = bool(st.get("occupancy"))
                kind = kind if kind != "Sensor" else "Bewegungsmelder"
            if "action" in ex or fn in btn:
                sensor["button"] = btn.get(fn) or (0, "")  # noch nie gedrückt
                kind = kind if kind != "Sensor" else "Taster"
            if not sensor and common["temp"] is None and common["humidity"] is None and st.get("power") is None:
                continue  # Router/Repeater ohne Anzeige
            out.append(new_dev(self.ain(ieee), fn, kind, vendor, **dict(common, sensor=sensor or None,
                                                                      power_w=_f(st.get("power")))))
        return out

    # ---------------- Schalten ----------------
    def _fn(self, dev):
        sub = sub_id(dev["ain"])
        ieee, _, ep = sub.partition(":")
        with self._lock:
            fn = next((n for n, d in self.info.items() if (d.get("ieee_address") or n) == ieee), None)
        if not fn:
            raise ProviderError("Zigbee-Gerät nicht gefunden")
        return fn, ep

    def _set(self, dev, data):
        fn, _ = self._fn(dev)
        self.mq.publish(f"{self.base}/{fn}/set", json.dumps(data))

    def set_switch(self, dev, on):
        fn, ep = self._fn(dev)
        self.mq.publish(f"{self.base}/{fn}/set", json.dumps({f"state_{ep}" if ep else "state": "ON" if on else "OFF"}))

    def toggle(self, dev):
        fn, ep = self._fn(dev)
        self.mq.publish(f"{self.base}/{fn}/set", json.dumps({f"state_{ep}" if ep else "state": "TOGGLE"}))

    def set_level(self, dev, pct):
        self._set(dev, {"state": "ON", "brightness": max(1, round(max(1, min(100, int(pct))) / 100 * 254))})

    def set_thermostat(self, dev, value):
        fn, _ = self._fn(dev)
        with self._lock:
            st = self.state.get(fn) or {}
        key = "current_heating_setpoint" if "current_heating_setpoint" in st or "occupied_heating_setpoint" not in st \
            else "occupied_heating_setpoint"
        if isinstance(value, str):
            self._set(dev, {"system_mode": "off" if value.lower() == "aus" else "heat"})
        else:
            self._set(dev, {key: float(value)})
