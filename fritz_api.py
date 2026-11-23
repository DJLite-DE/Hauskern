# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
fritz_api.py – Zugriff auf FRITZ!Box (TR-064) und FRITZ!DECT (AHA-HTTP-Interface).
Nur Python-Standardbibliothek, keine Zusatzpakete nötig.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

ZERO_SID = "0000000000000000"


class FritzError(Exception):
    pass


def _ln(tag):
    """Lokaler Name eines XML-Tags ohne Namespace."""
    return tag.rsplit("}", 1)[-1]


def _opener(*handlers):
    # ProxyHandler({}) -> Windows-Proxyeinstellungen für lokale Box ignorieren
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


# --------------------------------------------------------------------------- #
# TR-064 (Router-Daten)
# --------------------------------------------------------------------------- #
class TR064:
    def __init__(self, host, user, password, timeout=6):
        self.base = f"http://{host}:49000"
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.base, user, password)
        self.opener = _opener(urllib.request.HTTPDigestAuthHandler(pm))
        self.timeout = timeout
        self.services = {}

    def get(self, path):
        url = path if path.startswith("http") else self.base + path
        try:
            with self.opener.open(url, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FritzError("TR-064: Anmeldung fehlgeschlagen – Benutzer/Passwort prüfen")
            raise FritzError(f"TR-064: HTTP {e.code} bei {path}")
        except (urllib.error.URLError, OSError) as e:
            raise FritzError(f"FRITZ!Box nicht erreichbar ({getattr(e, 'reason', e)})")

    def load(self):
        root = ET.fromstring(self.get("/tr64desc.xml"))
        self.services = {}
        for el in root.iter():
            if _ln(el.tag) != "service":
                continue
            st = ctrl = None
            for c in el:
                if _ln(c.tag) == "serviceType":
                    st = (c.text or "").strip()
                elif _ln(c.tag) == "controlURL":
                    ctrl = (c.text or "").strip()
            if st and ctrl:
                self.services[st] = ctrl
        if not self.services:
            raise FritzError("TR-064 liefert keine Dienste – ist 'Zugriff für Anwendungen' aktiv?")

    def has(self, service):
        return service in self.services

    def call(self, service, action, **args):
        url = self.services.get(service)
        if not url:
            raise FritzError(f"Dienst nicht verfügbar: {service}")
        body_args = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in args.items())
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">{body_args}</u:{action}>'
            "</s:Body></s:Envelope>"
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base + url,
            data=body,
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SoapAction": f"{service}#{action}"},
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FritzError("TR-064: Anmeldung fehlgeschlagen – Benutzer/Passwort prüfen")
            detail = ""
            try:
                m = re.search(rb"<errorDescription>(.*?)</errorDescription>", e.read())
                detail = m.group(1).decode(errors="ignore") if m else ""
            except Exception:
                pass
            raise FritzError(f"{action}: HTTP {e.code} {detail}".strip())
        except (urllib.error.URLError, OSError) as e:
            raise FritzError(f"FRITZ!Box nicht erreichbar ({getattr(e, 'reason', e)})")
        result = {}
        for el in ET.fromstring(raw).iter():
            if _ln(el.tag) == action + "Response":
                for c in el:
                    result[_ln(c.tag)] = (c.text or "").strip()
        return result


# --------------------------------------------------------------------------- #
# AHA-HTTP-Interface (FRITZ!DECT / Smart Home)
# --------------------------------------------------------------------------- #
def calc_challenge_response(challenge, password):
    """Login-Antwort nach AVM-Doku (PBKDF2 bei Challenge '2$...', sonst MD5)."""
    if challenge.startswith("2$"):
        _, it1, salt1, it2, salt2 = challenge.split("$")
        h1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt1), int(it1))
        h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(salt2), int(it2))
        return f"{salt2}${h2.hex()}"
    md5 = hashlib.md5(f"{challenge}-{password}".encode("utf-16le")).hexdigest()
    return f"{challenge}-{md5}"


