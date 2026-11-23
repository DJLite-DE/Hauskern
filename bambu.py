# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
bambu.py – Lokale Verbindung zu Bambu-Lab-Druckern (X2D, H2D, X1, P1, A1 …) per MQTT über TLS.
Kleiner MQTT-3.1.1-Client nur mit der Python-Standardbibliothek.

Benötigt: Drucker-IP, Seriennummer und LAN-Zugangscode (am Druckerdisplay unter WLAN/Netzwerk).
Status lesen klappt im Heimnetz mit dem Zugangscode. Steuern (Pause/Stopp) verlangt bei aktueller
Firmware meist „LAN-Modus“ + „Entwicklermodus“ am Drucker.
"""
import json
import socket
import ssl
import struct
import threading
import time

PORT = 8883


def _s(text):
    b = text.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _remlen(n):
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _unpack_temp(v):
    """H2D/X2D liefern Temperaturen gepackt: untere 16 Bit = Ist, obere 16 Bit = Soll."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None, None
    return v & 0xFFFF, (v >> 16) & 0xFFFF


STATE_TEXT = {
    "IDLE": "Bereit", "PREPARE": "Bereitet vor", "RUNNING": "Druckt", "PAUSE": "Pausiert",
    "FINISH": "Fertig", "FAILED": "Fehlgeschlagen", "SLICING": "Slicen", "INIT": "Startet",
}


