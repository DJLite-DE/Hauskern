# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
onvif.py – IP-Kameras per ONVIF (offener Standard, z. B. Reolink, Tapo, Hikvision, Dahua, Annke, Amcrest …).
Nur Python-Standardbibliothek.

  discover()                 -> Kameras im Heimnetz finden (WS-Discovery)
  OnvifCamera(host, port, user, pw)
      .connect()             -> Geräteinfos, Profile, Stream-Adressen
      .stream_url(sub=False) -> RTSP-Adresse mit Zugangsdaten
      .move(x, y, zoom) / .stop()   -> Schwenken/Neigen/Zoomen (PTZ)
      .presets() / .goto_preset(token) / .save_preset(name) / .remove_preset(token)
      .has_events / .pullpoint() -> Ereignisse der Kamera (z. B. Bewegungserkennung)
"""
import base64
import datetime as dt
import hashlib
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
}
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class OnvifError(Exception):
    pass


def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for e in el.iter():
        if _ln(e.tag) == name:
            return e
    return None


def _findall(el, name):
    return [e for e in el.iter() if _ln(e.tag) == name]


def _text(el, name, default=""):
    e = _find(el, name) if el is not None else None
    return (e.text or "").strip() if e is not None and e.text else default


# --------------------------------------------------------------------------- #
# Suche im Netzwerk (WS-Discovery)
# --------------------------------------------------------------------------- #
PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{id}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
)


def parse_probe_match(data):
    text = data.decode("utf-8", "ignore")
    m = re.search(r"<[^>]*XAddrs>(.*?)</", text, re.S)
    if not m:
        return None
    xaddrs = [x for x in m.group(1).split() if x.startswith("http")]
    if not xaddrs:
        return None
    # IPv4-Adresse bevorzugen
    xaddr = next((x for x in xaddrs if re.match(r"https?://\d+\.\d+\.\d+\.\d+", x)), xaddrs[0])
    u = urllib.parse.urlparse(xaddr)
    scopes = re.search(r"<[^>]*Scopes[^>]*>(.*?)</", text, re.S)
    name = hw = ""
    if scopes:
        for sc in scopes.group(1).split():
            if "/name/" in sc:
                name = urllib.parse.unquote(sc.split("/name/", 1)[1]).replace("_", " ")
            elif "/hardware/" in sc:
                hw = urllib.parse.unquote(sc.split("/hardware/", 1)[1])
    return {"host": u.hostname, "port": u.port or 80, "xaddr": xaddr, "name": name, "hardware": hw}


def discover(timeout=3.0):
    """Sucht ONVIF-Kameras per Multicast. Gibt eine Liste von dicts zurück."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.bind(("", 0))
        msg = PROBE.format(id=uuid.uuid4()).encode()
        for _ in range(2):
            sock.sendto(msg, ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            info = parse_probe_match(data)
            if info:
                info["host"] = info["host"] or addr[0]
                found[info["host"]] = info
    finally:
        sock.close()
    return sorted(found.values(), key=lambda x: x["host"])


# --------------------------------------------------------------------------- #
# Kamera
# --------------------------------------------------------------------------- #
class OnvifCamera:
    def __init__(self, host, port=80, user="", password="", timeout=6):
        self.host, self.port, self.user, self.password = host, int(port or 80), user, password
        self.timeout = timeout
        self.device_url = f"http://{host}:{self.port}/onvif/device_service"
        self.media_url = self.ptz_url = self.events_url = None
        self.time_offset = 0.0
        self.info = {}
        self.profiles = []      # [{token, name, width, height, ptz}]
        self.main = self.sub = None
        self._uris = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------------- SOAP ----------------
    def _security(self):
        if not self.user:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.time_offset)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (f'<s:Header><Security s:mustUnderstand="1" xmlns="{WSSE}"><UsernameToken>'
                f"<Username>{escape(self.user)}</Username>"
                f'<Password Type="{PW_DIGEST}">{digest}</Password>'
                f'<Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</Nonce>'
                f'<Created xmlns="{WSU}">{created}</Created>'
                "</UsernameToken></Security></s:Header>")

    def call(self, url, body, auth=True, action=None, timeout=None):
        header = self._security() if auth else ""
        if action:  # WS-Addressing – für Ereignis-Abos nötig
            header = header.replace("<s:Header>", "").replace("</s:Header>", "")
            header = (f'<s:Header><wsa:Action xmlns:wsa="{NS["wsa"]}">{escape(action)}</wsa:Action>'
                      f'<wsa:To xmlns:wsa="{NS["wsa"]}">{escape(url)}</wsa:To>'
                      f'<wsa:MessageID xmlns:wsa="{NS["wsa"]}">urn:uuid:{uuid.uuid4()}</wsa:MessageID>{header}</s:Header>')
        env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS["s"]}" '
               f'xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" xmlns:tptz="{NS["tptz"]}" xmlns:tt="{NS["tt"]}" '
               f'xmlns:tev="{NS["tev"]}" xmlns:wsnt="{NS["wsnt"]}">'
               f'{header}<s:Body>{body}</s:Body></s:Envelope>')
        req = urllib.request.Request(url, data=env.encode("utf-8"),
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                return ET.fromstring(r.read())
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            reason = re.search(rb"<[^>]*Text[^>]*>(.*?)</", raw)
            txt = reason.group(1).decode(errors="ignore") if reason else ""
            if e.code in (400, 401, 500) and ("auth" in txt.lower() or "NotAuthorized" in raw.decode(errors="ignore")
                                               or e.code == 401):
                raise OnvifError("Anmeldung abgelehnt – Benutzer/Passwort der Kamera prüfen")
            raise OnvifError(f"Kamera meldet Fehler {e.code} {txt}".strip())
        except (urllib.error.URLError, OSError) as e:
            raise OnvifError(f"Kamera nicht erreichbar ({getattr(e, 'reason', e)})")
        except ET.ParseError:
            raise OnvifError("Ungültige Antwort der Kamera")

    # ---------------- Verbinden ----------------
    def connect(self):
        # Uhrzeit der Kamera holen (WS-Security ist zeitkritisch)
        try:
            r = self.call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
            utc = _find(r, "UTCDateTime")
            if utc is not None:
                d, t = _find(utc, "Date"), _find(utc, "Time")
                cam = dt.datetime(int(_text(d, "Year")), int(_text(d, "Month")), int(_text(d, "Day")),
                                  int(_text(t, "Hour")), int(_text(t, "Minute")), int(_text(t, "Second")),
                                  tzinfo=dt.timezone.utc)
                self.time_offset = (cam - dt.datetime.now(dt.timezone.utc)).total_seconds()
        except (OnvifError, ValueError, TypeError):
            pass
        r = self.call(self.device_url, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
        for cap, attr in (("Media", "media_url"), ("PTZ", "ptz_url"), ("Events", "events_url")):
            el = _find(r, cap)
            if el is not None and _text(el, "XAddr"):
                setattr(self, attr, self._fix_host(_text(el, "XAddr")))
        if not self.media_url:
            raise OnvifError("Kamera bietet keinen Videodienst an")
        try:
            r = self.call(self.device_url, "<tds:GetDeviceInformation/>")
            self.info = {k: _text(r, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber")}
        except OnvifError:
            self.info = {}
        r = self.call(self.media_url, "<trt:GetProfiles/>")
        self.profiles = []
        for p in _findall(r, "Profiles"):
            res = _find(p, "Resolution")
            self.profiles.append({
                "token": p.get("token", ""), "name": _text(p, "Name", p.get("token", "")),
                "width": int(_text(res, "Width", "0") or 0) if res is not None else 0,
                "height": int(_text(res, "Height", "0") or 0) if res is not None else 0,
                "ptz": _find(p, "PTZConfiguration") is not None,
            })
        if not self.profiles:
            raise OnvifError("Keine Videoprofile gefunden")
        by_size = sorted(self.profiles, key=lambda x: x["width"] * x["height"], reverse=True)
        self.main = by_size[0]
        self.sub = by_size[-1] if len(by_size) > 1 else by_size[0]
        return self

    def _fix_host(self, url):
        """Manche Kameras melden interne/falsche Adressen – Host durch den bekannten ersetzen."""
        u = urllib.parse.urlparse(url)
        port = u.port or self.port
        return urllib.parse.urlunparse(u._replace(netloc=f"{self.host}:{port}"))

    @property
    def has_ptz(self):
        return bool(self.ptz_url) and any(p["ptz"] for p in self.profiles)

    def _token(self, sub=False):
        p = self.sub if sub else self.main
        if self.has_ptz and not sub:
            p = next((x for x in self.profiles if x["ptz"]), p)
        return p["token"]

    def stream_url(self, sub=False):
        token = (self.sub if sub else self.main)["token"]
        if token not in self._uris:
            r = self.call(self.media_url,
                          "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                          "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                          f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>")
            uri = _text(r, "Uri")
            if not uri:
                raise OnvifError("Kamera liefert keine Stream-Adresse")
            self._uris[token] = uri
        return with_credentials(self._uris[token], self.user, self.password, self.host)

    # ---------------- PTZ ----------------
    def move(self, x=0.0, y=0.0, zoom=0.0):
        if not self.has_ptz:
            raise OnvifError("Diese Kamera kann nicht geschwenkt werden")
        vel = ""
        if x or y:
            vel += f'<tt:PanTilt x="{x:.2f}" y="{y:.2f}"/>'
        if zoom:
            vel += f'<tt:Zoom x="{zoom:.2f}"/>'
        self.call(self.ptz_url, f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(self._token())}</tptz:ProfileToken>"
                                f"<tptz:Velocity>{vel}</tptz:Velocity></tptz:ContinuousMove>")

    def stop(self):
        if self.has_ptz:
            self.call(self.ptz_url, f"<tptz:Stop><tptz:ProfileToken>{escape(self._token())}</tptz:ProfileToken>"
                                    "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")

    def presets(self):
        if not self.has_ptz:
            return []
        r = self.call(self.ptz_url, f"<tptz:GetPresets><tptz:ProfileToken>{escape(self._token())}"
                                    "</tptz:ProfileToken></tptz:GetPresets>")
        out = []
        for p in _findall(r, "Preset"):
            out.append({"token": p.get("token", ""), "name": _text(p, "Name", p.get("token", ""))})
        return out

    def goto_preset(self, token):
        self.call(self.ptz_url, f"<tptz:GotoPreset><tptz:ProfileToken>{escape(self._token())}</tptz:ProfileToken>"
                                f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>")

    def save_preset(self, name):
        r = self.call(self.ptz_url, f"<tptz:SetPreset><tptz:ProfileToken>{escape(self._token())}</tptz:ProfileToken>"
                                    f"<tptz:PresetName>{escape(name)}</tptz:PresetName></tptz:SetPreset>")
        return _text(r, "PresetToken")

    def remove_preset(self, token):
        self.call(self.ptz_url, f"<tptz:RemovePreset><tptz:ProfileToken>{escape(self._token())}</tptz:ProfileToken>"
                                f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:RemovePreset>")

    # ---------------- Ereignisse (Bewegung) ----------------
    @property
    def has_events(self):
        return bool(self.events_url)

    def pullpoint(self):
        """Ereignis-Abo anlegen. Gibt ein PullPoint-Objekt zurück."""
        if not self.events_url:
            raise OnvifError("Kamera meldet keine Ereignisse")
        r = self.call(self.events_url, "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT300S"
                                       "</tev:InitialTerminationTime></tev:CreatePullPointSubscription>",
                      action=f"{NS['tev']}/EventPortType/CreatePullPointSubscriptionRequest")
        ref = _find(r, "SubscriptionReference")
        addr = _text(ref, "Address") if ref is not None else ""
        if not addr:
            raise OnvifError("Kamera hat kein Ereignis-Abo angelegt")
        return PullPoint(self, self._fix_host(addr))

    def reboot(self):
        self.call(self.device_url, "<tds:SystemReboot/>")


class PullPoint:
    """Holt Ereignisse ab (Long-Polling). pull() -> [(topic, {name: value})]"""

    def __init__(self, cam, url):
        self.cam, self.url, self.last_renew = cam, url, time.time()

    def pull(self, wait=10):
        if time.time() - self.last_renew > 120:
            self.renew()
        r = self.cam.call(self.url, f"<tev:PullMessages><tev:Timeout>PT{int(wait)}S</tev:Timeout>"
                                    "<tev:MessageLimit>20</tev:MessageLimit></tev:PullMessages>",
                          action=f"{NS['tev']}/PullPointSubscription/PullMessagesRequest", timeout=wait + 8)
        return parse_events(r)

    def renew(self):
        self.cam.call(self.url, "<wsnt:Renew><wsnt:TerminationTime>PT300S</wsnt:TerminationTime></wsnt:Renew>",
                      action=f"{NS['wsnt']}/SubscriptionManager/RenewRequest")
        self.last_renew = time.time()

    def close(self):
        try:
            self.cam.call(self.url, "<wsnt:Unsubscribe/>", action=f"{NS['wsnt']}/SubscriptionManager/UnsubscribeRequest",
                          timeout=3)
        except OnvifError:
            pass


def parse_events(root):
    out = []
    for msg in _findall(root, "NotificationMessage"):
        topic = (_text(msg, "Topic") or "").strip()
        items = {}
        data = _find(msg, "Data")
        for si in (_findall(data, "SimpleItem") if data is not None else []):
            items[si.get("Name", "")] = si.get("Value", "")
        out.append((topic, items))
    return out


MOTION_KEYS = ("IsMotion", "State", "Motion", "IsPeople", "IsPerson")


def motion_value(topic, items):
    """True/False bei einem Bewegungs-Ereignis, sonst None."""
    t = topic.lower()
    if "motion" not in t and "people" not in t and "person" not in t:
        return None
    for k in MOTION_KEYS:
        if k in items:
            return str(items[k]).strip().lower() in ("true", "1", "on")
    return None


def with_credentials(url, user, password, host=None):
    """Fügt Benutzer/Passwort in eine RTSP-Adresse ein (und korrigiert ggf. den Host)."""
    u = urllib.parse.urlparse(url)
    hostname = host or u.hostname
    netloc = hostname + (f":{u.port}" if u.port else "")
    if user:
        netloc = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@{netloc}"
    return urllib.parse.urlunparse(u._replace(netloc=netloc))
