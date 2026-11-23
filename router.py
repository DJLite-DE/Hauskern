# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
router.py – Hauskern ohne FRITZ!Box: Router erkennen und mit „irgendeinem“ Router arbeiten.

    detect()            → {"kind": "fritz"|"generic"|"none", "host", "name", "upnp": bool, ...}
    GenericRouter(host) → gleiche Schnittstelle wie FritzClient (connect, overview, hosts, wlans, smarthome, mesh)

Was mit jedem Router geht:
  • Internet online/offline, externe IP, Datenrate – über UPnP (IGD), das fast alle Router können
  • Geräte im Netz + Anwesenheit – über die Nachbartabelle (ARP) des Rechners
Was nur mit FRITZ!Box geht: WLAN schalten, Gast-WLAN, DECT-Smart-Home, Strom-Statistik der Box.
"""
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from fritz_api import FritzError

ONLY_FRITZ = "Das geht nur mit einer FRITZ!Box"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _run(args, timeout=6):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    for enc in ("utf-8", "cp850", "cp1252"):
        try:
            return r.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.stdout.decode("latin-1", "replace")


# --------------------------------------------------------------------------- #
# Netz-Grundlagen
# --------------------------------------------------------------------------- #
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # es wird nichts gesendet – nur die passende eigene Adresse ermitteln
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def parse_route_print(text):
    """Windows `route print -4 0.0.0.0` → Gateway der Standardroute (kleinste Metrik)."""
    best = None
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0" and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p[2]):
            try:
                metric = int(p[-1])
            except ValueError:
                metric = 9999
            if best is None or metric < best[0]:
                best = (metric, p[2])
    return best[1] if best else ""


def parse_ip_route(text):
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1) if m else ""


def default_gateway():
    if os.name == "nt":
        gw = parse_route_print(_run(["route", "print", "-4", "0.0.0.0"]))
    else:
        gw = parse_ip_route(_run(["ip", "-4", "route", "show", "default"]))
    if not gw:  # Notlösung: x.y.z.1 im eigenen Netz
        ip = local_ip()
        if ip:
            gw = ".".join(ip.split(".")[:3] + ["1"])
    return gw


MAC_RX = re.compile(r"([0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2}")


def parse_neighbors(text):
    """Nachbartabelle lesen: Windows `netsh … show neighbors` / `arp -a` (deutsch oder englisch) oder Linux `ip neigh`.
    → {ip: (MAC, "now"|"recent"|"gone")}"""
    out = {}
    for line in text.splitlines():
        ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        mm = MAC_RX.search(line)
        if not ipm or not mm:
            continue
        ip, mac = ipm.group(1), mm.group(0).replace("-", ":").upper()
        if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF") or mac.startswith("01:00:5E") or mac.startswith("33:33"):
            continue
        low = line.lower()
        if any(w in low for w in ("unreachable", "nicht erreichbar", "failed", "incomplete", "unvollständig")):
            state = "gone"
        elif any(w in low for w in ("stale", "veraltet")):
            state = "recent"
        elif any(w in low for w in ("permanent", "statisch", "static", "noarp")):
            continue  # feste Einträge (Broadcast o. Ä.) sind keine Geräte
        else:  # reachable / erreichbar / delay / probe / dynamisch / dynamic
            state = "now"
        out[ip] = (mac, state)
    return out


def read_neighbors():
    if os.name == "nt":
        txt = _run(["netsh", "interface", "ipv4", "show", "neighbors"])
        if not MAC_RX.search(txt):
            txt = _run(["arp", "-a"])
    else:
        txt = _run(["ip", "-4", "neigh", "show"])
        if not MAC_RX.search(txt):
            txt = _run(["arp", "-an"])
    return parse_neighbors(txt)


def poke_subnet(my_ip, prefix=24):
    """Jede Adresse im Heimnetz kurz „anstupsen“, damit der Rechner ihre MAC-Adresse lernt (ARP).
    Kein Portscan: ein einzelnes UDP-Paket an den Discard-Port, ohne Inhalt."""
    try:
        net = ipaddress.ip_network(f"{my_ip}/{prefix}", strict=False)
    except ValueError:
        return
    if net.num_addresses > 1024:
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for h in net.hosts():
            if str(h) != my_ip:
                try:
                    s.sendto(b"", (str(h), 9))
                except OSError:
                    pass
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# UPnP Internet Gateway Device
# --------------------------------------------------------------------------- #
SSDP_ADDR = ("239.255.255.250", 1900)
IGD_ST = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2", "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
          "urn:schemas-upnp-org:service:WANIPConnection:1", "urn:schemas-upnp-org:service:WANPPPConnection:1")


def ssdp_igd(gateway="", timeout=2.5):
    """Sucht per SSDP nach dem Router (Internet Gateway Device). Gibt die LOCATION-URL zurück."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.4)
    found = []
    try:
        for st in IGD_ST:
            msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: {st}"
                   "\r\n\r\n").encode()
            try:
                s.sendto(msg, SSDP_ADDR)
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode("latin-1")
                if not gateway or addr[0] == gateway:
                    return loc
                found.append(loc)
    finally:
        s.close()
    return found[0] if found else ""


