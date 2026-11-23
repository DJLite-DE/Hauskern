# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
prov_media.py – Fernseher und TV-Boxen lokal im Heimnetz steuern (ohne Cloud).

    SkyQ       Sky-Q-Box (auch Sky Q Mini):  Zustand über Port 9006, laufender Sender über UPnP (Port 49153),
               Fernbedienungstasten über Port 49160.
    VestelTV   Fernseher von Vestel (verkauft als Hanseatic, Telefunken, Toshiba, JVC, Hitachi, Finlux …):
               Tasten per HTTP an Port 56789 („Virtuelle Fernbedienung“ muss im TV-Menü an sein).
               Einschalten aus dem echten Aus geht nur per Wake-on-LAN (MAC-Adresse eintragen).

Alle Geräte kommen im gemeinsamen Format (providers.new_dev) mit zusätzlich
    media = {"now": "ZDF HD (102)" | "", "keys": [unterstützte Tasten], "box": "sky" | "tv"}
Tasten (einheitliche Namen für beide Marken) siehe KEYS.
"""
import re
import socket
import time
import urllib.error
import urllib.request

from providers import Provider, ProviderError, http, new_dev

# einheitliche Tasten → Beschriftung
KEYS = {
    "power": "⏻ Ein/Aus", "play": "▶ Abspielen", "pause": "⏸ Pause", "stop": "⏹ Stopp", "rew": "⏪ Zurückspulen",
    "ffw": "⏩ Vorspulen", "record": "⏺ Aufnehmen", "ch_up": "Sender +", "ch_down": "Sender −",
    "vol_up": "🔊 Lauter", "vol_down": "🔉 Leiser", "mute": "🔇 Ton aus/an", "up": "▲ Hoch", "down": "▼ Runter",
    "left": "◀ Links", "right": "▶ Rechts", "ok": "OK", "back": "↩ Zurück", "home": "⌂ Startseite",
    "guide": "📋 TV-Programm", "info": "ℹ Info", "source": "⇄ Eingang (HDMI …)",
}
KEY_CHOICES = [(k, v) for k, v in KEYS.items()] + [("channel", "Sender wählen (Nummer unten)")]

SKY_CODES = {"power": 0, "ok": 1, "back": 2, "ch_up": 6, "ch_down": 7, "guide": 11, "info": 14, "up": 16,
             "down": 17, "left": 18, "right": 19, "play": 64, "pause": 65, "stop": 66, "record": 67, "ffw": 69,
             "rew": 71, "home": 241, **{str(i): 48 + i for i in range(10)}}
VESTEL_CODES = {"power": 1012, "ok": 1053, "back": 1010, "ch_up": 1032, "ch_down": 1033, "vol_up": 1016,
                "vol_down": 1017, "mute": 1013, "up": 1020, "down": 1019, "left": 1021, "right": 1022, "home": 1046,
                "guide": 1047, "info": 1018, "source": 1056, "play": 1025, "pause": 1049, "stop": 1024, "rew": 1027,
                "ffw": 1028, "record": 1051, **{str(i): 1000 + i for i in range(10)}}

SKY_REMOTE_PORT, SKY_REST_PORT, SKY_UPNP_PORT, SKY_LEGACY_PORT = 49160, 9006, 49153, 5900
VESTEL_PORT = 56789
SKY_UA = {"User-Agent": "SKYPLUS_skyplus"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _host(c):
    h = str(c.get("host", "")).strip()
    if not h:
        raise ProviderError("Bitte die IP-Adresse eintragen")
    return h


def channel_keys(num):
    """„102“ → ["1", "0", "2"]"""
    try:
        s = str(int(float(str(num).strip().replace(",", "."))))
    except ValueError:
        s = re.sub(r"\D", "", str(num))
    if not s:
        raise ProviderError("Bitte eine Sendernummer angeben")
    return list(s[:4])


# --------------------------------------------------------------------------- #
# Sky Q
# --------------------------------------------------------------------------- #
def sky_press(host, code, port=None, timeout=3):
    """Eine Taste an die Sky-Box (Protokoll wie die Sky-App: kurzer Handschlag, dann Drücken + Loslassen)."""
    s = None
    for p in ([port] if port else [SKY_REMOTE_PORT, SKY_LEGACY_PORT]):
        try:
            s = socket.create_connection((host, p), timeout=timeout)
            break
        except OSError:
            continue
    if s is None:
        raise ProviderError("Sky-Box nimmt keine Tasten an (Ports 49160/5900 zu oder Box ganz aus)")
    try:
        s.settimeout(timeout)
        echo = 12
        for _ in range(8):
            data = s.recv(64)
            if not data:
                raise ProviderError("Sky-Box hat die Verbindung beendet")
            if len(data) < 24:
                s.sendall(data[:echo])
                echo = 1
                continue
            hi, lo = 224 + code // 16, code % 16
            s.sendall(bytes([4, 1, 0, 0, 0, 0, hi, lo]))   # drücken
            s.sendall(bytes([4, 0, 0, 0, 0, 0, hi, lo]))   # loslassen
            return
        raise ProviderError("Sky-Box antwortet unerwartet")
    except OSError as e:
        raise ProviderError("Sky-Box antwortet nicht") from e
    finally:
        try:
            s.close()
        except OSError:
            pass


SOAP_MEDIA = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope s:encodingStyle='
              '"http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
              '<s:Body><u:GetMediaInfo xmlns:u="urn:schemas-nds-com:service:SkyPlay:2"><InstanceID>0</InstanceID>'
              '</u:GetMediaInfo></s:Body></s:Envelope>')


def _raw(url, data=None, headers=None, timeout=3):
    req = urllib.request.Request(url, data=data, headers=dict(SKY_UA, **(headers or {})),
                                 method="POST" if data is not None else "GET")
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read(1_000_000).decode("utf-8", "replace")


def find_skyplay(host, upto=12):
    """Steuer-Adresse des SkyPlay-Dienstes aus den UPnP-Beschreibungen der Box suchen."""
    for i in range(upto):
        try:
            xml = _raw(f"http://{host}:{SKY_UPNP_PORT}/description{i}.xml", timeout=2)
        except (OSError, urllib.error.URLError):
            continue
        for block in re.findall(r"<service>(.*?)</service>", xml, re.S):
            if "SkyPlay" in block:
                m = re.search(r"<controlURL>\s*([^<\s]+)\s*</controlURL>", block)
                if m:
                    u = m.group(1)
                    return u if u.startswith("http") else f"http://{host}:{SKY_UPNP_PORT}/{u.lstrip('/')}"
    return None


def parse_media_uri(uri, channels):
    """CurrentURI → Anzeigetext. „xsi://4B1“ = Live-Sender (Kennung hex), „file://pvr/…“ = Aufnahme."""
    uri = (uri or "").strip()
    if uri.startswith("xsi://"):
        try:
            sid = str(int(uri[6:], 16))
        except ValueError:
            return "Live-TV"
        ch = channels.get(sid)
        if ch:
            return f"{ch[1]} ({ch[0]})" if ch[0] else ch[1]
        return "Live-TV"
    if uri.startswith("file://pvr"):
        return "Aufnahme"
    return ""