class BambuPrinter:
    def __init__(self, name, ip, serial, code, on_update):
        self.name, self.ip, self.serial, self.code = name, ip, serial.strip(), code.strip()
        self.on_update = on_update  # callback(serial, summary_dict) – aus Hintergrund-Thread!
        self.raw = {}
        self.connected = False
        self.error = ""
        self._sock = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._was_connected = False

    # ---------------- Lebenszyklus ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _run(self):
        backoff = 3
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:  # Verbindung weg, Code falsch, Drucker aus …
                if self._stop.is_set():
                    return
                self.connected = False
                self.error = self._explain(e)
                self._emit()
            finally:
                try:
                    if self._sock:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None
            if self._was_connected:
                backoff, self._was_connected = 3, False
            self._stop.wait(backoff)
            backoff = min(60, backoff * 2)

    @staticmethod
    def _explain(e):
        msg = str(e)
        if isinstance(e, (socket.timeout, TimeoutError)):
            return "Drucker antwortet nicht (aus oder falsche IP?)"
        if isinstance(e, ConnectionRefusedError):
            return "Verbindung abgelehnt – LAN-Zugriff am Drucker aktiv?"
        if "CONNACK" in msg:
            return msg.replace("CONNACK: ", "")
        return f"Verbindung getrennt ({type(e).__name__})"

    # ---------------- MQTT ----------------
    def _send(self, data):
        with self._lock:
            if not self._sock:
                raise ConnectionError("nicht verbunden")
            self._sock.sendall(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Verbindung geschlossen")
            buf += chunk
        return buf

    def _read_packet(self):
        head = self._recv_exact(1)[0]
        mult, length = 1, 0
        while True:
            b = self._recv_exact(1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return head, (self._recv_exact(length) if length else b"")

    def _session(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Drucker nutzt ein selbstsigniertes Zertifikat (nur im Heimnetz)
        raw = socket.create_connection((self.ip, PORT), timeout=8)
        self._sock = ctx.wrap_socket(raw, server_hostname=self.ip)
        cid = f"homeapp-{int(time.time()) % 100000}"
        var = _s("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
        payload = _s(cid) + _s("bblp") + _s(self.code)
        self._send(bytes([0x10]) + _remlen(len(var) + len(payload)) + var + payload)
        head, body = self._read_packet()
        if head >> 4 != 2 or len(body) < 2 or body[1] != 0:
            code = body[1] if len(body) > 1 else -1
            raise ConnectionError("CONNACK: Zugangscode falsch" if code in (4, 5) else f"CONNACK: Fehler {code}")
        topic = f"device/{self.serial}/report"
        sub = struct.pack("!H", 1) + _s(topic) + b"\x00"
        self._send(bytes([0x82]) + _remlen(len(sub)) + sub)
        self.connected, self.error, self._was_connected = True, "", True
        self.request_full()
        self._sock.settimeout(5)
        last_ping, last_full = time.time(), time.time()
        while not self._stop.is_set():
            try:
                head, body = self._read_packet()
            except (socket.timeout, TimeoutError):
                head = None
            now = time.time()
            if now - last_ping > 30:
                self._send(b"\xC0\x00")  # PINGREQ
                last_ping = now
            if now - last_full > 300:  # ab und zu kompletten Stand holen
                self.request_full()
                last_full = now
            if head is None or head >> 4 != 3:
                continue
            tlen = struct.unpack("!H", body[:2])[0]
            pos = 2 + tlen + (2 if (head >> 1) & 3 else 0)
            try:
                msg = json.loads(body[pos:].decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(msg.get("print"), dict):
                _merge(self.raw, msg["print"])
                self._emit()

    def publish(self, obj):
        topic = f"device/{self.serial}/request"
        data = _s(topic) + json.dumps(obj).encode()
        self._send(bytes([0x30]) + _remlen(len(data)) + data)

    def request_full(self):
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}})

    # ---------------- Befehle ----------------
    def pause(self):
        self.publish({"print": {"sequence_id": "0", "command": "pause", "param": ""}})

    def resume(self):
        self.publish({"print": {"sequence_id": "0", "command": "resume", "param": ""}})

    def stop_print(self):
        self.publish({"print": {"sequence_id": "0", "command": "stop", "param": ""}})

    def set_light(self, on):
        self.publish({"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                                 "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                                 "loop_times": 0, "interval_time": 0}})

    # ---------------- Auswertung ----------------
    def summary(self):
        r = self.raw
        st = r.get("gcode_state", "")
        s = {
            "name": self.name, "connected": self.connected, "error": self.error,
            "state": st, "state_text": STATE_TEXT.get(st, st.title() if st else "–"),
            "percent": r.get("mc_percent"), "remaining_min": r.get("mc_remaining_time"),
            "layer": r.get("layer_num"), "layers": r.get("total_layer_num"),
            "file": r.get("subtask_name") or r.get("gcode_file") or "",
            "nozzles": [], "bed": None, "bed_target": None, "chamber": None,
            "light": None, "ams": [], "print_error": r.get("print_error") or 0,
        }
        import printwatch
        s["hms"] = printwatch.hms_codes(r.get("hms"))
        s["xcam"] = printwatch.xcam_info(r.get("xcam"))
        # Düsen: X2D/H2D (zwei Düsen, gepackt) oder klassisch
        ext = (r.get("device") or {}).get("extruder", {}).get("info")
        if isinstance(ext, list) and ext:
            for e in ext:
                cur, tgt = _unpack_temp(e.get("temp"))
                if cur is not None:
                    s["nozzles"].append((cur, tgt))
        elif r.get("nozzle_temper") is not None:
            s["nozzles"].append((r.get("nozzle_temper"), r.get("nozzle_target_temper")))
        bed = (r.get("device") or {}).get("bed", {}).get("info", {}).get("temp")
        if bed is not None:
            s["bed"], s["bed_target"] = _unpack_temp(bed)
        else:
            s["bed"], s["bed_target"] = r.get("bed_temper"), r.get("bed_target_temper")
        ch = (r.get("device") or {}).get("ctc", {}).get("info", {}).get("temp")
        if ch is not None:
            s["chamber"] = _unpack_temp(ch)[0]
        elif r.get("chamber_temper") is not None:
            s["chamber"] = r.get("chamber_temper")
        for l in r.get("lights_report") or []:
            if l.get("node") == "chamber_light":
                s["light"] = l.get("mode") == "on"
        ams = (r.get("ams") or {})
        active = str(ams.get("tray_now", ""))
        for unit in ams.get("ams") or []:
            for tray in unit.get("tray") or []:
                tid = str(int(unit.get("id", 0)) * 4 + int(tray.get("id", 0)))
                color = tray.get("tray_color") or ""
                s["ams"].append({
                    "color": "#" + color[:6] if len(color) >= 6 else None,
                    "type": tray.get("tray_type") or "", "active": tid == active,
                })
        s["ams_units"] = parse_ams(r)
        return s

    def _emit(self):
        try:
            self.on_update(self.serial, self.summary())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# AMS (automatisches Materialsystem)
# --------------------------------------------------------------------------- #
def _int(v, d=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _tray(t, slot_label, active):
    """Ein Fach. remain = Restmenge in % (nur bei Bambu-Rollen mit RFID, sonst -1 = unbekannt)."""
    ttype = (t.get("tray_type") or "").strip()
    color = t.get("tray_color") or ""
    remain = _int(t.get("remain"), -1)
    weight = _int(t.get("tray_weight"), 0) or 0
    grams = None
    if t.get("remain_g") not in (None, "", -1):
        grams = _int(t.get("remain_g"))
    elif remain is not None and remain >= 0 and weight > 0:
        grams = round(weight * remain / 100)
    return {"slot": slot_label, "empty": not ttype, "type": ttype, "brand": (t.get("tray_sub_brands") or "").strip(),
            "color": "#" + color[:6].lower() if len(color) >= 6 else None,
            "remain": remain if remain is not None and remain >= 0 else None, "grams": grams,
            "weight": weight or None, "active": active, "rfid": (t.get("tag_uid") or "0").strip("0") != "",
            "uuid": t.get("tray_uuid") or ""}


def parse_ams(r):
    """Alle AMS-Einheiten mit Fächern, Feuchte, Temperatur und Trocknung – plus externe Spule."""
    ams = r.get("ams") or {}
    now = str(ams.get("tray_now", ""))
    units = []
    for u in ams.get("ams") or []:
        uid = _int(u.get("id"), 0)
        if uid >= 128:  # AMS HT: ein Fach, eigene Nummern ab 128
            kind, number = "AMS HT", uid - 127
        elif "dry_time" in u or "humidity_raw" in u:
            kind, number = "AMS 2 Pro" if "dry_time" in u else "AMS", uid + 1
        else:
            kind, number = "AMS", uid + 1
        trays = []
        for t in u.get("tray") or []:
            tid = _int(t.get("id"), 0)
            cum = str(uid * 4 + tid) if uid < 128 else str(uid)
            trays.append(_tray(t, f"{chr(65 + tid) if uid < 128 else 'HT'}", cum == now))
        hum_raw = _int(u.get("humidity_raw"))
        temp = u.get("temp")
        try:
            temp = round(float(temp), 1) if temp not in (None, "", "0.0") else None
        except (TypeError, ValueError):
            temp = None
        dry = _int(u.get("dry_time"), 0) or 0
        units.append({"id": uid, "name": f"{kind} {number}", "kind": kind,
                      "humidity": _int(u.get("humidity")), "humidity_pct": hum_raw, "temp": temp,
                      "drying_min": dry if 0 < dry < 100000 else 0, "trays": trays})
    vt = r.get("vt_tray")
    if isinstance(vt, dict) and (vt.get("tray_type") or "").strip():
        units.append({"id": 254, "name": "Externe Spule", "kind": "extern", "humidity": None, "humidity_pct": None,
                      "temp": None, "drying_min": 0, "trays": [_tray(vt, "Ext", now == "254")]})
    return units


def humidity_text(u):
    if u.get("humidity_pct") is not None:
        return f"{u['humidity_pct']} % Luftfeuchte"
    if u.get("humidity"):
        return f"Feuchte-Stufe {u['humidity']}/5"
    return ""
