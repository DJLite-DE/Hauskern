# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
mqtt.py – kleiner MQTT-Client (3.1.1, QoS 0) nur mit Python-Bordmitteln.

    c = Client(host, port=1883, user="", pw="", on_message=fn(topic, payload_bytes), subs=["zigbee2mqtt/#"])
    c.start()            # verbindet im Hintergrund und verbindet sich bei Abbruch neu
    c.publish(topic, payload)
    c.connected / c.error
"""
import os
import socket
import ssl
import struct
import threading
import time


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


class Client:
    def __init__(self, host, port=1883, user="", pw="", on_message=None, subs=(), tls=False, keepalive=30):
        self.host, self.port, self.user, self.pw, self.tls = host, int(port or 1883), user or "", pw or "", tls
        self.on_message = on_message or (lambda t, p: None)
        self.on_connect = lambda: None
        self.subs, self.keepalive = list(subs), keepalive
        self.connected, self.error = False, ""
        self._sock, self._lock = None, threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._run, daemon=True, name=f"MQTT {self.host}")
            self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    # ---------------- senden ----------------
    def _send(self, data):
        with self._lock:
            if not self._sock:
                raise ConnectionError("MQTT nicht verbunden")
            self._sock.sendall(data)

    def publish(self, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode()
        data = _s(topic) + payload
        self._send(bytes([0x30 | (1 if retain else 0)]) + _remlen(len(data)) + data)

    # ---------------- empfangen ----------------
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
            if mult > 128 ** 4:
                raise ConnectionError("MQTT: kaputtes Paket")
        return head, (self._recv_exact(length) if length else b"")

    def _session(self):
        raw = socket.create_connection((self.host, self.port), timeout=8)
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # nur im Heimnetz
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self._sock = raw
        flags = 0x02 | (0x80 if self.user else 0) | (0x40 if self.pw else 0)
        var = _s("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        payload = _s(f"hauskern-{os.urandom(3).hex()}")
        if self.user:
            payload += _s(self.user)
        if self.pw:
            payload += _s(self.pw)
        self._send(bytes([0x10]) + _remlen(len(var) + len(payload)) + var + payload)
        head, body = self._read_packet()
        if head >> 4 != 2 or len(body) < 2 or body[1] != 0:
            code = body[1] if len(body) > 1 else -1
            raise ConnectionError("Benutzer oder Passwort falsch" if code in (4, 5) else f"MQTT abgelehnt ({code})")
        for i, t in enumerate(self.subs):
            sub = struct.pack("!H", i + 1) + _s(t) + b"\x00"
            self._send(bytes([0x82]) + _remlen(len(sub)) + sub)
        self.connected, self.error = True, ""
        try:
            self.on_connect()
        except Exception:  # noqa: BLE001
            pass
        self._sock.settimeout(5)
        last_ping = time.time()
        while not self._stop.is_set():
            try:
                head, body = self._read_packet()
            except (socket.timeout, TimeoutError):
                head = None
            if time.time() - last_ping > self.keepalive * 0.6:
                self._send(b"\xC0\x00")  # PINGREQ
                last_ping = time.time()
            if head is None or head >> 4 != 3:
                continue
            tlen = struct.unpack("!H", body[:2])[0]
            topic = body[2:2 + tlen].decode("utf-8", "replace")
            pos = 2 + tlen + (2 if (head >> 1) & 3 else 0)
            try:
                self.on_message(topic, body[pos:])
            except Exception:  # noqa: BLE001 – eine kaputte Nachricht darf nichts lahmlegen
                pass

    def _run(self):
        delay = 2
        while not self._stop.is_set():
            try:
                self._session()
                delay = 2
            except Exception as e:  # noqa: BLE001
                self.error = str(e) or type(e).__name__
            self.connected = False
            try:
                if self._sock:
                    self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._stop.wait(delay)
            delay = min(60, delay * 2)
