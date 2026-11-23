# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
prov_hue.py – Philips Hue über die Hue Bridge im Heimnetz (ohne Cloud, lokale API).

Kopplung: Knopf auf der Bridge drücken, dann innerhalb von 30 Sekunden pair(host) aufrufen → Schlüssel.
Lampen: an/aus und Helligkeit. Sensoren: Bewegungsmelder (mit Temperatur), Dimmschalter/Taster.
"""
import time

from providers import Provider, ProviderError, http, new_dev, sub_id


def pair(host):
    """→ Schlüssel. Wirft ProviderError mit Hinweis, wenn der Knopf nicht gedrückt wurde."""
    r = http(host, "/api", method="POST", body={"devicetype": "hauskern#zentrale"})
    item = r[0] if isinstance(r, list) and r else {}
    if "success" in item:
        return item["success"]["username"]
    err = (item.get("error") or {})
    if err.get("type") == 101:
        raise ProviderError("Bitte zuerst den runden Knopf auf der Hue Bridge drücken, dann innerhalb von "
                            "30 Sekunden noch einmal auf „Koppeln“")
    raise ProviderError(err.get("description") or "Hue Bridge hat die Kopplung abgelehnt")


def _ts(s):
    """Hue-Zeit „2026-11-18T07:15:02“ (UTC) → Unix-Zeit."""
    try:
        import calendar
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


class Hue(Provider):
    interval = 3

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host, self.key = c["host"].strip(), c.get("key", "")
        if not self.key:
            raise ProviderError("Hue Bridge ist noch nicht gekoppelt")

    def _api(self, path, method="GET", body=None):
        r = http(self.host, f"/api/{self.key}{path}", method=method, body=body)
        if isinstance(r, list) and r and isinstance(r[0], dict) and "error" in r[0]:
            e = r[0]["error"]
            if e.get("type") == 1:
                raise ProviderError("Hue Bridge kennt Hauskern nicht mehr – bitte neu koppeln")
            raise ProviderError(e.get("description") or "Hue-Fehler")
        return r

    def poll(self):
        lights, sensors = self._api("/lights"), self._api("/sensors")
        out = []
        for lid, l in sorted((lights or {}).items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            st = l.get("state") or {}
            dim = "bri" in st
            out.append(new_dev(self.ain(f"l{lid}"), l.get("name"), "Lampe", "Philips Hue",
                               product=l.get("productname") or l.get("modelid") or "Hue",
                               present=bool(st.get("reachable", True)), switch=bool(st.get("on")),
                               level=round(st["bri"] / 254 * 100) if dim and st.get("bri") is not None else None))
        # Sensoren eines Geräts (Bewegung + Temperatur + Helligkeit) haben dieselbe MAC am Anfang der uniqueid
        phys = {}
        for sid, s in (sensors or {}).items():
            typ, st, cf = s.get("type", ""), s.get("state") or {}, s.get("config") or {}
            if typ not in ("ZLLPresence", "ZLLTemperature", "ZLLSwitch", "ZGPSwitch", "ZLLLightLevel"):
                continue
            uid = (s.get("uniqueid") or sid)[:23]
            p = phys.setdefault(uid, {"ids": [], "name": None, "kind": None, "sensor": {}, "temp": None,
                                      "battery": None, "present": True, "product": s.get("productname") or
                                      s.get("modelid") or "Hue"})
            p["ids"].append(sid)
            if cf.get("reachable") is False:
                p["present"] = False
            if cf.get("battery") is not None:
                p["battery"] = cf["battery"]
            if typ == "ZLLPresence":
                p.update(name=s.get("name"), kind="Bewegungsmelder")
                p["sensor"]["motion"] = bool(st.get("presence"))
            elif typ == "ZLLTemperature" and st.get("temperature") is not None:
                p["temp"] = round(st["temperature"] / 100, 1)
                p["name"] = p["name"] or s.get("name")
            elif typ in ("ZLLSwitch", "ZGPSwitch"):
                p.update(name=s.get("name"), kind="Taster")
                if st.get("buttonevent") is not None:
                    p["sensor"]["button"] = (_ts(st.get("lastupdated")), str(st["buttonevent"]))
        for uid, p in phys.items():
            out.append(new_dev(self.ain("s" + min(p["ids"], key=lambda x: int(x) if x.isdigit() else 0)),
                               p["name"] or "Hue-Sensor", p["kind"] or "Sensor", "Philips Hue", product=p["product"],
                               present=p["present"], temp=p["temp"], battery=p["battery"],
                               sensor=p["sensor"] or None))
        return out

    def set_switch(self, dev, on):
        sub = sub_id(dev["ain"])
        if not sub.startswith("l"):
            raise ProviderError("Dieses Gerät kann nicht geschaltet werden")
        self._api(f"/lights/{sub[1:]}/state", "PUT", {"on": bool(on)})

    def set_level(self, dev, pct):
        sub = sub_id(dev["ain"])
        if not sub.startswith("l"):
            raise ProviderError("Dieses Gerät kann nicht gedimmt werden")
        pct = max(1, min(100, int(pct)))
        self._api(f"/lights/{sub[1:]}/state", "PUT", {"on": True, "bri": max(1, round(pct / 100 * 254))})