def _tag(el, name):
    for c in el.iter():
        if c.tag.rsplit("}", 1)[-1] == name:
            return (c.text or "").strip()
    return ""


def parse_igd(xml_text, base):
    """Gerätebeschreibung → {"name","vendor","model","services": {kurzname: control_url}}"""
    root = ET.fromstring(xml_text)
    dev = next((c for c in root.iter() if c.tag.rsplit("}", 1)[-1] == "device"), root)
    info = {"name": _tag(dev, "friendlyName"), "vendor": _tag(dev, "manufacturer"), "model": _tag(dev, "modelName"),
            "services": {}}
    from urllib.parse import urljoin
    for svc in root.iter():
        if svc.tag.rsplit("}", 1)[-1] != "service":
            continue
        st, ctl = _tag(svc, "serviceType"), _tag(svc, "controlURL")
        for short in ("WANIPConnection", "WANPPPConnection", "WANCommonInterfaceConfig"):
            if f":{short}:" in st and short not in info["services"]:
                info["services"][short] = (st, urljoin(base, ctl))
    return info


def soap(url, service_type, action, timeout=4):
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service_type}"/></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": 'text/xml; charset="utf-8"',
                                                          "SOAPAction": f'"{service_type}#{action}"'})
    with _OPENER.open(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    return {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in root.iter()}


def web_title(host, timeout=3):
    """Titel der Router-Weboberfläche – oft steht dort das Modell („Speedport Smart 4“)."""
    for scheme in ("http", "https"):
        try:
            import ssl
            ctx = ssl._create_unverified_context()  # Router haben selbstsignierte Zertifikate; nur der Titel wird gelesen
            h = urllib.request.HTTPSHandler(context=ctx)
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}), h)
            with op.open(f"{scheme}://{host}/", timeout=timeout) as r:
                txt = r.read(60000).decode("utf-8", "replace")
            m = re.search(r"<title[^>]*>\s*([^<]{2,80}?)\s*</title>", txt, re.I)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


GENERIC_TITLES = re.compile(r"^(login|anmeldung|index|home|startseite|web ?ui|router|loading|redirect.*|welcome)$", re.I)


def detect(timeout=2.5):
    """Was für ein Router ist das? Erst FRITZ!Box prüfen, dann UPnP, dann der Titel der Weboberfläche."""
    import fritz_api
    gw = default_gateway()
    res = {"kind": "none", "host": gw, "name": "", "vendor": "", "upnp": False}
    box = fritz_api.find_box(tuple(x for x in (gw, "fritz.box", "192.168.178.1") if x), timeout=timeout)
    if box:
        return dict(res, kind="fritz", host=box["host"] if box["host"] != "fritz.box" or not gw else gw,
                    name=box["model"], vendor="AVM", upnp=True)
    loc = ssdp_igd(gw, timeout)
    if loc:
        try:
            with _OPENER.open(loc, timeout=timeout) as r:
                info = parse_igd(r.read(), loc)
            host = re.search(r"//([^:/]+)", loc).group(1)
            name = info["model"] or info["name"]
            if not name or re.fullmatch(r"(?i)(wireless |internet )?(router|gateway|gateway device|igd)", name) \
                    or re.fullmatch(r"[A-Z0-9\-_ ]{2,12}", name):  # nur eine Typnummer → Hersteller davor
                name = f"{info['vendor']} {name}".strip()
            return dict(res, kind="generic", host=host or gw, name=name or "Router", vendor=info["vendor"],
                        upnp=bool(info["services"]))
        except Exception:  # noqa: BLE001 – dann eben ohne UPnP
            pass
    if gw:
        t = web_title(gw)
        name = t if t and not GENERIC_TITLES.match(t) else "Router"
        return dict(res, kind="generic", name=name)
    return res