class AHA:
    def __init__(self, host, user, password, timeout=8):
        self.base = f"http://{host}"
        self.user, self.password = user, password
        self.timeout = timeout
        self.opener = _opener()
        self.sid = None

    def _read(self, url, data=None):
        try:
            with self.opener.open(url, data=data, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise FritzError(f"FRITZ!Box nicht erreichbar ({getattr(e, 'reason', e)})")

    def login(self):
        url = self.base + "/login_sid.lua?version=2"
        root = ET.fromstring(self._read(url))
        block = int(root.findtext("BlockTime") or 0)
        if block > 0:
            raise FritzError(f"Login gesperrt – bitte {block} s warten (zu viele Fehlversuche)")
        resp = calc_challenge_response(root.findtext("Challenge") or "", self.password)
        data = urllib.parse.urlencode({"username": self.user, "response": resp}).encode()
        root = ET.fromstring(self._read(url, data))
        sid = root.findtext("SID")
        if not sid or sid == ZERO_SID:
            raise FritzError("Smart-Home-Login fehlgeschlagen – Benutzer/Passwort prüfen")
        rights = [(_ln(e.tag), e.text) for e in root.iter() if _ln(e.tag) == "Name"]
        names = [t for _, t in rights]
        if names and "HomeAuto" not in names:
            raise FritzError("Dem FRITZ!Box-Benutzer fehlt das Recht 'Smart Home'")
        self.sid = sid

    def cmd(self, switchcmd, **params):
        for attempt in range(2):
            if not self.sid:
                self.login()
            q = urllib.parse.urlencode(
                {"switchcmd": switchcmd, "sid": self.sid, **params}, quote_via=urllib.parse.quote
            )
            try:
                return self._read(f"{self.base}/webservices/homeautoswitch.lua?{q}").decode("utf-8", "replace").strip()
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    self.sid = None  # Session abgelaufen -> neu anmelden
                    continue
                if e.code == 400:
                    raise FritzError(f"Smart Home: Befehl '{switchcmd}' ungültig")
                raise FritzError(f"Smart Home: HTTP {e.code}")
        raise FritzError("Smart Home: Zugriff verweigert")


def _hkr_temp(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if v == 253:
        return "AUS"
    if v == 254:
        return "AN"
    if v > 254 or v < 0:
        return None
    return v / 2


def parse_devicelist(xml_text):
    devices = []
    root = ET.fromstring(xml_text)
    for d in root:
        if d.tag not in ("device", "group"):
            continue
        dev = {
            "ain": d.get("identifier", ""),
            "name": (d.findtext("name") or "").strip() or d.get("identifier", ""),
            "product": d.get("productname", "") or ("Gruppe" if d.tag == "group" else ""),
            "is_group": d.tag == "group",
            "present": d.findtext("present") == "1",
            "switch": None, "simpleonoff": None, "power_w": None, "energy_kwh": None,
            "temp": None, "humidity": None, "hkr": None, "battery": None, "batterylow": False,
        }
        sw = d.find("switch")
        if sw is not None and (sw.findtext("state") or "") in ("0", "1"):
            dev["switch"] = sw.findtext("state") == "1"
        so = d.find("simpleonoff")
        if so is not None and (so.findtext("state") or "") in ("0", "1"):
            dev["simpleonoff"] = so.findtext("state") == "1"
        pm = d.find("powermeter")
        if pm is not None:
            try:
                dev["power_w"] = int(pm.findtext("power")) / 1000
            except (TypeError, ValueError):
                pass
            try:
                dev["energy_kwh"] = int(pm.findtext("energy")) / 1000
            except (TypeError, ValueError):
                pass
        t = d.find("temperature")
        if t is not None:
            try:
                dev["temp"] = int(t.findtext("celsius")) / 10
            except (TypeError, ValueError):
                pass
        h = d.find("humidity")
        if h is not None:
            try:
                dev["humidity"] = int(h.findtext("rel_humidity"))
            except (TypeError, ValueError):
                pass
        hkr = d.find("hkr")
        if hkr is not None:
            dev["hkr"] = {
                "ist": _hkr_temp(hkr.findtext("tist")),
                "soll": _hkr_temp(hkr.findtext("tsoll")),
                "window_open": hkr.findtext("windowopenactiv") == "1",
            }
            if hkr.findtext("battery"):
                dev["battery"] = hkr.findtext("battery")
            dev["batterylow"] = hkr.findtext("batterylow") == "1"
        if d.findtext("battery"):
            dev["battery"] = d.findtext("battery")
        if d.findtext("batterylow") == "1":
            dev["batterylow"] = True
        # Dimmbare Lampen (FRITZ!DECT 500 u. a.)
        lc = d.find("levelcontrol")
        dev["level"] = None
        if lc is not None and (lc.findtext("levelpercentage") or "").isdigit():
            dev["level"] = int(lc.findtext("levelpercentage"))
        # Taster (FRITZ!DECT 440, HAN-FUN-Taster) und Alarm-Melder (Tür/Fenster, Rauch, Wasser über DECT ULE)
        sensor = {}
        presses = []
        for b in d.findall("button"):
            ts = b.findtext("lastpressedtimestamp") or ""
            if ts.isdigit() and int(ts) > 0:
                presses.append((int(ts), (b.findtext("name") or "").strip() or b.get("id", "")))
        if presses:
            sensor["button"] = max(presses)
        al = d.find("alert")
        if al is not None and (al.findtext("state") or "") in ("0", "1"):
            low = dev["name"].lower()
            key = ("contact" if re.search(r"t(ü|ue)r|fenster|kontakt|door|window", low) else
                   "smoke" if re.search(r"rauch|smoke", low) else
                   "water" if re.search(r"wasser|water|leck", low) else "alarm")
            on = al.findtext("state") == "1"
            sensor[key] = ("open" if on else "closed") if key == "contact" else on
        dev["sensor"] = sensor or None
        dev["vendor"] = "AVM"
        # Gerätetyp ableiten
        if dev["is_group"]:
            kind = "Gruppe"
        elif dev["hkr"]:
            kind = "Heizkörperregler"
        elif dev["switch"] is not None and dev["power_w"] is not None:
            kind = "Steckdose"
        elif d.find("colorcontrol") is not None or d.find("levelcontrol") is not None:
            kind = "Lampe"
        elif dev["switch"] is not None or dev["simpleonoff"] is not None:
            kind = "Schalter"
        elif d.find("button") is not None:
            kind = "Taster"
        elif dev["sensor"] and "contact" in dev["sensor"]:
            kind = "Kontakt"
        elif dev["sensor"] and "smoke" in dev["sensor"]:
            kind = "Rauchmelder"
        elif dev["sensor"] and "water" in dev["sensor"]:
            kind = "Wassermelder"
        elif dev["sensor"] and "alarm" in dev["sensor"]:
            kind = "Sensor"
        elif dev["temp"] is not None:
            kind = "Sensor"
        else:
            kind = "Gerät"
        dev["kind"] = kind
        devices.append(dev)
    return devices


# --------------------------------------------------------------------------- #
# Zusammengefasster Client
# --------------------------------------------------------------------------- #
S_DEVINFO = "urn:dslforum-org:service:DeviceInfo:1"
S_COMMON = "urn:dslforum-org:service:WANCommonInterfaceConfig:1"
S_PPP = "urn:dslforum-org:service:WANPPPConnection:1"
S_IP = "urn:dslforum-org:service:WANIPConnection:1"
S_HOSTS = "urn:dslforum-org:service:Hosts:1"
S_WLAN = "urn:dslforum-org:service:WLANConfiguration:{}"


class FritzClient:
    def __init__(self, host, user, password):
        self.host = host
        self.tr = TR064(host, user, password)
        self.aha = AHA(host, user, password)
        self._last_bytes = None  # (zeit, gesendet, empfangen)

    def connect(self):
        self.tr.load()
        self.tr.call(S_DEVINFO, "GetInfo")  # prüft gleich die Anmeldung

    # ---------- Übersicht ----------
    def overview(self):
        d = {}
        info = self.tr.call(S_DEVINFO, "GetInfo")
        d["model"] = info.get("NewModelName", "FRITZ!Box")
        d["firmware"] = info.get("NewSoftwareVersion", "")
        d["uptime"] = int(info.get("NewUpTime") or 0)

        try:
            link = self.tr.call(S_COMMON, "GetCommonLinkProperties")
            d["access"] = link.get("NewWANAccessType", "")
            d["link_status"] = link.get("NewPhysicalLinkStatus", "")
            d["max_up"] = int(link.get("NewLayer1UpstreamMaxBitRate") or 0)
            d["max_down"] = int(link.get("NewLayer1DownstreamMaxBitRate") or 0)
        except FritzError:
            pass

        conn = None
        for svc in (S_PPP, S_IP):
            if not self.tr.has(svc):
                continue
            try:
                st = self.tr.call(svc, "GetInfo")
            except FritzError:
                continue
            if conn is None or st.get("NewConnectionStatus") == "Connected":
                conn = st
            if st.get("NewConnectionStatus") == "Connected":
                break
        if conn:
            d["online"] = conn.get("NewConnectionStatus") == "Connected"
            d["ext_ip"] = conn.get("NewExternalIPAddress", "")
            d["wan_uptime"] = int(conn.get("NewUptime") or 0)

        try:
            sent = int(self.tr.call(S_COMMON, "GetTotalBytesSent").get("NewTotalBytesSent") or 0)
            recv = int(self.tr.call(S_COMMON, "GetTotalBytesReceived").get("NewTotalBytesReceived") or 0)
            now = time.time()
            if self._last_bytes:
                t0, s0, r0 = self._last_bytes
                dt = max(0.5, now - t0)
                d["rate_up"] = ((sent - s0) % 2**32) / dt
                d["rate_down"] = ((recv - r0) % 2**32) / dt
            self._last_bytes = (now, sent, recv)
        except FritzError:
            pass
        return d

    # ---------- Geräte im Heimnetz ----------
    def hosts(self):
        hosts = []
        try:
            path = self.tr.call(S_HOSTS, "X_AVM-DE_GetHostListPath").get("NewX_AVM-DE_HostListPath")
            root = ET.fromstring(self.tr.get(path))
            for item in root.iter("Item"):
                g = lambda k: (item.findtext(k) or "").strip()
                hosts.append({
                    "name": g("HostName"), "ip": g("IPAddress"), "mac": g("MACAddress"),
                    "active": g("Active") == "1", "iface": g("InterfaceType"),
                    "port": g("X_AVM-DE_Port"), "speed": g("X_AVM-DE_Speed"),
                    "guest": g("X_AVM-DE_Guest") == "1",
                })
            return hosts
        except FritzError:
            pass
        # Fallback für ältere FRITZ!OS-Versionen
        n = int(self.tr.call(S_HOSTS, "GetHostNumberOfEntries").get("NewHostNumberOfEntries") or 0)
        for i in range(min(n, 250)):
            e = self.tr.call(S_HOSTS, "GetGenericHostEntry", NewIndex=i)
            hosts.append({
                "name": e.get("NewHostName", ""), "ip": e.get("NewIPAddress", ""),
                "mac": e.get("NewMACAddress", ""), "active": e.get("NewActive") == "1",
                "iface": e.get("NewInterfaceType", ""), "port": "", "speed": "", "guest": False,
            })
        return hosts

    # ---------- Wake-on-LAN ----------
    def wake(self, mac):
        """Gerät über die FRITZ!Box aufwecken (klappt auch, wenn Hauskern in einem anderen Netzsegment läuft)."""
        self.tr.call(S_HOSTS, "X_AVM-DE_WakeOnLANByMACAddress", NewMACAddress=mac.upper())

    # ---------- Mesh (welche Box, welches Gerät hängt wo) ----------
    def mesh(self):
        """Gibt (boxen, mac_map) zurück.
        boxen:   [{name, model, role, mac}]  – alle AVM-Geräte im Mesh (Master, Repeater …)
        mac_map: {MAC: {"ap": Gerätename der Box, "model": …, "via": "2,4 GHz"/"5 GHz"/"LAN"}}"""
        path = self.tr.call(S_HOSTS, "X_AVM-DE_GetMeshListPath").get("NewX_AVM-DE_MeshListPath")
        if not path:
            return [], {}
        data = json.loads(self.tr.get(path).decode("utf-8", "replace"))
        return parse_mesh(data)

    # ---------- WLAN ----------
    def wlans(self):
        res = []
        for i in range(1, 5):
            svc = S_WLAN.format(i)
            if not self.tr.has(svc):
                continue
            try:
                info = self.tr.call(svc, "GetInfo")
            except FritzError:
                continue
            try:
                clients = int(self.tr.call(svc, "GetTotalAssociations").get("NewTotalAssociations") or 0)
            except FritzError:
                clients = None
            band = info.get("NewX_AVM-DE_FrequencyBand", "")
            ch = int(info.get("NewChannel") or 0)
            if band.startswith("2"):
                band = "2,4 GHz"
            elif band.startswith("5"):
                band = "5 GHz"
            elif band.startswith("6"):
                band = "6 GHz"
            else:
                band = "2,4 GHz" if 0 < ch <= 14 else ("5 GHz" if ch else "–")
            res.append({
                "index": i, "ssid": info.get("NewSSID", ""), "enabled": info.get("NewEnable") == "1",
                "status": info.get("NewStatus", ""), "channel": ch, "standard": info.get("NewStandard", ""),
                "band": band, "clients": clients,
            })
        return res

    def set_wlan(self, index, enable):
        self.tr.call(S_WLAN.format(index), "SetEnable", NewEnable=1 if enable else 0)

    # ---------- Gast-WLAN ----------
    def wlan_details(self, index):
        """SSID, Passwort und Verschlüsselung eines WLANs (für den QR-Code)."""
        svc = S_WLAN.format(index)
        info = self.tr.call(svc, "GetInfo")
        keys = self.tr.call(svc, "GetSecurityKeys")
        beacon = info.get("NewBeaconType", "")
        return {"index": index, "ssid": info.get("NewSSID", ""), "enabled": info.get("NewEnable") == "1",
                "password": keys.get("NewKeyPassphrase", ""), "beacon": beacon,
                "security": "nopass" if beacon in ("None", "Basic", "") and not keys.get("NewKeyPassphrase") else "WPA"}

    def set_wlan_password(self, index, password):
        """Neues WLAN-Passwort setzen – die übrigen Schlüssel bleiben, wie sie sind."""
        if not 8 <= len(password) <= 63:
            raise FritzError("Das WLAN-Passwort muss 8 bis 63 Zeichen lang sein")
        svc = S_WLAN.format(index)
        k = self.tr.call(svc, "GetSecurityKeys")
        self.tr.call(svc, "SetSecurityKeys", NewWEPKey0=k.get("NewWEPKey0", ""), NewWEPKey1=k.get("NewWEPKey1", ""),
                     NewWEPKey2=k.get("NewWEPKey2", ""), NewWEPKey3=k.get("NewWEPKey3", ""),
                     NewPreSharedKey=k.get("NewPreSharedKey", ""), NewKeyPassphrase=password)

    # ---------- Anrufliste ----------
    def call_list(self, max_calls=60):
        """Letzte Anrufe (neueste zuerst). Braucht das FRITZ!Box-Recht „… und Anrufliste“."""
        import urllib.parse
        import calls
        if not self.tr.has(calls.S_ONTEL):
            raise FritzError("Diese FRITZ!Box bietet keine Anrufliste an")
        try:
            url = self.tr.call(calls.S_ONTEL, "GetCallList").get("NewCallListURL", "")
        except FritzError as e:
            if "606" in str(e) or "Not Authorized" in str(e) or "HTTP 500" in str(e):
                raise FritzError("Keine Berechtigung: Der FRITZ!Box-Benutzer von Hauskern braucht das Recht "
                                 "„Sprachnachrichten, Faxnachrichten, FRITZ!App Fon und Anrufliste“") from None
            raise
        if not url:
            raise FritzError("Anrufliste: keine Adresse erhalten")
        u = urllib.parse.urlsplit(url)
        return calls.parse(self.tr.get(f"{u.path}?{u.query}&max={int(max_calls)}"))

    # ---------- Energie-Statistik der FRITZ!Box (Tage/Monate, auch aus der Zeit vor Hauskern) ----------
    def energy_stats(self, ain):
        return parse_energy_stats(self.aha.cmd("getbasicdevicestats", ain=ain))

    # ---------- Smart Home ----------
    def smarthome(self):
        return parse_devicelist(self.aha.cmd("getdevicelistinfos"))

    def toggle(self, dev):
        if dev.get("switch") is not None or dev.get("is_group"):
            self.aha.cmd("setswitchtoggle", ain=dev["ain"])
        elif dev.get("simpleonoff") is not None:
            self.aha.cmd("setsimpleonoff", ain=dev["ain"], onoff=2)
        else:
            raise FritzError("Dieses Gerät kann nicht geschaltet werden")

    def set_switch(self, dev, on):
        if dev.get("switch") is not None or dev.get("is_group"):
            self.aha.cmd("setswitchon" if on else "setswitchoff", ain=dev["ain"])
        elif dev.get("simpleonoff") is not None:
            self.aha.cmd("setsimpleonoff", ain=dev["ain"], onoff=1 if on else 0)
        else:
            raise FritzError("Dieses Gerät kann nicht geschaltet werden")

    def set_level(self, ain, pct):
        """Helligkeit 0–100 % (FRITZ!DECT 500 u. a.)."""
        self.aha.cmd("setlevelpercentage", ain=ain, level=max(0, min(100, int(pct))))

    def set_thermostat(self, ain, value):
        """value: Temperatur in °C (8–28) oder 'aus'/'an'."""
        if isinstance(value, str):
            param = 253 if value.lower() == "aus" else 254
        else:
            param = max(16, min(56, round(value * 2)))
        self.aha.cmd("sethkrtsoll", ain=ain, param=param)


def _via(iface_name, iface_type):
    n = (iface_name or "").upper()
    if "6G" in n:
        return "6 GHz"
    if "5G" in n:
        return "5 GHz"
    if "2G" in n or "2.4" in n:
        return "2,4 GHz"
    if (iface_type or "").upper() == "LAN" or n.startswith("LAN"):
        return "LAN"
    return iface_type or ""


def parse_mesh(data):
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    by_iface = {}
    for n in nodes:
        for i in n.get("node_interfaces", []) or []:
            by_iface[i.get("uid")] = (n, i)

    def is_avm(n):
        return bool(n.get("is_meshed")) or (n.get("device_manufacturer", "").upper() == "AVM"
                                            and "FRITZ" in (n.get("device_model", "") + n.get("device_name", "")).upper())
    roles = {"master": "Mesh Master", "slave": "Mesh Repeater"}
    boxes = []
    for n in nodes:
        if is_avm(n):
            mac = next((i.get("mac_address", "") for i in n.get("node_interfaces", []) or [] if i.get("mac_address")), "")
            boxes.append({"name": n.get("device_name", ""), "model": n.get("device_model", ""),
                          "role": roles.get(n.get("mesh_role", ""), "im Heimnetz"), "mac": mac.upper(),
                          "uid": n.get("uid", "")})
    mac_map = {}
    for n in nodes:
        if is_avm(n):
            continue
        for i in n.get("node_interfaces", []) or []:
            for link in i.get("node_links", []) or []:
                if link.get("state", "CONNECTED") != "CONNECTED":
                    continue
                other = link.get("node_interface_1_uid") if link.get("node_interface_2_uid") == i.get("uid") \
                    else link.get("node_interface_2_uid")
                hit = by_iface.get(other)
                if hit and is_avm(hit[0]) and i.get("mac_address"):
                    mac_map[i["mac_address"].upper()] = {
                        "ap": hit[0].get("device_name", ""), "model": hit[0].get("device_model", ""),
                        "via": _via(hit[1].get("name"), i.get("type")),
                        "rate": link.get("cur_data_rate_rx") or link.get("max_data_rate_rx")}
    return boxes, mac_map


# --------------------------------------------------------------------------- #
# Ping
# --------------------------------------------------------------------------- #
def magic_packet(mac, broadcast="255.255.255.255", port=9):
    """Wake-on-LAN direkt ins Heimnetz schicken (Ersatzweg, falls die FRITZ!Box nicht will)."""
    import socket
    hexmac = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexmac) != 12:
        raise ValueError("ungültige MAC-Adresse")
    data = b"\xff" * 6 + bytes.fromhex(hexmac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s_:
        s_.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s_.sendto(data, (broadcast, port))
    return len(data)


def ping(host, timeout_ms=1000):
    """Gibt (online, antwortzeit_text) zurück."""
    if os.name == "nt":
        cmd, flags = ["ping", "-n", "1", "-w", str(timeout_ms), host], 0x08000000  # CREATE_NO_WINDOW
    else:
        cmd, flags = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host], 0
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout_ms / 1000 + 3, creationflags=flags)
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    text = out.stdout.decode(errors="ignore")
    ok = out.returncode == 0 and "TTL=" in text.upper()
    m = re.search(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*ms", text)
    if ok and m:
        return True, ("<" if m.group(1) == "<" else "") + m.group(2).replace(",", ".") + " ms"
    return ok, ""


def parse_energy_stats(xml_text, now=None):
    """getbasicdevicestats → {"days": [(datum, kWh)], "months": [("JJJJ-MM", kWh)]}, neueste zuerst.
    Der erste Wert ist der laufende Tag bzw. Monat."""
    import datetime as _dt
    out = {"days": [], "months": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    en = root.find("energy")
    if en is None:
        return out
    for st in en.findall("stats"):
        try:
            grid = int(st.get("grid") or 0)
        except ValueError:
            continue
        vals = [v.strip() for v in (st.text or "").split(",")]
        ts = int(st.get("datatime") or 0) or int(now or __import__("time").time())
        ref = _dt.datetime.fromtimestamp(ts)
        kwh = [None if v in ("", "-") else int(v) / 1000 for v in vals]
        if 80000 <= grid <= 90000 and not out["days"]:
            d0 = ref.date()
            out["days"] = [(d0 - _dt.timedelta(days=i), v) for i, v in enumerate(kwh)]
        elif grid >= 2400000 and not out["months"]:
            y, m = ref.year, ref.month
            for v in kwh:
                out["months"].append((f"{y:04d}-{m:02d}", v))
                y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def find_box(candidates=("fritz.box", "192.168.178.1", "192.168.179.1", "192.168.188.1", "169.254.1.1"), timeout=2.0):
    """Sucht die FRITZ!Box ohne Anmeldung (die TR-064-Beschreibung ist frei lesbar).
    Gibt {"host", "model"} oder None zurück."""
    import urllib.request as _ur
    op = _ur.build_opener(_ur.ProxyHandler({}))
    for host in candidates:
        try:
            with op.open(f"http://{host}:49000/tr64desc.xml", timeout=timeout) as r:
                txt = r.read(200000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 – nächste Adresse probieren
            continue
        m = re.search(r"<modelName>([^<]+)</modelName>", txt) or re.search(r"<friendlyName>([^<]+)</friendlyName>", txt)
        if m or "AVM" in txt:
            return {"host": host, "model": (m.group(1).strip() if m else "FRITZ!Box")}
    return None


def test_login(host, user, password):
    """Prüft Adresse + Zugangsdaten. Gibt den Modellnamen zurück oder wirft FritzError."""
    c = FritzClient(host, user, password)
    c.connect()
    info = c.tr.call(S_DEVINFO, "GetInfo")
    return info.get("NewModelName", "FRITZ!Box")
