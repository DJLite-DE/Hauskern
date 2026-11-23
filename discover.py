# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
discover.py – Findet Smart-Home-Geräte im Heimnetz und erkennt, was sie sind.

Quellen (alles lokal, nichts geht ins Internet):
  * Geräteliste der FRITZ!Box (Name, IP, MAC)
  * mDNS / Bonjour  (224.0.0.251:5353)  – z. B. Shelly, HomeKit, Chromecast, Sonos, ESPHome, WLED, Drucker
  * SSDP / UPnP     (239.255.255.250:1900) – z. B. Hue-Bridge, Fernseher, Sonos, FRITZ!Box
  * kurze HTTP-Proben bei unbekannten Geräten (Shelly, Tasmota, WLED)

scan(hosts, known) -> [{"ip", "mac", "name", "category", "vendor", "model", "status", "hint", "via"}]
status: "drin" (schon in Hauskern) · "lokal" (lokal steuerbar) · "cloud" (nur über Hersteller-Cloud) · "erkannt"
"""
import json
import re
import socket
import struct
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MDNS_ADDR, SSDP_ADDR = ("224.0.0.251", 5353), ("239.255.255.250", 1900)
MDNS_TYPES = ["_services._dns-sd._udp", "_hap._tcp", "_googlecast._tcp", "_airplay._tcp", "_raop._tcp",
              "_shelly._tcp", "_http._tcp", "_esphomelib._tcp", "_wled._tcp", "_hue._tcp", "_sonos._tcp",
              "_spotify-connect._tcp", "_ipp._tcp", "_printer._tcp", "_miio._udp", "_matter._tcp", "_matterc._udp",
              "_homekit._tcp", "_amzn-wplay._tcp", "_androidtvremote2._tcp", "_bambulab._tcp", "_onvif._tcp"]

STATUS_TEXT = {"drin": "In Hauskern eingebunden", "lokal": "Lokal steuerbar – kann eingebaut werden",
               "cloud": "Nur über die Hersteller-Cloud", "erkannt": "Erkannt"}

# (Muster auf Name/Dienste/Server, Kategorie, Hersteller, Status, Hinweis)
RULES = [
    # --- Basisstationen / Hubs (dahinter hängen Geräte per Funk) ---
    (r"x-?sense|sbs50|\bxs0\d", "Rauchmelder-Basisstation", "X-Sense", "cloud",
     "Basisstation für Rauch-/Wassermelder. Die Melder funken direkt mit der Station und sind nicht im WLAN."),
    (r"ei-?electronics|ei-?gateway|\bi-?gw|ei650", "Rauchmelder-Gateway", "Ei Electronics", "cloud",
     "Gateway für Ei-Rauchmelder. Die Melder funken mit dem Gateway und sind nicht im WLAN."),
    (r"hekatron|genius.?(port|gateway|hub)", "Rauchmelder-Gateway", "Hekatron", "cloud",
     "Gateway für Hekatron-Rauchmelder. Die Melder funken mit dem Gateway."),
    (r"rauchmelder|smoke.?(detector|alarm)?|feuermelder", "Rauchmelder", "", "erkannt",
     "WLAN-Rauchmelder – Hauskern kann zeigen, ob er erreichbar ist."),
    (r"ajax.?hub|\bajax\b", "Alarmanlagen-Zentrale", "Ajax", "cloud",
     "Zentrale einer Alarmanlage. Die Sensoren funken mit der Zentrale."),
    (r"homematic|raspberrymatic|ccu[23]|hmip-?hap|hm-?ccu|piVCCU", "Smart-Home-Zentrale", "Homematic (IP)", "lokal",
     "Homematic-Zentrale – die Thermostate, Sensoren und Schalter funken mit ihr."),
    (r"bosch.?smart.?home|\bshc\b|bosch-?shc", "Smart-Home-Zentrale", "Bosch Smart Home", "lokal",
     "Bosch-Smart-Home-Controller – die Geräte funken mit ihm."),
    (r"tradfri|dirigera|ikea", "Zigbee-Gateway", "IKEA", "lokal",
     "IKEA-Gateway – Lampen, Fernbedienungen und Sensoren funken per Zigbee mit ihm."),
    (r"aqara|lumi-?gateway|lumi\.gateway", "Zigbee-Gateway", "Aqara", "cloud",
     "Aqara-Hub – Sensoren funken per Zigbee mit ihm."),
    (r"deconz|phoscon|conbee|raspbee|zbbridge|zigbee|zha-|z2m|zigbee2mqtt", "Zigbee-Gateway", "", "lokal",
     "Zigbee-Gateway – die Zigbee-Geräte funken mit ihm."),
    (r"gigaset.?(elements|base|go)|gigaset", "Basisstation", "Gigaset", "cloud",
     "Gigaset-Basis – Telefone/Sensoren funken per DECT mit ihr."),
    (r"arlo|vmb\d{4}", "Kamera-Basisstation", "Arlo", "cloud", "Arlo-Basisstation – die Kameras funken mit ihr."),
    (r"blink.?sync|sync.?module", "Kamera-Basisstation", "Blink", "cloud",
     "Blink Sync-Modul – die Kameras funken mit ihm."),
    (r"ring.?(alarm|base)", "Alarmanlagen-Zentrale", "Ring", "cloud", "Ring-Alarm-Basis – die Sensoren funken mit ihr."),
    (r"gardena|smart.?gateway", "Garten-Gateway", "Gardena", "cloud",
     "Gardena-Gateway – Mähroboter, Ventile und Sensoren funken mit ihm."),
    (r"nuki.?bridge", "Türschloss-Bridge", "Nuki", "lokal",
     "Nuki-Bridge – das Schloss ist per Bluetooth mit ihr verbunden (lokale Schnittstelle mit Token)."),
    # --- TV-Boxen ---
    (r"sky.?q.?mini|skyq-?mini|sky-?mini", "TV-Box (Sky Q Mini)", "Sky", "erkannt",
     "Zweitbox von Sky – hängt an der Sky-Q-Hauptbox."),
    (r"sky.?q|skyq|sky.?box|sky.?stream|stream.?puck|\bsky\b", "TV-Box (Sky)", "Sky", "erkannt",
     "Sky-Receiver. Sky-Q-Mini-Boxen gehören meist dazu und sind oft selbst im Netz sichtbar."),
    (r"magenta.?tv|mr\d{3}|mediareceiver|magentatv", "TV-Box (MagentaTV)", "Telekom", "erkannt", ""),
    (r"apple.?tv|appletv", "TV-Box (Apple TV)", "Apple", "erkannt", ""),
    (r"nvidia.?shield|shield-?tv", "TV-Box", "NVIDIA", "erkannt", ""),
    (r"playstation|ps[45]-|\bps[45]\b|xbox|nintendo|switch-", "Spielkonsole", "", "erkannt",
     "Kann als Gegenstand („Konsole an“) oder im Ping-Monitor genutzt werden."),
    (r"dreame", "Saugroboter", "Dreame", "cloud",
     "Mit der Dreamehome-App nur über die Dreame-Cloud steuerbar (keine offizielle Schnittstelle). "
     "Hauskern kann zeigen, ob er im WLAN ist – als Gegenstand oder im Ping-Monitor."),
    (r"roborock|rockrobo", "Saugroboter", "Roborock", "cloud",
     "Lokal nur mit Xiaomi-Gerätetoken steuerbar (über die Mi-Home-App) – sonst Roborock-Cloud."),
    (r"_miio\._udp|xiaomi|zhimi|chuangmi|yeelink|lumi-", "Xiaomi-Gerät", "Xiaomi", "lokal",
     "Xiaomi-Geräte lassen sich lokal steuern, brauchen aber einen Gerätetoken aus der Mi-Home-App."),
    (r"shelly", "Schalter/Steckdose", "Shelly", "lokal", "Shelly hat eine offene lokale Schnittstelle – ideal."),
    (r"tasmota", "Schalter/Steckdose", "Tasmota", "lokal", "Tasmota ist lokal per HTTP steuerbar."),
    (r"_wled\._tcp|wled", "Licht (LED)", "WLED", "lokal", "WLED ist lokal per HTTP/JSON steuerbar."),
    (r"_esphomelib\._tcp|esphome", "Smart-Gerät (ESPHome)", "ESPHome", "lokal", "ESPHome-Geräte sind lokal steuerbar."),
    (r"_hue\._tcp|ipbridge|philips.?hue|hue bridge", "Licht (Bridge)", "Philips Hue", "lokal",
     "Die Hue-Bridge ist lokal steuerbar (einmal den Knopf auf der Bridge drücken)."),
    (r"_sonos\._tcp|sonos", "Lautsprecher", "Sonos", "lokal", "Sonos ist lokal steuerbar (Lautstärke, Play/Pause)."),
    (r"_googlecast\._tcp|chromecast|google-home|google-nest", "Lautsprecher/TV", "Google", "erkannt",
     "Google Cast – Steuerung eingeschränkt, Anwesenheit/Status möglich."),
    (r"_amzn-wplay|amazon-|echo-|fire-tv|kindle", "Alexa/Fire TV", "Amazon", "cloud", "Amazon-Geräte laufen über die Cloud."),
    (r"_hap\._tcp|_homekit\._tcp", "HomeKit-Gerät", "", "erkannt",
     "Spricht Apple HomeKit – lokal, aber mit Kopplung; nicht von Hauskern steuerbar."),
    (r"_matter", "Matter-Gerät", "", "erkannt", "Matter-Geräte brauchen eine eigene Kopplung."),
    (r"_bambulab|bambu|\bx1c\b|\bp1s\b|\bx2d\b|\bh2d\b|\ba1\b", "3D-Drucker", "Bambu Lab", "drin", ""),
    (r"tapo.*c\d{2,3}|c200|c210|c21a|c225|c310|c320", "Kamera", "TP-Link Tapo", "drin", ""),
    (r"tapo|p100|p105|p110|p115|l510|l530|l900|kp105|kp115|hs1\d\d", "Steckdose/Licht", "TP-Link Tapo/Kasa", "cloud",
     "Tapo-Steckdosen/-Lampen brauchen die Tapo-Kontodaten und eine Verschlüsselung (KLAP) – nur eingeschränkt lokal."),
    (r"fritz|avm", "Router/Repeater", "AVM", "drin", ""),
    (r"_ipp\._tcp|_printer\._tcp|brother|canon|epson|hp[a-f0-9]{6}|officejet|laserjet", "Drucker (Papier)", "", "erkannt", ""),
    (r"nuki", "Türschloss", "Nuki", "lokal", "Die Nuki-Bridge hat eine lokale HTTP-Schnittstelle (Token nötig)."),
    (r"tado", "Heizung", "tado°", "cloud", "tado° läuft über die Cloud."),
    (r"netatmo", "Wetter/Heizung", "Netatmo", "cloud", "Netatmo läuft über die Cloud."),
    (r"ring-|ringpro|doorbell", "Türklingel", "", "cloud", "Klingeln laufen meist über die Hersteller-Cloud."),
    (r"eufy|homebase", "Kamera", "eufy", "cloud", "eufy läuft meist über die Cloud."),
    (r"samsung|tizen|\[tv\]", "Fernseher", "Samsung", "lokal", "Samsung-TVs sind lokal (Ein/Aus, Lautstärke) ansprechbar."),
    (r"lgwebos|webos|lg-tv", "Fernseher", "LG", "lokal", "LG webOS ist lokal steuerbar (einmal am TV bestätigen)."),
    (r"_airplay\._tcp|_raop\._tcp|apple-?tv|homepod", "AirPlay (Apple TV/HomePod)", "Apple", "erkannt", ""),
    (r"watch", "Smartwatch", "", "erkannt", "Kann als Gegenstand für die Anwesenheit genutzt werden."),
    (r"iphone|ipad|android|galaxy|pixel|redmi|oneplus|xiaomi-?\d|huawei|handy|smartphone", "Handy/Tablet", "",
     "erkannt", "Kann als Person für die Anwesenheit genutzt werden."),
    (r"tesla|\bbmw|vw-|skoda|mercedes|audi|\bford\b|\bcar\b|\bauto\b", "Auto", "", "erkannt",
     "Bucht sich das Auto ins WLAN ein, kann es als Gegenstand („Auto ist da“) dienen."),
    (r"desktop-|laptop-|\bpc\b|-pc\b|^pc-|gaming|workstation|\bnas\b|synology|qnap|macbook|imac|mac-?mini|mac-?studio", "Computer/NAS", "", "erkannt",
     "Kann per Wake-on-LAN aufgeweckt werden (Geräte → ⏻ Aufwecken)."),
    (r"^esp[_-]|espressif|esp32|esp8266", "Smart-Gerät (ESP-Modul)", "unbekannt", "erkannt",
     "Typischer Chip in günstigen Smart-Home-Geräten. Hersteller unbekannt – oft Tuya/Smart Life (Cloud)."),
    (r"tuya|smartlife|smart-life", "Smart-Gerät", "Tuya/Smart Life", "cloud", "Tuya läuft über die Cloud."),
]


# --------------------------------------------------------------------------- #
# mDNS
# --------------------------------------------------------------------------- #
def _qname(name):
    out = b""
    for part in name.strip(".").split("."):
        b = part.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"


def build_query(types):
    qs = b"".join(_qname(t + ".local") + struct.pack("!HH", 12, 1) for t in types)  # PTR, IN
    return struct.pack("!HHHHHH", 0, 0, len(types), 0, 0, 0) + qs


def _read_name(data, off, depth=0):
    labels, jumped, end = [], False, off
    while off < len(data) and depth < 20:
        ln = data[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped = True
            off, depth = ptr, depth + 1
            continue
        labels.append(data[off + 1:off + 1 + ln].decode("utf-8", "replace"))
        off += 1 + ln
    return ".".join(labels), (end if jumped else off)


def parse_mdns(data):
    """Liefert {"services": set, "names": set, "hosts": set, "txt": {}} aus einer mDNS-Antwort."""
    res = {"services": set(), "names": set(), "hosts": set(), "txt": {}}
    try:
        _id, _fl, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        off = 12
        for _ in range(qd):
            _, off = _read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _read_name(data, off)
            typ, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            rd = off
            off += rdlen
            if typ == 12:  # PTR
                target, _ = _read_name(data, rd)
                svc = re.search(r"(_[\w-]+\._(?:tcp|udp))", name)
                if svc and svc.group(1) != "_services._dns-sd._udp":
                    res["services"].add(svc.group(1))
                    inst = target.split("._")[0]
                    if inst:
                        res["names"].add(inst)
                elif svc:
                    s2 = re.search(r"(_[\w-]+\._(?:tcp|udp))", target)
                    if s2:
                        res["services"].add(s2.group(1))
            elif typ == 33:  # SRV
                host, _ = _read_name(data, rd + 6)
                res["hosts"].add(host.replace(".local", ""))
                svc = re.search(r"(_[\w-]+\._(?:tcp|udp))", name)
                if svc:
                    res["services"].add(svc.group(1))
                res["names"].add(name.split("._")[0])
            elif typ == 16:  # TXT
                p = rd
                while p < rd + rdlen:
                    ln = data[p]
                    kv = data[p + 1:p + 1 + ln].decode("utf-8", "replace")
                    p += 1 + ln
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        res["txt"][k.lower()] = v
    except (struct.error, IndexError):
        pass
    return res


def mdns_scan(timeout=3.0):
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.bind(("", 0))
        s.settimeout(0.4)
        q = build_query(MDNS_TYPES)
        for _ in range(2):
            s.sendto(q, MDNS_ADDR)
            time.sleep(0.1)
    except OSError:
        return found
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        r = parse_mdns(data)
        cur = found.setdefault(addr[0], {"services": set(), "names": set(), "hosts": set(), "txt": {}})
        for k in ("services", "names", "hosts"):
            cur[k] |= r[k]
        cur["txt"].update(r["txt"])
    s.close()
    return found


# --------------------------------------------------------------------------- #
# SSDP
# --------------------------------------------------------------------------- #
def parse_ssdp(data):
    out = {}
    for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def ssdp_scan(timeout=3.0):
    found = {}
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: ssdp:all\r\n\r\n").encode()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.4)
        for _ in range(2):
            s.sendto(msg, SSDP_ADDR)
    except OSError:
        return found
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        h = parse_ssdp(data)
        cur = found.setdefault(addr[0], {"server": set(), "st": set(), "location": ""})
        if h.get("server"):
            cur["server"].add(h["server"])
        if h.get("st"):
            cur["st"].add(h["st"])
        if h.get("location") and not cur["location"]:
            cur["location"] = h["location"]
    s.close()
    return found


def _http(url, timeout=2.0, limit=65536):
    req = urllib.request.Request(url, headers={"User-Agent": "Hauskern"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read(limit)


def upnp_info(location):
    """friendlyName/manufacturer/modelName aus der UPnP-Beschreibung (nur Heimnetz-Adressen)."""
    if not re.match(r"https?://(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.)", location or ""):
        return {}
    try:
        x = _http(location).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    g = lambda t: (re.search(rf"<{t}>([^<]*)</{t}>", x) or [None, ""])[1].strip()  # noqa: E731
    return {"friendly": g("friendlyName"), "manufacturer": g("manufacturer"), "model": g("modelName")}


def http_probe(ip):
    """Kurze Proben auf bekannte lokale Schnittstellen. Gibt (Regel-Text, Modell) oder None zurück."""
    for path, key in (("/shelly", "shelly"), ("/json/info", "wled"), ("/cm?cmnd=Status", "tasmota")):
        try:
            d = json.loads(_http(f"http://{ip}{path}", 1.5).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        if key == "shelly" and isinstance(d, dict) and ("type" in d or "gen" in d or "model" in d):
            return "shelly", d.get("model") or d.get("type") or "Shelly"
        if key == "wled" and isinstance(d, dict) and ("leds" in d or str(d.get("brand", "")).upper() == "WLED"):
            return "wled", d.get("name") or "WLED"
        if key == "tasmota" and isinstance(d, dict) and "Status" in d:
            return "tasmota", (d["Status"].get("DeviceName") or "Tasmota")
    return None


def _port_open(ip, port, t=0.6):
    try:
        with socket.create_connection((ip, port), timeout=t):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Zusammenführen & erkennen
# --------------------------------------------------------------------------- #
def identify(text):
    t = text.lower()
    for rx, cat, vendor, status, hint in RULES:
        if re.search(rx, t):
            return {"category": cat, "vendor": vendor, "status": status, "hint": hint, "rule": rx}
    return None


def scan(hosts, known=None, timeout=3.0, probe=True, progress=None):
    """hosts: FRITZ!Box-Geräteliste. known: {ip: "Beschreibung"} – schon in Hauskern eingebunden."""
    known = known or {}
    progress = progress or (lambda t: None)
    progress("Suche per Bonjour/mDNS und UPnP …")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fm, fs = ex.submit(mdns_scan, timeout), ex.submit(ssdp_scan, timeout)
        mdns, ssdp = fm.result(), fs.result()
    by_ip = {}
    for h in hosts:
        if h.get("active") and h.get("ip"):
            by_ip[h["ip"]] = {"ip": h["ip"], "mac": h.get("mac", ""), "name": h.get("name", ""), "via": ["FRITZ!Box"]}
    for ip in set(mdns) | set(ssdp):
        by_ip.setdefault(ip, {"ip": ip, "mac": "", "name": "", "via": []})
    locs = {ip: s["location"] for ip, s in ssdp.items() if s.get("location")}
    progress("Lese Gerätebeschreibungen …")
    with ThreadPoolExecutor(max_workers=12) as ex:
        upnp = dict(zip(locs, ex.map(upnp_info, locs.values())))
    out = []
    unknown = []
    for ip, d in by_ip.items():
        m, s, u = mdns.get(ip, {}), ssdp.get(ip, {}), upnp.get(ip, {})
        if m:
            d["via"].append("mDNS")
        if s:
            d["via"].append("UPnP")
        parts = [d["name"]] + sorted(m.get("services", [])) + sorted(m.get("names", [])) + sorted(m.get("hosts", [])) + \
            sorted(s.get("server", [])) + sorted(s.get("st", [])) + [u.get("friendly", ""), u.get("manufacturer", ""),
                                                                    u.get("model", ""), m.get("txt", {}).get("md", "")]
        text = " ".join(p for p in parts if p)
        hit = identify(text)
        model = u.get("model") or m.get("txt", {}).get("md") or m.get("txt", {}).get("model") or ""
        if not d["name"]:
            d["name"] = u.get("friendly") or next(iter(sorted(m.get("names", []))), "") or ip
        d.update(model=model, services=sorted(m.get("services", [])), server=" ".join(sorted(s.get("server", [])))[:120])
        if ip in known:
            info = identify(text) or {}
            d.update(category=info.get("category") or "Gerät", vendor=info.get("vendor") or u.get("manufacturer", ""),
                     status="drin", hint=known[ip])
        elif hit:
            d.update(category=hit["category"], vendor=hit["vendor"] or u.get("manufacturer", ""), status=hit["status"],
                     hint=hit["hint"])
            if hit["status"] == "drin":
                d["status"], d["hint"] = "erkannt", "Kann Hauskern grundsätzlich – noch nicht eingerichtet."
            if hit["rule"].startswith("^esp"):
                unknown.append(d)  # ESP-Chip: könnte Shelly/Tasmota/WLED sein – genauer nachsehen
        else:
            unknown.append(d)
            d.update(category="", vendor=u.get("manufacturer", ""), status="", hint="")
        out.append(d)
    if probe and unknown:
        progress(f"Prüfe {len(unknown)} unbekannte Geräte …")

        def pr(d):
            if not _port_open(d["ip"], 80):
                return d, None
            return d, http_probe(d["ip"])
        with ThreadPoolExecutor(max_workers=16) as ex:
            for d, res in ex.map(pr, unknown):
                if res:
                    hit = identify(res[0])
                    d.update(category=hit["category"], vendor=hit["vendor"], status=hit["status"], hint=hit["hint"],
                             model=res[1] or d.get("model", ""))
                    d["via"].append("HTTP")
    for d in out:
        if not d["status"]:
            d.update(category="Unbekannt", status="", hint="")
    order = {"lokal": 0, "cloud": 1, "erkannt": 2, "drin": 3, "": 4}
    out.sort(key=lambda d: (order.get(d["status"], 9), d["category"], d["name"].lower()))
    return out


# --------------------------------------------------------------------------- #
# Einordnen beim Hinzufügen: Was ist das, ist es eine Person, gehört noch was dazu?
# --------------------------------------------------------------------------- #
PHONE_CATS = {"Handy/Tablet"}
HUB_WORDS = ("Basisstation", "Gateway", "Zentrale", "Bridge", "Hub")
# zu allgemein, um daraus „gehört zusammen“ zu schließen
BROAD_CATS = {"", "Unbekannt", "Handy/Tablet", "Computer/NAS", "Router/Repeater", "Smart-Gerät (ESP-Modul)",
              "Smart-Gerät", "HomeKit-Gerät", "Matter-Gerät", "Drucker (Papier)", "Smartwatch", "Auto"}
GENERIC_STEMS = {"iphone", "ipad", "android", "galaxy", "desktop", "laptop", "unknown", "unbekannt", "fritz", "amazon",
                 "echo", "google", "apple", "samsung", "windows", "smart", "device", "geraet", "gerät", "host", "pixel",
                 "client", "wlan", "redmi", "xiaomi", "huawei", "oneplus", "macbook", "localhost", "mobile", "phone",
                 "tablet", "watch", "galaxy", "espressif", "esp32", "esp8266", "tasmota", "shelly", "tapo", "wled",
                 "speaker", "living", "kitchen", "bedroom", "office"}


def is_private_mac(mac):
    """Zufällige („private“) WLAN-Adresse? Handys, Tablets und Uhren benutzen die gerne."""
    m = (mac or "").replace("-", ":").strip()
    try:
        return bool(int(m.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def is_hub(category):
    return any(w in (category or "") for w in HUB_WORDS)


def classify(name, mac="", disc=None):
    """Ordnet ein Gerät ein. disc: Treffer aus der Gerätesuche (falls vorhanden – genauer als nur der Name).
    Gibt {category, vendor, hint, status, role ("person"|"item"), hub, why} zurück."""
    d = dict(disc or {})
    if d.get("category") and d["category"] != "Unbekannt":
        out = {"category": d["category"], "vendor": d.get("vendor", ""), "hint": d.get("hint", ""),
               "status": d.get("status", "")}
    else:
        hit = identify(name or "") or {}
        out = {"category": hit.get("category", ""), "vendor": hit.get("vendor", "") or d.get("vendor", ""),
               "hint": hit.get("hint", ""), "status": hit.get("status", "")}
    why = []
    if out["category"] in PHONE_CATS:
        role = "person"
        why.append("Name sieht nach Handy/Tablet aus")
    elif not out["category"] and is_private_mac(mac):
        role = "person"
        out["category"] = "Handy/Tablet/Uhr (vermutlich)"
        why.append("benutzt eine private WLAN-Adresse – typisch für Handys, Tablets und Uhren")
    else:
        role = "item"
    out.update(role=role, hub=is_hub(out["category"]), why=why)
    return out


def _stem(name):
    for tok in re.split(r"[-_.\s]+", (name or "").lower()):
        tok = re.sub(r"\d+$", "", tok)
        if len(tok) >= 4 and tok not in GENERIC_STEMS and not re.fullmatch(r"[0-9a-f]{4,}", tok):
            return tok
        return ""
    return ""


def _part_of(hub_cat, cat):
    """„Rauchmelder“ gehört zur „Rauchmelder-Basisstation“, „Kamera“ zur „Kamera-Basisstation“ …"""
    if not is_hub(hub_cat) or not cat or is_hub(cat):
        return False
    return hub_cat.lower().startswith(cat.lower().split(" ")[0])


def related(target, others):
    """Welche anderen Geräte im Netz gehören vermutlich dazu?
    target/others: {"mac","name","category","vendor"}. Gibt [(other, grund)] zurück (ohne target)."""
    tmac = (target.get("mac") or "").upper()
    tcat, tven = target.get("category", ""), (target.get("vendor") or "").lower()
    tstem = _stem(target.get("name"))
    out = []
    for o in others:
        if (o.get("mac") or "").upper() == tmac or not o.get("mac"):
            continue
        ocat, oven = o.get("category", ""), (o.get("vendor") or "").lower()
        reason = ""
        if tven and tven == oven and tven not in ("unbekannt", "apple", "google", "amazon", "samsung") \
                and tcat not in BROAD_CATS and ocat not in BROAD_CATS:
            reason = f"auch {o.get('vendor')}"
        elif tcat and tcat == ocat and tcat not in BROAD_CATS:
            reason = f"auch {tcat}"
        elif _part_of(tcat, ocat) or _part_of(ocat, tcat):
            reason = f"{ocat} – gehört oft zur Station"
        elif tstem and _stem(o.get("name")) == tstem and ocat not in PHONE_CATS:
            reason = f"ähnlicher Name („{tstem}…“)"
        if reason:
            out.append((o, reason))
    return out