# --------------------------------------------------------------------------- #
# Allgemeiner Router – gleiche Schnittstelle wie FritzClient
# --------------------------------------------------------------------------- #
class GenericRouter:
    kind = "generic"
    SCAN_EVERY = 45      # alle 45 s das Heimnetz anstupsen
    GONE_AFTER = 240     # so lange gilt ein Gerät nach dem letzten Lebenszeichen noch als „da“

    def __init__(self, host="", name=""):
        self.host, self.name = host, name
        self.igd = None
        self._last_bytes = None
        self._seen = {}      # MAC -> {"ip", "name", "last", "first"}
        self._names = {}     # IP -> Name (DNS)
        self._last_scan = 0.0
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="Namen")

    # --- Verbindung ---
    def connect(self):
        self.host = self.host or default_gateway()
        if not self.host:
            raise FritzError("Kein Router gefunden – ist der PC mit dem Heimnetz verbunden?")
        loc = ssdp_igd(self.host)
        if loc:
            try:
                with _OPENER.open(loc, timeout=4) as r:
                    self.igd = parse_igd(r.read(), loc)
            except Exception:  # noqa: BLE001
                self.igd = None
        if not self.name:
            if self.igd:
                self.name = self.igd["model"] or self.igd["name"] or "Router"
            else:
                t = web_title(self.host)
                self.name = t if t and not GENERIC_TITLES.match(t) else "Router"

    def _call(self, short, action):
        if not self.igd or short not in self.igd["services"]:
            raise FritzError("UPnP nicht verfügbar")
        st, url = self.igd["services"][short]
        try:
            return soap(url, st, action)
        except Exception as e:  # noqa: BLE001
            raise FritzError(f"UPnP: {e}") from e

    # --- Übersicht ---
    def overview(self):
        d = {"model": self.name or "Router", "firmware": "", "uptime": 0, "kind": "generic",
             "upnp": bool(self.igd and self.igd["services"])}
        conn = None
        for short in ("WANIPConnection", "WANPPPConnection"):
            try:
                st = self._call(short, "GetStatusInfo")
            except FritzError:
                continue
            conn = (short, st)
            if st.get("NewConnectionStatus") == "Connected":
                break
        if conn:
            short, st = conn
            d["online"] = st.get("NewConnectionStatus") == "Connected"
            d["wan_uptime"] = int(st.get("NewUptime") or 0)
            try:
                d["ext_ip"] = self._call(short, "GetExternalIPAddress").get("NewExternalIPAddress", "")
            except FritzError:
                pass
        else:
            d["online"] = internet_ok()
        try:
            link = self._call("WANCommonInterfaceConfig", "GetCommonLinkProperties")
            d["access"] = link.get("NewWANAccessType", "")
            d["max_up"] = int(link.get("NewLayer1UpstreamMaxBitRate") or 0)
            d["max_down"] = int(link.get("NewLayer1DownstreamMaxBitRate") or 0)
        except (FritzError, ValueError):
            pass
        try:
            sent = int(self._call("WANCommonInterfaceConfig", "GetTotalBytesSent").get("NewTotalBytesSent") or 0)
            recv = int(self._call("WANCommonInterfaceConfig", "GetTotalBytesReceived").get("NewTotalBytesReceived") or 0)
            now = time.time()
            if self._last_bytes and (sent or recv):
                t0, s0, r0 = self._last_bytes
                dt = max(0.5, now - t0)
                d["rate_up"] = ((sent - s0) % 2**32) / dt
                d["rate_down"] = ((recv - r0) % 2**32) / dt
            self._last_bytes = (now, sent, recv)
        except (FritzError, ValueError):
            pass
        return d

    # --- Geräte im Netz ---
    def _name(self, ip):
        if ip in self._names:
            return self._names[ip]
        self._names[ip] = ""

        def look():
            try:
                n = socket.gethostbyaddr(ip)[0]
                if n and not re.fullmatch(r"[\d.]+", n):
                    self._names[ip] = n.split(".")[0] if n.endswith((".fritz.box", ".lan", ".local", ".home",
                                                                       ".speedport.ip", ".localdomain")) else n
            except OSError:
                pass
        try:
            self._pool.submit(look)
        except RuntimeError:  # Programm wird gerade beendet
            pass
        return ""

    def hosts(self, now=None, neighbors=None):
        now = now or time.time()
        my = local_ip()
        if neighbors is None:
            if now - self._last_scan >= self.SCAN_EVERY and my:
                self._last_scan = now
                poke_subnet(my)
                time.sleep(1.5)
            neighbors = read_neighbors()
        with self._lock:
            for ip, (mac, state) in neighbors.items():
                if ip == self.host:
                    continue
                e = self._seen.setdefault(mac, {"ip": ip, "first": now, "last": 0.0})
                e["ip"] = ip
                if state == "now":
                    e["last"] = now
                elif state == "recent" and not e["last"]:
                    e["last"] = now - self.GONE_AFTER / 2
            out = []
            for mac, e in self._seen.items():
                active = now - e["last"] < self.GONE_AFTER
                name = self._name(e["ip"]) or ""
                out.append({"name": name, "ip": e["ip"], "mac": mac, "active": active, "iface": "", "port": "",
                            "speed": "", "guest": False})
        if my:
            out.append({"name": socket.gethostname(), "ip": my, "mac": "", "active": True, "iface": "Ethernet",
                        "port": "", "speed": "", "guest": False})
        return out

    # --- Was es hier nicht gibt ---
    def wlans(self):
        return []

    def smarthome(self):
        return []

    def mesh(self):
        raise FritzError("Mesh-Übersicht gibt es nur mit FRITZ!Box")

    def wake(self, mac):
        raise FritzError(ONLY_FRITZ)  # Hauskern sendet dann selbst ein Magic Packet

    def __getattr__(self, name):
        if name.startswith(("set_", "toggle", "energy_", "wlan_")):
            def nope(*a, **k):
                raise FritzError(ONLY_FRITZ)
            return nope
        raise AttributeError(name)


def internet_ok(timeout=2.5):
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False
