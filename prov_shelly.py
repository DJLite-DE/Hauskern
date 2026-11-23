# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
prov_shelly.py – Shelly-Geräte im Heimnetz (ohne Cloud).

Gen1 (Shelly 1, 1PM, 2.5, Plug S, Dimmer, Door/Window 2, Flood, H&T …):  /shelly, /status, /relay/N, /light/N
Gen2/3/4 (Plus, Pro, Mini, Plug S Gen3, Smoke …):                       /shelly, /rpc/Shelly.GetStatus, /rpc/Switch.Set

Ein Eintrag = ein Shelly (Adresse). Mehrkanal-Geräte (2.5, Pro 4PM …) werden zu mehreren Geräten.
Hinweis: Batterie-Geräte (Tür/Fenster, Flood, H&T) schlafen meist und sind nur kurz erreichbar – die zeigt
Hauskern dann mit dem letzten bekannten Stand an.
"""
from providers import Provider, ProviderError, http, new_dev, sub_id


class Shelly(Provider):
    interval = 4

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host = c["host"].strip()
        self.user, self.pw = c.get("user", "") or "", c.get("pw", "") or ""
        self.gen, self.info = None, {}

    def _get(self, path):
        return http(self.host, path, user=self.user if self.gen == 1 else ("admin" if self.pw else ""), pw=self.pw)

    def _detect(self):
        j = http(self.host, "/shelly")
        self.gen = int(j.get("gen") or (2 if j.get("id") and j.get("model") else 1))
        self.info = j

    def poll(self):
        if self.gen is None:
            self._detect()
        return self._poll1() if self.gen == 1 else self._poll2()

    # ---------------- Gen1 ----------------
    def _poll1(self):
        st = self._get("/status")
        base = self.name(st.get("name") or self.info.get("type") or "Shelly")
        typ = str(self.info.get("type", "")).upper()
        out = []
        relays, meters = st.get("relays") or [], st.get("meters") or st.get("emeters") or []
        for i, r in enumerate(relays):
            m = meters[i] if i < len(meters) else (meters[0] if len(relays) == 1 and meters else {})
            kind = "Steckdose" if "PLG" in typ else "Schalter"
            out.append(new_dev(self.ain(f"r{i}"), base if len(relays) == 1 else f"{base} {i + 1}", kind, "Shelly",
                               product=typ or "Shelly", switch=bool(r.get("ison")),
                               power_w=_f(m.get("power")),
                               energy_kwh=(_f(m.get("total")) / 60000) if m.get("total") is not None else None))
        for i, li in enumerate(st.get("lights") or []):
            out.append(new_dev(self.ain(f"l{i}"), base if len(st["lights"]) == 1 else f"{base} {i + 1}", "Lampe",
                               "Shelly", product=typ or "Shelly Dimmer", switch=bool(li.get("ison")),
                               level=_i(li.get("brightness")),
                               power_w=_f((meters[i] if i < len(meters) else {}).get("power"))))
        bat = (st.get("bat") or {}).get("value")
        if isinstance(st.get("sensor"), dict) and st["sensor"].get("state") in ("open", "close", "closed"):
            out.append(new_dev(self.ain("s"), base, "Kontakt", "Shelly", product=typ or "Shelly Door/Window",
                               sensor={"contact": "open" if st["sensor"]["state"] == "open" else "closed"},
                               temp=_f((st.get("tmp") or {}).get("tC")) if (st.get("tmp") or {}).get("is_valid", True)
                               else None, battery=bat))
        elif "flood" in st:
            out.append(new_dev(self.ain("s"), base, "Wassermelder", "Shelly", product=typ or "Shelly Flood",
                               sensor={"water": bool(st.get("flood"))}, temp=_f((st.get("tmp") or {}).get("tC")),
                               battery=bat))
        elif not out and (st.get("tmp") or st.get("hum")):
            out.append(new_dev(self.ain("s"), base, "Sensor", "Shelly", product=typ or "Shelly H&T",
                               temp=_f((st.get("tmp") or {}).get("tC")), humidity=_i((st.get("hum") or {}).get("value")),
                               battery=bat))
        if not out:
            raise ProviderError("Shelly antwortet, aber ich kenne diesen Typ noch nicht")
        return out

    # ---------------- Gen2+ ----------------
    def _poll2(self):
        st = self._get("/rpc/Shelly.GetStatus")
        model = self.info.get("app") or self.info.get("model") or "Shelly"
        base = self.name(self.info.get("name") or model)
        sw = sorted(k for k in st if k.startswith("switch:"))
        li = sorted(k for k in st if k.startswith("light:"))
        out = []
        bat = (((st.get("devicepower:0") or {}).get("battery")) or {}).get("percent")
        temp = _f((st.get("temperature:0") or {}).get("tC"))
        hum = _i((st.get("humidity:0") or {}).get("rh"))
        for k in sw:
            s, n = st[k], k.split(":")[1]
            plug = "plug" in model.lower()
            out.append(new_dev(self.ain(f"switch:{n}"), base if len(sw) == 1 else f"{base} {int(n) + 1}",
                               "Steckdose" if plug else "Schalter", "Shelly", product=model,
                               switch=bool(s.get("output")), power_w=_f(s.get("apower")),
                               energy_kwh=(_f((s.get("aenergy") or {}).get("total")) or 0) / 1000
                               if (s.get("aenergy") or {}).get("total") is not None else None))
        for k in li:
            s, n = st[k], k.split(":")[1]
            out.append(new_dev(self.ain(f"light:{n}"), base if len(li) == 1 else f"{base} {int(n) + 1}", "Lampe",
                               "Shelly", product=model, switch=bool(s.get("output")), level=_i(s.get("brightness")),
                               power_w=_f(s.get("apower"))))
        pm = st.get("pm1:0")
        if pm and not sw:
            out.append(new_dev(self.ain("pm1:0"), base, "Messgerät", "Shelly", product=model,
                               power_w=_f(pm.get("apower")),
                               energy_kwh=(_f((pm.get("aenergy") or {}).get("total")) or 0) / 1000))
        if "smoke:0" in st:
            out.append(new_dev(self.ain("smoke:0"), base, "Rauchmelder", "Shelly", product=model,
                               sensor={"smoke": bool(st["smoke:0"].get("alarm"))}, battery=bat))
        elif "flood:0" in st:
            out.append(new_dev(self.ain("flood:0"), base, "Wassermelder", "Shelly", product=model,
                               sensor={"water": bool(st["flood:0"].get("alarm"))}, temp=temp, battery=bat))
        elif not out and (temp is not None or hum is not None):
            out.append(new_dev(self.ain("sensor"), base, "Sensor", "Shelly", product=model, temp=temp, humidity=hum,
                               battery=bat))
        elif out and temp is not None and len(out) == 1 and out[0]["temp"] is None:
            out[0]["temp"] = temp  # z. B. Plus 1PM mit Temperaturfühler-Add-on
        if not out:
            raise ProviderError("Shelly antwortet, hat aber nichts, was Hauskern anzeigen kann")
        return out

    # ---------------- Schalten ----------------
    def set_switch(self, dev, on):
        sub = sub_id(dev["ain"])
        if self.gen is None:
            self._detect()
        if self.gen == 1:
            kind, n = ("relay", sub[1:]) if sub.startswith("r") else ("light", sub[1:])
            self._get(f"/{kind}/{n}?turn={'on' if on else 'off'}")
        elif sub.startswith("switch:"):
            self._get(f"/rpc/Switch.Set?id={sub.split(':')[1]}&on={'true' if on else 'false'}")
        elif sub.startswith("light:"):
            self._get(f"/rpc/Light.Set?id={sub.split(':')[1]}&on={'true' if on else 'false'}")
        else:
            raise ProviderError("Dieses Gerät kann nicht geschaltet werden")

    def set_level(self, dev, pct):
        sub, pct = sub_id(dev["ain"]), max(1, min(100, int(pct)))
        if self.gen == 1 and sub.startswith("l"):
            self._get(f"/light/{sub[1:]}?turn=on&brightness={pct}")
        elif sub.startswith("light:"):
            self._get(f"/rpc/Light.Set?id={sub.split(':')[1]}&on=true&brightness={pct}")
        else:
            raise ProviderError("Dieses Gerät kann nicht gedimmt werden")


def _f(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None