class SkyQ(Provider):
    interval = 5

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host = _host(c)
        self._ctrl, self._ctrl_try = None, 0
        self._channels, self._ch_at = {}, 0
        self._model = ""

    def _info(self):
        return http(f"{self.host}:{SKY_REST_PORT}", "/as/system/information", timeout=3)

    def _load_channels(self):
        if self._channels and time.time() - self._ch_at < 6 * 3600:
            return
        self._ch_at = time.time()
        try:
            j = http(f"{self.host}:{SKY_REST_PORT}", "/as/services", timeout=4)
            self._channels = {str(s.get("sid")): (str(s.get("c") or ""), str(s.get("t") or ""))
                              for s in (j.get("services") or []) if s.get("sid") is not None}
        except ProviderError:
            pass

    def _now(self):
        if not self._ctrl and time.time() - self._ctrl_try > 600:
            self._ctrl_try = time.time()
            self._ctrl = find_skyplay(self.host)
        if not self._ctrl:
            return ""
        try:
            xml = _raw(self._ctrl, SOAP_MEDIA.encode(), {
                "SOAPACTION": '"urn:schemas-nds-com:service:SkyPlay:2#GetMediaInfo"',
                "Content-Type": 'text/xml; charset="utf-8"'})
        except (OSError, urllib.error.URLError):
            self._ctrl = None  # Adresse ändert sich nach Neustart der Box
            return ""
        m = re.search(r"<CurrentURI>([^<]*)</CurrentURI>", xml)
        if not m:
            return ""
        self._load_channels()
        return parse_media_uri(m.group(1), self._channels)

    def poll(self):
        try:
            info = self._info()
            on = not bool(info.get("activeStandby", False))
            self._model = info.get("hardwareName") or info.get("modelNumber") or self._model
        except ProviderError:
            if port_open(self.host, SKY_REMOTE_PORT) or port_open(self.host, SKY_UPNP_PORT):
                # Box antwortet, nur die Statusabfrage nicht (manche Software-Stände) → Tasten gehen trotzdem
                on = bool(self._ctrl) or bool(self.devs and self.devs[0].get("switch"))
            elif port_open(self.host, SKY_LEGACY_PORT):
                on = bool(self.devs and self.devs[0].get("switch"))  # ältere Software: nur Tasten über Port 5900
            elif not self.ok_once:
                raise ProviderError(f"Sky-Box unter {self.host} antwortet auf keinem ihrer Ports "
                                    f"({SKY_REST_PORT}, {SKY_UPNP_PORT}, {SKY_REMOTE_PORT}, {SKY_LEGACY_PORT})")
            else:
                on = False  # tiefer Standby / Strom aus
        now = self._now() if on else ""
        return [new_dev(self.ain("box"), self.name("Sky Q"), "TV-Box", "Sky", switch=on,
                        product=f"Sky Q {self._model}".strip(),
                        media={"now": now, "box": "sky", "keys": [k for k in KEYS if k in SKY_CODES]})]

    def press(self, dev, key, value=None):
        keys = channel_keys(value) if key == "channel" else [key]
        for i, k in enumerate(keys):
            if k not in SKY_CODES:
                raise ProviderError("Lautstärke und Eingang regelt der Fernseher, nicht die Sky-Box"
                                    if k in ("vol_up", "vol_down", "mute", "source") else f"Taste „{k}“ gibt es hier nicht")
            if i:
                time.sleep(0.4)
            sky_press(self.host, SKY_CODES[k])

    def set_switch(self, dev, on):
        if bool(dev.get("switch")) == bool(on):
            return
        sky_press(self.host, SKY_CODES["power"])
        if on:
            time.sleep(0.6)


# --------------------------------------------------------------------------- #
# Vestel (Hanseatic, Telefunken, Toshiba, JVC …)
# --------------------------------------------------------------------------- #
def wake_on_lan(mac):
    m = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(m) != 12:
        raise ProviderError("MAC-Adresse ungültig (Form AA:BB:CC:DD:EE:FF)")
    pkt = b"\xff" * 6 + bytes.fromhex(m) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(pkt, ("255.255.255.255", port))
    finally:
        s.close()


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class VestelTV(Provider):
    interval = 6
    PATHS = ("/apps/SmartCenter", "/apps/vr/remote")

    def __init__(self, c, on_change=None):
        super().__init__(c, on_change)
        self.host = _host(c)
        self._path = None
        self._woke = 0

    def _send(self, code):
        body_new = f'<?xml version="1.0" ?><remote><key code="{code}"/></remote>'.encode()
        paths = [self._path] if self._path else list(self.PATHS)
        last = None
        for p in paths:
            for body in (body_new, f"<key code={code}/>".encode()):
                req = urllib.request.Request(f"http://{self.host}:{VESTEL_PORT}{p}", data=body, method="POST",
                                             headers={"Content-Type": "text/plain; charset=ISO-8859-1"})
                try:
                    with _OPENER.open(req, timeout=3) as r:
                        r.read(1000)
                    self._path = p
                    return
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code in (404, 405):
                        break  # anderer Pfad
                except OSError as e:
                    raise ProviderError("Fernseher nicht erreichbar (aus oder WLAN im Standby getrennt)") from e
        raise ProviderError("Fernseher nimmt keine Tasten an – im TV-Menü „Virtuelle Fernbedienung“ einschalten"
                            ) from last

    def poll(self):
        on = port_open(self.host, VESTEL_PORT)
        if on:
            self._woke = 0
        elif self._woke and time.time() - self._woke < 40:
            on = True  # wird gerade wach – nicht zurückspringen lassen
        return [new_dev(self.ain("tv"), self.name("Fernseher"), "Fernseher", self.c.get("brand") or "",
                        switch=on, product="Fernseher",
                        media={"now": "", "box": "tv", "keys": [k for k in KEYS if k in VESTEL_CODES]})]

    def press(self, dev, key, value=None):
        keys = channel_keys(value) if key == "channel" else [key]
        for i, k in enumerate(keys):
            if k not in VESTEL_CODES:
                raise ProviderError(f"Taste „{k}“ gibt es hier nicht")
            if i:
                time.sleep(0.3)
            self._send(VESTEL_CODES[k])

    def set_switch(self, dev, on):
        reachable = port_open(self.host, VESTEL_PORT)
        if on:
            if reachable:
                return
            if not self.c.get("mac"):
                raise ProviderError("Der Fernseher ist ganz aus. Zum Einschalten in der Einrichtung die MAC-Adresse "
                                    "eintragen (Wake-on-LAN) – oder die Sky-Box einschalten, die weckt ihn mit.")
            wake_on_lan(self.c["mac"])
            self._woke = time.time()
        elif reachable:
            self._send(VESTEL_CODES["power"])


def is_sky(host):
    """Mehrere Wege, eine Sky-Box zu erkennen (Statusdienst, UPnP-Beschreibung, Fernbedienungs-Port)."""
    try:
        j = http(f"{host}:{SKY_REST_PORT}", "/as/system/information", timeout=3)
        if isinstance(j, dict) and j:
            return True
    except ProviderError:
        pass
    for i in range(4):
        try:
            xml = _raw(f"http://{host}:{SKY_UPNP_PORT}/description{i}.xml", timeout=2)
            if re.search(r"sky|nds-com", xml, re.I):
                return True
        except (OSError, urllib.error.URLError):
            pass
    return port_open(host, SKY_REMOTE_PORT, 2) or port_open(host, SKY_LEGACY_PORT, 2)


def detect(host):
    """(Typ, Name) oder (None, None) – für die automatische Erkennung."""
    if is_sky(host):
        return "skyq", "Sky Q"
    if port_open(host, VESTEL_PORT, 2):
        try:
            req = urllib.request.Request(f"http://{host}:{VESTEL_PORT}/apps/SmartCenter", method="OPTIONS")
            with _OPENER.open(req, timeout=3) as r:
                r.read(100)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
        except OSError:
            return None, None
        return "vestel", "Fernseher"
    return None, None
