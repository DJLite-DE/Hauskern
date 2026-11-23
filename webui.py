# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
webui.py – Handy-Oberfläche für Hauskern (im WLAN oder per FRITZ!Box-VPN).

Sicherheit:
  * nur Zugriffe aus dem Heimnetz / VPN (private Adressen) – aus dem Internet wird abgelehnt
  * PIN-Login, Sitzung per Cookie (HttpOnly, SameSite=Strict), nach 5 Fehlversuchen 5 Minuten Sperre
  * PIN und Sitzungen werden nur als Hash gespeichert
Die Daten (state) und Aktionen laufen über Callbacks der App – der Server fasst Tk nie direkt an.
"""
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import re
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("fm")

import users as U

COOKIE = "hk_session"
SESSION_DAYS = 60


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def check_pin(pin, stored):
    try:
        salt, _ = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(hash_pin(pin, salt), stored)


def _tok_hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def _call(fn, *args):
    """Ruft fn mit so vielen Argumenten auf, wie es annimmt (ältere Schnittstellen ohne Nutzer)."""
    try:
        n = len([p for p in inspect.signature(fn).parameters.values()
                 if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        n = len(args)
    return fn(*args[:n])


def is_local(addr):
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class WebServer:
    """api: get_state() -> dict, do_action(dict) -> (ok, msg), cam_png(key) -> bytes|None,
            pin_hash() -> str, sessions() -> set, add_session(hash), app_name, assets_dir"""

    def __init__(self, api, port=8770):
        self.api, self.port = api, int(port)
        self.httpd = None
        self.error = ""
        self._fails = {}

    def start(self):
        self.stop()
        handler = self._make_handler()
        for host in ("::", "0.0.0.0"):
            try:
                cls = ThreadingHTTPServer
                if host == "::":
                    class V6(ThreadingHTTPServer):
                        address_family = __import__("socket").AF_INET6

                        def server_bind(self):
                            import socket
                            try:
                                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                            except OSError:
                                pass
                            super().server_bind()
                    cls = V6
                self.httpd = cls((host, self.port), handler)
                break
            except OSError as e:
                self.error = str(e)
                self.httpd = None
        if not self.httpd:
            return False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="Handy-Server", daemon=True).start()
        self.error = ""
        return True

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None

    @property
    def running(self):
        return self.httpd is not None

    # ------------------------------------------------------------------ #
    def _make_handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            server_version = "Hauskern"

            def log_message(self, *a):
                pass

            # ---------- Hilfen ----------
            def _send(self, code, body=b"", ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store" if ctype != "image/png" or code != 200 else "no-cache")
                extra = dict(extra or {})
                tok = getattr(self, "_slide", None)
                if tok and "Set-Cookie" not in extra and code < 400:  # Anmeldung verlängert sich bei Benutzung
                    extra["Set-Cookie"] = f"{COOKIE}={tok}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Strict"
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _client(self):
                return self.client_address[0]

            def _user(self):
                raw = self.headers.get("Cookie", "")
                for part in raw.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE and v:
                        h = _tok_hash(v)
                        if hasattr(srv.api, "session_user"):
                            u = srv.api.session_user(h)
                            if u:
                                if urlparse(self.path).path == "/api/state":
                                    self._slide = v  # höchstens bei der Zustandsabfrage erneuern
                                return u
                        elif h in srv.api.sessions():
                            return {"id": "u0", "name": "", "admin": True}
                return None

            def _authed(self):
                return self._user() is not None

            def _send_file(self, path, ctype):
                """Datei streamen – mit Range (iPhone/Safari spielt Videos nur so ab)."""
                size = os.path.getsize(path)
                start, end = 0, size - 1
                rng = self.headers.get("Range", "")
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                partial = bool(m and (m.group(1) or m.group(2)))
                if partial:
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:  # die letzten N Bytes
                        start = max(0, size - int(m.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        left -= len(chunk)

            def _siri(self, key, text):
                """Antwort als reiner Text – den liest der Kurzbefehl vor."""
                usr = _call(srv.api.siri_user, key) if hasattr(srv.api, "siri_user") else None
                if not usr:
                    time.sleep(1.0)
                    return self._send(403, "Der Schlüssel passt nicht. Bitte den Kurzbefehl in Hauskern neu einrichten.",
                                      "text/plain; charset=utf-8")
                from urllib.parse import unquote_plus
                for _ in range(2):  # doppelt codiert (Kurzbefehl codiert selbst + „URL codieren“)? → auspacken
                    if re.search(r"%[0-9A-Fa-f]{2}", text):
                        text = unquote_plus(text)
                return self._send(200, srv.api.siri_command(usr, text[:300]), "text/plain; charset=utf-8")

            def _admin_ok(self, usr, section, write):
                if usr.get("admin"):
                    return True
                import webadmin
                a = webadmin.SECTION_AREA.get(section)
                if a and a[0] == "*":  # gilt für jeden angemeldeten Nutzer (nur die eigenen Daten)
                    return True
                return bool(a) and U.can(usr, a[0], a[2] if write else a[1])

            def _bridge_ok(self):
                """Matter-Bridge (Alexa/Google) – eigener Schlüssel, nur aus dem Heimnetz."""
                tok = getattr(srv.api, "bridge_token", lambda: "")()
                got = self.headers.get("X-Hauskern-Token", "")
                if not tok or not got or not is_local(self._client()) or not hmac.compare_digest(tok, got):
                    time.sleep(0.5)
                    self._send(403, {"ok": False, "msg": "Bridge-Schlüssel falsch oder Sprachassistenten aus"})
                    return False
                return True

            def _need(self, area=None, lvl=1):
                """Angemeldet und berechtigt? Sonst Antwort senden und None zurückgeben."""
                u = self._user()
                if not u:
                    self._send(401, {"error": "login"})
                    return None
                if area and not U.can(u, area, lvl):
                    self._send(403, {"error": "Keine Berechtigung für diesen Bereich"})
                    return None
                return u

            def _body(self):
                n = min(int(self.headers.get("Content-Length") or 0), 10000)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            def _body_large(self):
                n = min(int(self.headers.get("Content-Length") or 0), 5_000_000)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            def _guard(self):
                if not is_local(self._client()):
                    self._send(403, {"error": "Nur im Heimnetz oder per VPN erreichbar"})
                    return False
                return True

            # ---------- GET ----------
            def do_GET(self):
                self._safe(self._do_GET)

            def do_POST(self):
                self._safe(self._do_POST)

            def _safe(self, fn):
                """Ein Fehler in einer Anfrage soll eine verständliche Meldung liefern – nicht „Verbindung weg“."""
                self._slide = None  # Verbindungen werden wiederverwendet – pro Anfrage neu entscheiden
                try:
                    fn()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:  # noqa: BLE001
                    log.exception("Handy-Anfrage %s", self.path.split("?")[0])
                    try:
                        self._send(500, {"ok": False, "msg": f"Da ist in Hauskern etwas schiefgegangen: {e}"})
                    except Exception:  # noqa: BLE001 – Antwort war schon halb raus
                        pass

            def _do_GET(self):
                if not self._guard():
                    return
                u = urlparse(self.path)
                if u.path in ("/", "/index.html"):
                    return self._send(200, PAGE.replace("{{APP}}", srv.api.app_name), "text/html; charset=utf-8")
                if u.path in ("/wand", "/wand/"):
                    import wall
                    return self._send(200, wall.PAGE.replace("{{APP}}", srv.api.app_name), "text/html; charset=utf-8")
                if u.path == "/wand.webmanifest":
                    import wall
                    return self._send(200, dict(wall.MANIFEST, name=srv.api.app_name + " Wand"), "application/manifest+json")
                if u.path == "/manifest.webmanifest":
                    return self._send(200, {
                        "name": srv.api.app_name, "short_name": srv.api.app_name, "start_url": "/",
                        "display": "standalone", "background_color": "#111318", "theme_color": "#111318",
                        "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
                        "application/manifest+json")
                if u.path in ("/icon-512.png", "/apple-touch-icon.png", "/favicon.png"):
                    p = os.path.join(srv.api.assets_dir, "icon-512.png")
                    if os.path.exists(p):
                        with open(p, "rb") as f:
                            return self._send(200, f.read(), "image/png")
                    return self._send(404, {})
                if u.path == "/api/bridge":
                    if not self._bridge_ok():
                        return
                    return self._send(200, srv.api.bridge_devices())
                if u.path == "/api/siri":
                    q = parse_qs(u.query)
                    return self._siri(q.get("k", [""])[0], q.get("t", [""])[0])
                if u.path == "/api/lists":
                    if not self._need("lists"):
                        return
                    return self._send(200, srv.api.web_lists())
                if u.path == "/api/whatsnew":
                    if not self._need():
                        return
                    return self._send(200, srv.api.web_whatsnew(parse_qs(u.query).get("since", [""])[0][:12]))
                if u.path == "/api/calls":
                    if not self._need("calls"):
                        return
                    return self._send(200, srv.api.web_calls())
                if u.path == "/api/heating":
                    if not self._need("smart"):
                        return
                    return self._send(200, srv.api.main_call(srv.api.web_heating))
                if u.path == "/api/battery":
                    if not self._need("smart"):
                        return
                    return self._send(200, {"ok": True, "items": _call(srv.api.web_battery)})
                if u.path == "/api/timelapse":
                    if not self._need("printers"):
                        return
                    return self._send(200, srv.api.web_timelapse())
                if u.path == "/api/timelapse/file":
                    if not self._need("printers"):
                        return
                    q = parse_qs(u.query)
                    path = srv.api.timelapse_path(q.get("name", [""])[0])
                    if not path:
                        return self._send(404, {"error": "Video nicht gefunden"})
                    return self._send_file(path, "video/mp4")
                if u.path == "/api/clips":
                    if not self._need("cams"):
                        return
                    return self._send(200, _call(srv.api.web_clips, parse_qs(u.query).get("cam", [""])[0]))
                if u.path == "/api/clips/file":
                    if not self._need("cams"):
                        return
                    path = srv.api.clip_path(parse_qs(u.query).get("name", [""])[0])
                    if not path:
                        return self._send(404, {"error": "Video nicht gefunden"})
                    return self._send_file(path, "video/mp4")
                if u.path == "/api/log":
                    usr = self._need()
                    if not usr:
                        return
                    q = parse_qs(u.query)
                    g = lambda k, d="": q.get(k, [d])[0]  # noqa: E731
                    return self._send(200, srv.api.web_log(usr, g("since", "0"), g("kind"), g("q"), g("limit", "200")))
                if u.path == "/api/backup/latest":
                    # PC holt die neueste Sicherung vom Heimserver – gemeinsamer Schlüssel, nur im Heimnetz
                    tok = getattr(srv.api, "pull_token", lambda: "")()
                    got = self.headers.get("X-Hauskern-Token", "")
                    if not tok or not got or not is_local(self._client()) or not hmac.compare_digest(tok, got):
                        time.sleep(0.5)
                        return self._send(403, {"error": "Schlüssel falsch"})
                    name, data = srv.api.latest_backup_bytes()
                    if not data:
                        return self._send(404, {"error": "noch keine Sicherung"})
                    return self._send(200, data, "application/zip", extra={"X-Hauskern-Name": name})
                if u.path == "/api/backup/download":
                    usr = self._need()
                    if not usr:
                        return
                    if not usr.get("admin"):
                        return self._send(403, {"error": "Nur für Admins"})
                    want = parse_qs(u.query).get("name", [""])[0]
                    b = next((x for x in srv.api.list_backups() if x["name"] == want), None)
                    if not b:
                        return self._send(404, {"error": "Sicherung nicht gefunden"})
                    with open(b["path"], "rb") as f:
                        data = f.read()
                    return self._send(200, data, "application/zip",
                                      extra={"Content-Disposition": f'attachment; filename="{b["name"]}"'})
                if u.path == "/api/setup":
                    need = bool(getattr(srv.api, "setup_needed", lambda: False)())
                    return self._send(200, {"needed": need, "local": is_local(self._client())})
                if u.path == "/api/auto":
                    usr = self._need("automations")
                    if not usr:
                        return
                    return self._send(200, srv.api.web_admin("GET", "auto", "", None, {}, usr))
                if u.path == "/api/admin":
                    usr = self._need()
                    if not usr:
                        return
                    q = parse_qs(u.query, keep_blank_values=True)  # „id=“ (leer) heißt: neu anlegen
                    if not self._admin_ok(usr, q.get("s", ["menu"])[0], False):
                        return self._send(403, {"ok": False, "msg": "Dafür hast du keine Berechtigung 🔒"})
                    return self._send(200, srv.api.web_admin("GET", q.get("s", ["menu"])[0], "",
                                                             q.get("id", [None])[0], {}, usr))
                if u.path == "/api/state":
                    if getattr(srv.api, "setup_needed", lambda: False)():
                        return self._send(503, {"error": "Noch nicht eingerichtet", "setup": True})
                    if not srv.api.pin_hash():
                        return self._send(503, {"error": "Kein PIN gesetzt – bitte in Hauskern unter Einstellungen festlegen"})
                    usr = self._need()
                    if not usr:
                        return
                    return self._send(200, U.filter_state(_call(srv.api.get_state, usr), usr))
                if u.path == "/api/power":
                    if not self._need("power"):
                        return
                    return self._send(200, srv.api.web_power())
                if u.path == "/api/history":
                    if not self._need("history"):
                        return
                    q = parse_qs(u.query)
                    try:
                        data = srv.api.web_history(q.get("id", [""])[0], int(q.get("range", ["0"])[0] or 0))
                    except Exception as e:  # noqa: BLE001
                        return self._send(500, {"error": str(e)})
                    return self._send(200, data)
                if u.path == "/api/calc":
                    if not self._need("calc"):
                        return
                    return self._send(200, srv.api.web_calc_info())
                if u.path == "/api/diag":
                    if not self._need("system"):
                        return
                    return self._send(200, srv.api.diag_state())
                if u.path == "/api/cam":
                    if not self._need("cams"):
                        return
                    key = parse_qs(u.query).get("key", [""])[0]
                    png = srv.api.cam_png(key)
                    return self._send(200, png, "image/png") if png else self._send(204, b"", "image/png")
                self._send(404, {"error": "nicht gefunden"})

            do_HEAD = do_GET

            # ---------- POST ----------
            def _do_POST(self):
                if not self._guard():
                    return
                u = urlparse(self.path)
                if self.headers.get("Origin") and urlparse(self.headers["Origin"]).netloc != self.headers.get("Host"):
                    return self._send(403, {"error": "Herkunft abgelehnt"})
                if u.path == "/api/login":
                    ip = self._client()
                    f = srv._fails.get(ip, [0, 0.0])
                    if f[0] >= 5 and time.time() - f[1] < 300:
                        return self._send(429, {"error": "Zu viele Versuche – bitte 5 Minuten warten"})
                    body = self._body()
                    pin, code = str(body.get("pin", "")), str(body.get("code", "")).strip()
                    if hasattr(srv.api, "login_pin"):
                        usr = srv.api.login_pin(pin) if pin else None
                    else:
                        ph = srv.api.pin_hash()
                        usr = {"id": "u0", "admin": True} if ph and pin and check_pin(pin, ph) else None
                    if usr and hasattr(srv.api, "twofa_login") and not srv.api.twofa_login(usr, code):
                        # Zwei-Faktor: PIN stimmt, jetzt fehlt noch der Code aus der Authenticator-App
                        if not code:
                            return self._send(401, {"need_code": True,
                                                    "error": "Bitte den Code aus deiner Authenticator-App eingeben"})
                        srv._fails[ip] = [f[0] + 1 if time.time() - f[1] < 300 else 1, time.time()]
                        time.sleep(0.8)
                        return self._send(403, {"need_code": True, "error": "Code falsch – oder schon benutzt, dann kurz auf den nächsten warten"})
                    if usr:
                        srv._fails.pop(ip, None)
                        tok = secrets.token_urlsafe(32)
                        _call(srv.api.add_session, _tok_hash(tok), usr)
                        return self._send(200, {"ok": True}, extra={
                            "Set-Cookie": f"{COOKIE}={tok}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Strict"})
                    srv._fails[ip] = [f[0] + 1 if time.time() - f[1] < 300 else 1, time.time()]
                    time.sleep(0.8)
                    return self._send(403, {"error": "PIN falsch"})
                if u.path == "/api/setup":
                    # Ersteinrichtung (Heimserver ohne Bildschirm): nur solange niemand eingerichtet ist, nur im Heimnetz
                    ip = self._client()
                    if not getattr(srv.api, "setup_needed", lambda: False)():
                        return self._send(409, {"ok": False, "msg": "Hauskern ist schon eingerichtet – bitte anmelden"})
                    if not is_local(ip):
                        return self._send(403, {"ok": False, "msg": "Die Ersteinrichtung geht nur im Heimnetz"})
                    b = self._body()
                    name, pin = str(b.get("name", "")).strip()[:40], str(b.get("pin", "")).strip()
                    if not name or not (pin.isdigit() and 4 <= len(pin) <= 12):
                        return self._send(400, {"ok": False, "msg": "Bitte Name und eine PIN aus 4–12 Ziffern"})
                    usr = srv.api.setup_create(name, pin)
                    if not usr:
                        return self._send(409, {"ok": False, "msg": "Hauskern ist schon eingerichtet"})
                    tok = secrets.token_urlsafe(32)
                    _call(srv.api.add_session, _tok_hash(tok), usr)
                    return self._send(200, {"ok": True}, extra={
                        "Set-Cookie": f"{COOKIE}={tok}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Strict"})
                if u.path == "/api/bridge":
                    if not self._bridge_ok():
                        return
                    b = self._body()
                    ok, msg = srv.api.bridge_command(str(b.get("id", "")), str(b.get("cmd", "")), b.get("value"))
                    return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
                if u.path == "/api/siri":
                    q = parse_qs(u.query)
                    raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4000)).decode("utf-8", "replace")
                    text = ""
                    try:
                        j = json.loads(raw) if raw.strip().startswith("{") else {}
                        text = str(j.get("t") or j.get("text") or "")
                    except ValueError:
                        pass
                    if not text:
                        text = (parse_qs(raw).get("t") or [raw if "=" not in raw else ""])[0]
                    return self._siri(q.get("k", [""])[0] or self.headers.get("X-Hauskern-Key", ""),
                                      text or q.get("t", [""])[0])
                if u.path == "/api/lists":
                    usr = self._need("lists", 2)
                    if not usr:
                        return
                    ok, msg = srv.api.lists_op(self._body(), usr)
                    return self._send(200 if ok else 400, {**srv.api.web_lists(), "ok": ok, "msg": msg})
                if u.path == "/api/clips/delete":
                    usr = self._need("cams", 2)
                    if not usr:
                        return
                    ok = srv.api.clip_delete(str(self._body().get("name", "")), usr.get("name", "") if isinstance(usr, dict) else "")
                    return self._send(200 if ok else 404, {"ok": ok, "msg": "Gelöscht" if ok else "nicht gefunden"})
                if u.path == "/api/timelapse/delete":
                    if not self._need("printers", 2):
                        return
                    ok = srv.api.timelapse_delete(str(self._body().get("name", "")))
                    return self._send(200 if ok else 404, {"ok": ok, "msg": "Gelöscht" if ok else "nicht gefunden"})
                if u.path in ("/api/auto", "/api/admin"):
                    usr = self._need()
                    if not usr:
                        return
                    b = self._body_large()
                    op = str(b.get("op", ""))
                    if u.path == "/api/auto":
                        if not U.can(usr, "automations", 1 if op == "run" else 2):
                            return self._send(403, {"ok": False, "msg": "Dafür hast du keine Berechtigung 🔒"})
                        res = srv.api.web_admin("POST", "auto", op, b.get("id"), b, usr)
                    else:
                        if not self._admin_ok(usr, str(b.get("s", "")), True):
                            return self._send(403, {"ok": False, "msg": "Dafür hast du keine Berechtigung 🔒"})
                        res = srv.api.web_admin("POST", str(b.get("s", "")), op, b.get("id"), b.get("values") or {}, usr)
                    return self._send(200, res)
                if u.path == "/api/logout":
                    return self._send(200, {"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
                if u.path == "/api/config":
                    # Einstellungen vom PC übernehmen – verlangt immer die PIN (nicht nur ein Cookie)
                    ip = self._client()
                    f = srv._fails.get(ip, [0, 0.0])
                    if f[0] >= 5 and time.time() - f[1] < 300:
                        return self._send(429, {"error": "Zu viele Versuche – bitte 5 Minuten warten"})
                    body = self._body_large()
                    pin = str(body.get("pin", ""))
                    if hasattr(srv.api, "check_admin_pin"):
                        pin_ok = srv.api.check_admin_pin(pin)
                    else:
                        ph = srv.api.pin_hash()
                        pin_ok = bool(ph and check_pin(pin, ph))
                    if not pin_ok:
                        srv._fails[ip] = [f[0] + 1, time.time()]
                        time.sleep(0.8)
                        return self._send(403, {"error": "PIN falsch"})
                    if hasattr(srv.api, "login_pin") and hasattr(srv.api, "twofa_login"):
                        adm = srv.api.login_pin(pin)
                        code = str(body.get("code", "")).strip()
                        if adm and not srv.api.twofa_login(adm, code):
                            if code:
                                srv._fails[ip] = [f[0] + 1, time.time()]
                                time.sleep(0.8)
                            return self._send(403, {"need_code": True, "error": "Code falsch oder abgelaufen" if code else
                                                    "Zwei-Faktor ist an – bitte den Code aus der Authenticator-App mitschicken"})
                    cfg = body.get("config")
                    if not isinstance(cfg, dict) or "host" not in cfg:
                        return self._send(400, {"error": "Keine gültigen Einstellungen"})
                    ok, msg = srv.api.import_config(cfg)
                    return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
                if u.path == "/api/info":
                    return self._send(200, {"app": srv.api.app_name, "version": getattr(srv.api, "version", "?"),
                                            "headless": getattr(srv.api, "headless", False)})
                if u.path == "/api/update":
                    n = int(self.headers.get("Content-Length") or 0)
                    if not n or n > 60_000_000:
                        return self._send(413, {"error": "Paket fehlt oder zu groß"})
                    data = self.rfile.read(n)
                    ok, msg = srv.api.receive_update(data, self.headers.get("X-Hauskern-Signature", ""))
                    if not ok:
                        time.sleep(0.8)
                    return self._send(200 if ok else 403, {"ok": ok, "msg": msg, "error": "" if ok else msg})
                if u.path.startswith("/api/calc"):
                    if not self._need("calc", 2 if u.path in ("/api/calc/save", "/api/calc/delete") else 1):
                        return
                    try:
                        if u.path == "/api/calc/3mf":
                            n = int(self.headers.get("Content-Length") or 0)
                            if n > 40_000_000:
                                return self._send(413, {"error": "Datei zu groß"})
                            data = self.rfile.read(n)
                            name = parse_qs(u.query).get("name", [""])[0]
                            return self._send(200, srv.api.web_calc_3mf(data, name))
                        body = self._body_large()
                        if u.path == "/api/calc":
                            return self._send(200, srv.api.web_calc(body.get("job") or {}))
                        if u.path == "/api/calc/save":
                            srv.api.web_calc_save(body.get("job") or {})
                            return self._send(200, {"ok": True})
                        if u.path == "/api/calc/delete":
                            srv.api.web_calc_delete(body.get("id"))
                            return self._send(200, {"ok": True})
                    except Exception as e:  # noqa: BLE001
                        return self._send(400, {"error": str(e)})
                    return self._send(404, {"error": "nicht gefunden"})
                if u.path == "/api/action":
                    usr = self._need()
                    if not usr:
                        return
                    body = self._body()
                    key = str(body.get("key", ""))
                    if not U.can(usr, U.action_area(key), U.action_need(key, body.get("action"))):
                        return self._send(403, {"ok": False, "msg": "Dafür hast du keine Berechtigung 🔒"})
                    ok, msg = _call(srv.api.do_action, body, usr)
                    return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
                self._send(404, {"error": "nicht gefunden"})

        return H


# --------------------------------------------------------------------------- #
# Oberfläche (eine Seite, läuft als „App“ vom iPhone-Homebildschirm)
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="{{APP}}">
<meta name="theme-color" content="#111318">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/favicon.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>{{APP}}</title>
<style>
:root{--bg:#111318;--card:#1b1e25;--card2:#272b34;--on:#1b2c47;--fg:#eceef2;--mut:#8e96a3;--acc:#4f8ff7;--ok:#3ecf8e;--bad:#f2626b;--warn:#e0a33a;--amb:#f6c15a;--heat:#ff8a5c;--line:rgba(255,255,255,.06);--ease:cubic-bezier(.2,.8,.2,1)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(17,19,24,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
 padding:calc(env(safe-area-inset-top) + 10px) 16px 10px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #1b1e25}
header img{width:32px;height:32px;border-radius:8px}
header h1{font-size:19px;margin:0;font-weight:700}header h1 span{font-weight:400}
#st{margin-left:auto;font-size:12px;color:var(--mut);display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;white-space:nowrap}#st span:last-child{overflow:hidden;text-overflow:ellipsis}header>.b{flex-shrink:0}@media(max-width:430px){#bu .un{display:none}#stt{display:none}}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
main{padding:6px 12px calc(env(safe-area-inset-bottom) + 24px)}
h2{font-size:14px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:18px 4px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.t{background:var(--card);border-radius:16px;padding:12px 12px 11px;position:relative;min-height:96px;border:1px solid var(--card2);transition:background .2s}
.t.on{background:var(--on);border-color:#2f4f80}.t.wide{grid-column:1/-1}.t.off{opacity:.55}
.n{font-weight:600;font-size:15px;line-height:1.2;padding-right:52px}.s{color:var(--mut);font-size:12.5px;margin-top:3px}
.big{font-size:22px;font-weight:650;margin-top:8px}
.sw{position:absolute;right:12px;top:12px;width:46px;height:28px;border-radius:14px;background:#3b404b;border:0;padding:0}
.sw:after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:left .18s}
.sw.on{background:var(--acc)}.sw.on:after{left:21px}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
button.b,a.b{background:var(--card2);color:var(--fg);border:0;border-radius:11px;padding:9px 14px;font:600 14px inherit;font-family:inherit}
button.b.acc{background:var(--acc)}button.b.red{background:#5a2629}button.b:active{opacity:.7}
.bar{height:10px;border-radius:5px;background:var(--card2);overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;background:var(--ok);border-radius:5px}
.temps{display:flex;gap:14px;margin-top:10px;flex-wrap:wrap}.temps div{font-size:12px;color:var(--mut)}.temps b{display:block;color:var(--fg);font-size:15px}
.ams{display:flex;gap:6px;margin-top:10px}.ams i{width:18px;height:18px;border-radius:50%;border:1px solid #444}.ams i.a{outline:2px solid #fff}
.cam{padding:0;overflow:hidden}.cam img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#0d0e11}
.cam .cap{padding:8px 12px;display:flex;justify-content:space-between;font-size:13px}
.ag{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--card2);font-size:14px}.ag:last-child{border:0}
.ag .c{width:4px;border-radius:2px}.ag .tm{color:var(--mut);width:74px;flex:none}
.day{color:var(--mut);font-size:12px;font-weight:600;margin-top:8px}
#login{max-width:340px;margin:12vh auto;text-align:center;padding:0 20px}#login img{width:84px;border-radius:20px}
#login input{width:100%;font-size:24px;letter-spacing:.4em;text-align:center;padding:14px;border-radius:14px;border:1px solid var(--card2);background:var(--card);color:var(--fg);margin:18px 0 10px}
#login .err{color:var(--bad);min-height:20px;font-size:14px}
#ov{position:fixed;inset:0;background:#000;z-index:20;display:none;flex-direction:column}
#ov img{width:100%;flex:1;object-fit:contain;min-height:0}#ov .ctl{padding:12px 12px calc(env(safe-area-inset-bottom) + 12px);background:#111}
.pad{display:grid;grid-template-columns:repeat(3,64px);gap:8px;justify-content:center}.pad button{height:54px;font-size:20px}
.dg{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--card2);font-size:13.5px}.dg:last-child{border:0}
.dg .h{color:var(--mut);font-size:12.5px;margin-top:2px}.dg .h.w{color:var(--amb)}
#hv{position:fixed;inset:0;background:var(--bg);z-index:15;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#hv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card)}
#hv select{width:100%;font:600 16px inherit;font-family:inherit;color:var(--fg);background:var(--card);border:1px solid var(--card2);border-radius:11px;padding:10px 12px;margin-top:8px;-webkit-appearance:none;appearance:none}
.seg{display:flex;background:var(--card);border-radius:11px;padding:3px;margin-top:8px}.seg button{flex:1;border:0;background:none;color:var(--mut);font:600 13px inherit;font-family:inherit;padding:8px 0;border-radius:9px}
.seg button.on{background:var(--card2);color:var(--fg)}
.st{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}.st div{background:var(--card);border-radius:13px;padding:10px 12px}.st b{display:block;font-size:17px;margin-top:2px}
.ch{background:var(--card);border-radius:14px;padding:10px 6px 4px;margin-bottom:12px;position:relative}.ch .ti{font-size:12.5px;color:var(--mut);padding:0 8px 6px}
.ch svg{display:block;width:100%;touch-action:pan-y}.lg{display:flex;gap:12px;flex-wrap:wrap;padding:0 8px 6px;font-size:12px}.lg i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.tip{position:absolute;top:28px;background:#0d0f13;border:1px solid #343944;border-radius:9px;padding:6px 9px;font-size:12.5px;pointer-events:none;display:none;white-space:nowrap}
.out{font-size:14px}.out div{display:flex;justify-content:space-between;padding:8px 8px;border-bottom:1px solid var(--card2)}.out div:last-child{border:0}
.amsu{margin-top:12px;border-top:1px solid var(--card2);padding-top:10px}.amsu .hd{display:flex;justify-content:space-between;font-size:13px}
.spools{display:flex;gap:10px;margin-top:8px}.sp{flex:1;text-align:center;font-size:12px;min-width:0}
.sp .tube{position:relative;height:64px;border-radius:10px;background:var(--card2);border:1px solid #343944;overflow:hidden;margin:0 auto;max-width:48px}
.sp .tube.a{border:2px solid #fff}.sp .fill{position:absolute;left:3px;right:3px;bottom:3px;border-radius:7px}
.sp .pc{position:absolute;left:0;right:0;bottom:6px;font-weight:700;font-size:12px;text-shadow:0 0 3px #000}
.sp .fill{box-shadow:inset 0 0 0 1px rgba(255,255,255,.22)}
label.b{background:var(--card2);color:var(--fg);border-radius:11px;padding:9px 14px;font:600 14px inherit;font-family:inherit}
.sp b{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#cv{position:fixed;inset:0;background:var(--bg);z-index:16;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
.ro .b:not(.view),.ro .sw,.ro button.pm{opacity:.35;pointer-events:none}#lsv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#lsin{flex:1;min-width:0;background:var(--card);color:var(--fg);border:1px solid var(--card2,#2a2d35);border-radius:12px;padding:12px 14px;font-size:16px;font-family:inherit;outline:none}#lsin:focus{border-color:var(--acc,#4a8df8)}#lsadd .b{font-size:20px;padding:10px 18px}
#lsv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.fm textarea{min-height:130px;resize:vertical;line-height:1.4}.pv{white-space:pre-wrap;font-size:12px;background:var(--card);border-radius:10px;padding:10px;margin-top:12px;color:var(--mut);overflow-wrap:anywhere}
.li2{display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:13px 14px;margin:6px 0;font-size:16px;cursor:pointer;-webkit-user-select:none;user-select:none}.li2 .ck{width:24px;height:24px;border-radius:50%;border:2px solid var(--mut,#8a8f98);flex:none;display:flex;align-items:center;justify-content:center;font-size:14px}.li2.d{opacity:.45}.li2.d .ck{background:var(--ok);border-color:var(--ok);color:#fff}.li2.d .tx2{text-decoration:line-through}.li2 .tx2{flex:1}.li2 .by{font-size:11px;color:var(--mut,#8a8f98)}.li2 .x{padding:2px 8px;font-size:18px;color:var(--mut,#8a8f98)}
#clv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#clv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.call{display:flex;align-items:center;gap:12px;background:var(--card);border-radius:14px;padding:11px 14px;margin:6px 0}.call .w{flex:1;min-width:0}.call .w b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.call.m b{color:var(--bad)}.call a{font-size:20px;text-decoration:none;padding:6px 8px;border-radius:10px;background:var(--card2,#2a2d35)}
#hcv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#hcv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.hf{display:flex;gap:8px;margin-top:8px;line-height:1.4}.hf i{font-style:normal;flex:none}
#bv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#bv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.bat{background:var(--card);border-radius:14px;padding:12px 14px;margin:8px 0}.bar{height:8px;border-radius:4px;background:var(--card2,#2a2d35);margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;border-radius:4px}
.rm{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:6px 0 14px}.rm .b{padding:14px 4px;font-size:15px;margin:0}
.rm .b.acc{font-weight:700}.rmn{font-size:20px;font-weight:700;margin:2px 0 4px}.chi{display:flex;gap:8px}.chi input{flex:1;font-size:18px}
#wxv,#rmv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#rmv .top,#wxv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
.wxab{display:flex;gap:12px;align-items:center;margin:12px 0 0;padding:11px 14px;border-radius:14px;cursor:pointer;
 background:linear-gradient(135deg,color-mix(in srgb,var(--wc) 22%,#151820),#151820);border:1px solid color-mix(in srgb,var(--wc) 45%,transparent);animation:hkFade .4s var(--ease)}
.wxab>span{font-size:26px}.wxab b{color:var(--wc)}
.n{hyphens:auto;-webkit-hyphens:auto;overflow-wrap:break-word}.sw~.s{padding-right:54px}
.t.alarm{border-color:rgba(242,98,107,.6);box-shadow:0 0 0 2px rgba(242,98,107,.25),0 8px 24px rgba(0,0,0,.22);animation:hkPulse 1.6s ease-in-out infinite}
@keyframes hkPulse{50%{box-shadow:0 0 0 6px rgba(242,98,107,.12),0 8px 24px rgba(0,0,0,.22)}}
.vacb{margin:12px 0 0;padding:10px 14px;border-radius:12px;background:#3a2d12;color:var(--amb);font-weight:600;cursor:pointer}
#rcv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#rcv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
#tv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#tv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.vid{background:var(--card);border-radius:14px;padding:12px 14px;margin:8px 0}.vid a.b{background:var(--card2);color:var(--fg);border-radius:11px;padding:9px 14px;font:600 14px inherit;font-family:inherit;display:inline-flex;align-items:center}.vid video{width:100%;border-radius:10px;margin-top:10px;background:#000}
#lv{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#lv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card)}
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;-webkit-overflow-scrolling:touch}.chips span{flex:none;padding:7px 11px;border-radius:16px;background:var(--card);font-size:13px;cursor:pointer;white-space:nowrap}.chips span.on{background:var(--acc);color:#fff}
.le{display:flex;gap:10px;padding:10px 4px;border-bottom:1px solid var(--card)}.le .ic{font-size:18px;width:24px;text-align:center}.le .tx{flex:1;min-width:0}.le .tm{font-size:12px;color:var(--mut);white-space:nowrap}.le.new{border-left:3px solid var(--acc);padding-left:8px;background:rgba(61,139,253,.06)}
#wn{display:none;background:linear-gradient(135deg,#2f5fae,#6b4bd8);color:#fff;border-radius:14px;padding:11px 14px;margin:8px 12px 0;cursor:pointer;font-size:14px}
#wv{position:fixed;inset:0;background:var(--bg);z-index:19;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#wv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}
.wns{background:var(--card);border-radius:14px;padding:4px 16px 10px;margin:10px 0;line-height:1.5;font-size:15px}.wns h3{font-size:16px;margin:12px 0 4px;color:var(--acc,#4a8df8)}.wns ul{padding-left:20px;margin:4px 0}.wns li{margin:4px 0}.wns p{margin:8px 0}.wns code{background:var(--bg);padding:1px 5px;border-radius:5px;font-size:13px}
#news{display:none;background:var(--card);border-radius:14px;padding:11px 14px;margin:8px 12px 0;cursor:pointer;font-size:14px}
#sv,#vh{position:fixed;inset:0;background:var(--bg);z-index:18;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}#sv .top,#vh .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;gap:10px}#sv .top b,#vh .top b{font-size:18px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sb{padding:4px 14px calc(env(safe-area-inset-bottom) + 30px)}.li{background:var(--card);border-radius:14px;padding:13px 14px;margin:8px 0;display:flex;align-items:center;gap:10px;cursor:pointer}.li .tx{flex:1;min-width:0}.li .tt{font-weight:600}.li .su{font-size:13px;color:var(--mut);margin-top:2px}.li .ch{color:var(--mut);font-size:20px}.hint{font-size:13px;color:var(--mut);margin:10px 2px;white-space:pre-line}.fm .fh{font-size:12px;color:var(--mut);margin:3px 2px 0}.fm .ck{display:flex;align-items:center;gap:10px;padding:9px 10px;background:var(--card);border-radius:10px;margin:4px 0;font-size:14px}.fm .ck input{width:20px;height:20px;flex:none;-webkit-appearance:auto;appearance:auto}.fm .ml{max-height:260px;overflow-y:auto;border-radius:10px}.dy{display:flex;gap:5px;flex-wrap:wrap}.dy span{padding:8px 10px;border-radius:10px;background:var(--card);font-size:13px;cursor:pointer;user-select:none}.dy span.on{background:var(--acc);color:#fff}.ac{background:var(--card2);border-radius:14px;padding:10px 12px;margin:8px 0}.ac .hd{display:flex;align-items:center;gap:6px;font-weight:600}.ac .hd span{flex:1}.ac .hd button{padding:4px 9px}.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.btns .b{flex:1;padding:12px;min-width:120px}.danger{color:var(--bad)!important}#off{display:none;background:#3a2a10;color:#f6d38a;font-size:13px;padding:9px 14px;border-radius:12px;margin:8px 12px 0;cursor:pointer}body.offline main .b,body.offline main .sw,body.offline main .t[onclick]{pointer-events:none;opacity:.45}#setup{display:none;max-width:420px;margin:0 auto;padding:calc(env(safe-area-inset-top) + 40px) 22px 30px;text-align:center}#setup img{width:76px;height:76px;border-radius:18px}#setup input{width:100%;font-size:18px;padding:13px;border-radius:13px;border:1px solid var(--card2);background:var(--card);color:var(--fg);margin:7px 0}.flow{display:flex;gap:4px;margin:10px 0}.flow i{flex:1;height:4px;border-radius:2px;background:var(--card2)}.flow i.on{background:var(--acc)}
#pv,#gv{position:fixed;inset:0;background:var(--bg);z-index:17;display:none;overflow-y:auto;-webkit-overflow-scrolling:touch}
#pv .top,#gv .top{position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card);display:flex;align-items:center;justify-content:space-between}
.pc{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}.pc .t{min-height:0}
.mb{display:flex;align-items:flex-end;gap:4px;height:150px;margin:8px 0 4px}.mb div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;font-size:9px;color:var(--mut)}.mb i{display:block;width:100%;background:var(--acc);border-radius:4px 4px 0 0;min-height:1px}.mb .cur i{background:var(--amb)}
.dv{background:var(--card);border-radius:14px;padding:12px 14px;margin:8px 0}.dv .r{display:flex;justify-content:space-between;gap:8px;font-size:13px;color:var(--mut);margin-top:3px}
.qrb{background:#fff;border-radius:22px;padding:14px;width:min(78vw,320px);margin:16px auto}.qrb svg{width:100%;height:auto;display:block}
.gk{text-align:center;margin:6px 0}.gk .l{font-size:13px;color:var(--mut)}.gk .v{font-size:20px;font-weight:600;word-break:break-all}
@media print{body>*:not(#gv){display:none!important}#gv{position:static;display:block!important;background:#fff;color:#000}#gv .top,#gv .np{display:none!important}#gv .gk .l{color:#444}.qrb{border:2px solid #000}}
.fm label{display:block;font-size:12px;color:var(--mut);margin:10px 2px 4px}.fm input,.fm select,.fm textarea{width:100%;font:16px inherit;font-family:inherit;color:var(--fg);background:var(--card);border:1px solid var(--card2);border-radius:10px;padding:9px 10px;-webkit-appearance:none;appearance:none}
.fm .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.fm .fr{display:grid;grid-template-columns:1fr 90px;gap:8px;margin-top:6px}
.res{background:var(--card);border-radius:14px;padding:14px;margin-top:14px}.res .pr{font-size:30px;font-weight:750}
.res .ln{display:flex;justify-content:space-between;font-size:14px;padding:3px 0}.res .ln.b{font-weight:700}.res .ln.g{color:var(--ok);font-weight:700}.res .ln span:first-child{color:var(--mut)}.res .ln.b span:first-child{color:var(--fg)}
#toast{position:fixed;left:50%;bottom:calc(env(safe-area-inset-bottom) + 18px);transform:translateX(-50%);background:#272b34;padding:10px 16px;border-radius:12px;font-size:14px;display:none;z-index:30}

/* ===== Feinschliff 8.6: weicher, ruhiger, mit sanften Bewegungen ===== */
html{scroll-behavior:smooth}
body{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
body:before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
 background:radial-gradient(900px 520px at 85% -120px,rgba(79,143,247,.13),transparent 70%),
            radial-gradient(700px 480px at -10% 8%,rgba(124,92,255,.08),transparent 70%)}
header{background:rgba(17,19,24,.72);backdrop-filter:blur(18px) saturate(160%);-webkit-backdrop-filter:blur(18px) saturate(160%);border-bottom:1px solid var(--line)}
header img{border-radius:10px;box-shadow:0 4px 14px rgba(79,143,247,.25)}
h2{font-size:12.5px;letter-spacing:.09em;color:#7f8794;margin:24px 6px 10px;font-weight:650}
.grid{gap:12px}
.t{border-radius:20px;border:1px solid var(--line);
 background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,0) 60%),var(--card);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 8px 24px rgba(0,0,0,.22);
 transition:transform .2s var(--ease),background .35s var(--ease),box-shadow .35s var(--ease),border-color .35s}
.t:active{transform:scale(.97)}
.t.on{background:linear-gradient(160deg,#243f69 0%,#1a2b47 70%);border-color:rgba(79,143,247,.32);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 10px 28px rgba(40,100,220,.20)}
.n{letter-spacing:.005em}
.big{letter-spacing:-.01em}
.sw{background:#3b404b;transition:background .28s var(--ease);box-shadow:inset 0 1px 2px rgba(0,0,0,.35)}
.sw:after{transition:left .28s var(--ease);box-shadow:0 2px 6px rgba(0,0,0,.35)}
.sw.on{background:linear-gradient(135deg,#5b9bff,#3d7ef0)}
button.b{border-radius:12px;transition:transform .15s var(--ease),background .25s,opacity .2s;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
button.b:active{transform:scale(.95);opacity:.85}
button.b.acc{background:linear-gradient(135deg,#5b9bff,#3d7ef0);box-shadow:0 6px 16px rgba(61,126,240,.28)}
.bar{background:#262a33}.bar i{transition:width .8s var(--ease)}
.cam{border-radius:20px}.cam img{transition:opacity .4s}
.st div,.ch,.vid{border-radius:16px;border:1px solid var(--line)}
.seg{border-radius:12px}.seg button{transition:background .25s var(--ease),color .25s}
#login img{box-shadow:0 12px 34px rgba(79,143,247,.30)}
#login input{transition:border-color .2s,box-shadow .2s}
#login input:focus,#sv input:focus,#sv select:focus,#sv textarea:focus,#lsv input:focus{outline:none;border-color:var(--acc)!important;box-shadow:0 0 0 3px rgba(79,143,247,.22)}
#sv input,#sv select,#sv textarea{border-radius:12px;transition:border-color .2s,box-shadow .2s}
#toast{border-radius:14px;background:rgba(39,43,52,.92);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
 box-shadow:0 10px 30px rgba(0,0,0,.35);border:1px solid var(--line);animation:hkToast .3s var(--ease)}
.vacb{border:1px solid rgba(246,193,90,.18);background:linear-gradient(135deg,#3b2e14,#2c2412);animation:hkFade .4s var(--ease)}
#wn{border-radius:16px;box-shadow:0 10px 28px rgba(80,80,220,.25)}
/* Fenster gleiten weich herein */
#sv,#vh,#hv,#lsv,#clv,#hcv,#bv,#rcv,#tv,#lv,#wv,#cv,#gv,#pv,#wxv,#rmv{animation:hkUp .32s var(--ease)}
#ov{animation:hkFade .25s ease}
#sv .top,#vh .top,#hv .top,#lsv .top,#clv .top,#hcv .top,#bv .top,#rcv .top,#tv .top,#lv .top,#wv .top,#gv .top,#pv .top{border-bottom:1px solid var(--line)!important}
@keyframes hkUp{from{opacity:0;transform:translateY(28px)}to{opacity:1;transform:none}}
@keyframes hkFade{from{opacity:0}to{opacity:1}}
@keyframes hkToast{from{opacity:0;transform:translate(-50%,10px)}to{opacity:1;transform:translate(-50%,0)}}
@media (hover:hover){.t:hover{border-color:rgba(255,255,255,.11)}button.b:hover{filter:brightness(1.12)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style></head><body>
<div id="login" style="display:none">
  <img src="/icon-512.png" alt=""><h2 style="text-transform:none;color:var(--fg);font-size:22px;letter-spacing:0">{{APP}}</h2>
  <div class="s" id="lhint">PIN eingeben (festgelegt in {{APP}} → Einstellungen)</div>
  <input id="pin" type="password" inputmode="numeric" autocomplete="current-password" maxlength="12">
  <input id="tfa" style="display:none" inputmode="numeric" autocomplete="one-time-code" maxlength="12" placeholder="123456">
  <button class="b acc" style="width:100%;padding:13px" onclick="login()">Anmelden</button>
  <div class="s" id="tfab" style="display:none;margin-top:12px"><a href="#" onclick="tfaBackup();return false" style="color:var(--mut)">Notfallcode eingeben</a> · <a href="#" onclick="tfaReset();return false" style="color:var(--mut)">‹ andere PIN</a></div>
  <div class="err" id="lerr"></div>
</div>
<div id="setup"><img src="/icon-512.png" alt=""><h2 style="text-transform:none;color:var(--fg);font-size:23px;letter-spacing:0;margin:14px 0 4px">Willkommen bei Hauskern 👋</h2>
 <div class="s" style="margin-bottom:14px">Hauskern läuft – leg jetzt deinen Zugang an. Du bist Admin und darfst alles einrichten.</div>
 <input id="su_name" placeholder="Dein Name" autocomplete="name"><input id="su_pin" type="password" inputmode="numeric" placeholder="PIN (4–12 Ziffern)" maxlength="12"><input id="su_pin2" type="password" inputmode="numeric" placeholder="PIN wiederholen" maxlength="12">
 <button class="b acc" style="width:100%;padding:14px;margin-top:8px" onclick="doSetup()">Los geht's</button><div class="err" id="su_err"></div>
 <div class="s" style="margin-top:18px">Danach führt dich Hauskern durch Router, Personen, Drucker, Kameras und Push.</div></div>
<div id="sv"><div class="top"><button class="b" id="sv_back" onclick="svBack()">‹</button><b id="sv_t">Einstellungen</b><button class="b" onclick="svClose()">Fertig</button></div><div class="sb" id="sv_b"></div></div>
<div id="wv"><div class="top"><b style="font-size:18px;flex:1">📰 Was ist neu?</b><button class="b acc" onclick="closeWN()">Alles klar 👍</button></div><div class="sb" id="wbody"></div></div>
<div id="lsv"><div class="top"><b style="font-size:18px;flex:1" id="lst">🛒 Einkaufsliste</b><button class="b" onclick="closeOv('lsv')">Fertig</button></div>
<div class="sb"><div class="row" style="gap:6px;margin:10px 0" id="lstabs"></div><div id="lsadd" class="row" style="flex-wrap:nowrap;gap:6px;margin-bottom:8px"><input id="lsin" placeholder="z. B. Milch, Eier, Brot" enterkeyhint="done" autocomplete="off" onkeydown="if(event.key==='Enter'){event.preventDefault();lsAdd()}"><button class="b acc" onclick="lsAdd()">＋</button></div><div id="lsbody"></div></div></div>
<div id="clv"><div class="top"><b style="font-size:18px;flex:1">📞 Anrufe</b><button class="b" onclick="closeOv('clv')">Fertig</button></div><div class="sb" id="clbody"></div></div>
<div id="hcv"><div class="top"><b style="font-size:18px;flex:1">🔥 Heizungs-Check</b><button class="b" onclick="closeOv('hcv')">Fertig</button></div><div class="sb" id="hcbody"></div></div>
<div id="bv"><div class="top"><b style="font-size:18px;flex:1">🔋 Batterien</b><button class="b" onclick="closeOv('bv')">Fertig</button></div><div class="sb" id="bbody"></div></div>
<div id="tv"><div class="top"><b style="font-size:18px;flex:1">🎬 Zeitraffer</b><button class="b" onclick="closeOv('tv')">Fertig</button></div><div class="sb" id="tbody"></div></div>
<div id="lv"><div class="top"><div style="display:flex;align-items:center;gap:10px"><b style="font-size:18px;flex:1">📜 Logbuch</b><button class="b" onclick="closeLog()">Fertig</button></div><input id="lq" placeholder="Suchen …" style="width:100%;margin-top:8px;font-size:16px;padding:9px 11px;border-radius:10px;border:1px solid var(--card2);background:var(--card);color:var(--fg)" oninput="clearTimeout(window._lqt);window._lqt=setTimeout(loadLog,350)"><div class="chips" id="lchips"></div></div><div class="sb" id="lbody"></div></div>
<div id="vh"><div class="top"><b>📴 Unterwegs verbinden</b><button class="b" onclick="closeOv('vh')">Fertig</button></div><div class="sb" id="vh_b"></div></div>
<div id="app" style="display:none">
<header><img src="/icon-512.png" alt=""><h1>Haus<span>kern</span></h1><div id="st"><span class="dot" id="dot"></span><span id="stt">verbinde …</span></div><button class="b" id="bh" style="padding:6px 10px;font-size:16px" onclick="openHist()" aria-label="Verlauf">📈</button><button class="b" id="bc" style="padding:6px 10px;font-size:16px" onclick="openCalc()" aria-label="Druckkosten">🧮</button><button class="b" id="bs" style="padding:6px 10px;font-size:16px;display:none" onclick="openSettings()" aria-label="Einstellungen">⚙</button><button class="b" id="bu" style="padding:6px 10px;font-size:13px;flex-shrink:0;white-space:nowrap" onclick="logout()" aria-label="Abmelden">👤</button></header><div id="off" onclick="vpnHelp()"></div><div id="wn" onclick="openWN()"></div><div id="news" onclick="openLog()"></div>
<main id="main"></main><div id="diag"></div></div>
<div id="rmv"><div class="top"><b style="font-size:18px;flex:1" id="rmt">📺 Fernbedienung</b><button class="b" onclick="RMK=null;closeOv('rmv')">Fertig</button></div><div class="sb" id="rmbody"></div></div>
<div id="wxv"><div class="top"><b style="font-size:18px;flex:1">⛈ Unwetterwarnungen</b><button class="b" onclick="closeOv('wxv')">Fertig</button></div><div class="sb" id="wxbody"></div></div>
<div id="rcv"><div class="top"><b style="font-size:18px;flex:1" id="rctitle">🎥 Aufnahmen</b><button class="b" onclick="closeOv('rcv')">Fertig</button></div><div class="sb" id="rcbody"></div></div>
<div id="ov"><img id="ovimg" alt=""><div class="ctl">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px"><b id="ovname" style="flex:1"></b><button class="b" onclick="openRec(ovKey)">🎥 Aufnahmen</button><button class="b" onclick="closeCam()">Schließen</button></div>
  <div id="ptz"></div></div></div>
<div id="hv"><div class="top"><div style="display:flex;align-items:center;justify-content:space-between"><b style="font-size:19px">📈 Verlauf</b><button class="b" onclick="closeHist()">Fertig</button></div>
<select id="hsrc" onchange="loadHist(this.value,H.range)"></select><div class="seg" id="hrg"></div></div><div style="padding:0 12px calc(env(safe-area-inset-bottom) + 24px)" id="hbody"></div></div>
<div id="pv"><div class="top"><b style="font-size:19px">⚡ Stromkosten</b><button class="b" onclick="closeOv('pv')">Fertig</button></div><div style="padding:0 14px calc(env(safe-area-inset-bottom) + 24px)" id="pbody"></div></div>
<div id="gv"><div class="top"><b style="font-size:19px">📶 Gast-WLAN</b><button class="b" onclick="closeOv('gv')">Fertig</button></div><div style="padding:0 14px calc(env(safe-area-inset-bottom) + 24px)" id="gbody"></div></div>
<div id="cv"><div class="top" style="position:sticky;top:0;background:rgba(17,19,24,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;z-index:2;border-bottom:1px solid var(--card)">
<div style="display:flex;align-items:center;justify-content:space-between"><b style="font-size:19px">🧮 Druckkosten</b><button class="b" onclick="closeCalc()">Fertig</button></div>
<div class="row" style="margin-top:8px"><label class="b" style="display:inline-block">📄 .3mf öffnen<input type="file" accept=".3mf" style="display:none" onchange="up3mf(this)"></label><button class="b" onclick="pickPrint()">Aus Druck</button><button class="b" onclick="newCalc()">Neu</button></div></div>
<div style="padding:0 12px calc(env(safe-area-inset-bottom) + 24px)" id="cbody"></div></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let S=null,camTimer=null,ovKey=null,busy=false;const camSrc={};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=v=>(Math.round(v*10)/10).toString().replace(".",",");
function toast(t){const e=$("#toast");e.textContent=t;e.style.display="block";clearTimeout(e._t);e._t=setTimeout(()=>e.style.display="none",2200)}
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),credentials:"same-origin"}:{credentials:"same-origin"});
 if(r.status==401){showLogin();throw"login"} return r}
let TFA_PIN="";
async function login(){const step2=!!TFA_PIN,pin=step2?TFA_PIN:$("#pin").value,code=step2?$("#tfa").value.trim():"";$("#lerr").textContent="";
 const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(step2?{pin,code}:{pin}),credentials:"same-origin"});
 if(r.ok){tfaReset();$("#login").style.display="none";$("#app").style.display="block";load();return}
 const j=await r.json().catch(()=>({}));
 if(j.need_code){TFA_PIN=pin;$("#pin").style.display="none";$("#tfa").style.display="";$("#tfab").style.display="";
  $("#lhint").textContent="🔐 Code aus deiner Authenticator-App – oder einer deiner Notfallcodes";$("#tfa").value="";setTimeout(()=>$("#tfa").focus(),100);
  if(step2)$("#lerr").textContent=j.error||"Code falsch";return}
 $("#lerr").textContent=j.error||"Fehler";if(step2&&r.status==429){tfaReset()}else $("#pin").value=""}
function tfaBackup(){const e=$("#tfa");e.setAttribute("inputmode","text");e.setAttribute("autocomplete","off");e.placeholder="abcd-efgh";e.style.letterSpacing=".1em";e.blur();setTimeout(()=>e.focus(),50)}
function tfaReset(){const e=$("#tfa");e.setAttribute("inputmode","numeric");e.setAttribute("autocomplete","one-time-code");e.placeholder="123456";e.style.letterSpacing="";TFA_PIN="";$("#pin").value="";$("#pin").style.display="";$("#tfa").style.display="none";$("#tfab").style.display="none";
 $("#lhint").textContent="PIN eingeben (festgelegt in "+document.title+" → Einstellungen)"}
$("#pin").addEventListener("keydown",e=>{if(e.key==="Enter")login()});
$("#tfa").addEventListener("keydown",e=>{if(e.key==="Enter")login()});
$("#tfa").addEventListener("input",e=>{const v=e.target.value.replace(/\s/g,"");if(/^\d{6}$/.test(v))login()});
function showLogin(){$("#app").style.display="none";$("#login").style.display="block";setTimeout(()=>(TFA_PIN?$("#tfa"):$("#pin")).focus(),100)}
async function act(o,msg){try{const r=await api("/api/action",o);const j=await r.json();toast(j.ok?(msg||j.msg||"OK"):("⚠ "+(j.msg||"Fehler")));setTimeout(load,700)}catch(e){}}
async function load(){if(busy)return;busy=true;try{const r=await api("/api/state");if(r.status==503){const j=await r.json();if(j.setup){showSetup();return}$("#stt").textContent=j.error;return}
 S=await r.json();backOnline();cacheState();$("#login").style.display="none";$("#setup").style.display="none";$("#app").style.display="block";render();checkVer();if($("#gv").style.display=="block"&&!window.matchMedia("print").matches)renderGuest();
 $("#dot").style.background=(R("net")<1||(S.net&&S.net.online))?"var(--ok)":"var(--bad)";$("#stt").textContent="aktualisiert "+new Date().toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"})}
 catch(e){if(e!=="login"){$("#dot").style.background="var(--bad)";$("#stt").textContent="keine Verbindung";goneOffline()}}finally{busy=false}}
const SECAREA={"Personen":"home","Termine":"home","Listen":"lists","Szenen":"smart","Smart Home":"smart","Kameras":"cams","3D-Druck":"printers","Netzwerk":"net"};
function R(a){return S&&S.me&&S.me.rights?(S.me.rights[a]||0):2}
function sec(t,html){const a=SECAREA[t];if(a&&R(a)<1)return "";const ro=a&&a!="home"&&R(a)==1;return html?`<h2>${t}${ro?' <span class="s" style="font-weight:400">· nur ansehen</span>':""}</h2><div class="grid${ro?" ro":""}">${html}</div>`:""}
async function logout(){if(!confirm((S.me&&S.me.name?S.me.name+" – ":"")+"auf diesem Handy abmelden?"))return;await fetch("/api/logout",{method:"POST",credentials:"same-origin"});location.reload()}
function render(){let h="";
 const WA=(S.weather&&S.weather.alerts)||[];
 if(WA.length)h+=WA.slice(0,2).map(a=>`<div class="wxab" style="--wc:${a.color}" onclick="openWxa()"><span>${a.icon}</span><div><b>${esc(a.headline.replace(/^Amtliche /,""))}</b><div class="s" style="margin:0">${esc(a.level_name)}${a.when?" · "+esc(a.when):""} · antippen für Details</div></div></div>`).join("")+(WA.length>2?`<div class="s" style="margin:4px 6px" onclick="openWxa()">+ ${WA.length-2} weitere Warnungen ›</div>`:"");
 if(S.vacation&&S.vacation.active&&R("smart")>=1)h+=`<div class="vacb" ${R("smart")>=2?`onclick="openSec('vacation')"`:""}>🏖 Urlaubsmodus ${esc(S.vacation.text.replace(/^an ?/,""))||"an"}</div>`;
 $("#bh").style.display=R("history")?"":"none";$("#bc").style.display=R("calc")?"":"none";$("#bs").style.display=canSettings()?"":"none";
 $("#bu").innerHTML="👤"+(S.me&&S.me.name&&S.me.name!="Hauptnutzer"?'<span class="un"> '+esc(S.me.name.length>7?S.me.name.slice(0,6)+"…":S.me.name)+"</span>":"");
 const W=S.weather;if(W){const c=W.current;h+=`<div class="t wide" style="margin-top:12px;display:flex;gap:14px;align-items:center;flex-wrap:wrap"><div style="font-size:44px;line-height:1">${W.emoji}</div>
  <div style="flex:1;min-width:150px"><div class="big" style="margin:0;font-size:30px">${Math.round(c.temp)}°</div><div class="n" style="padding:0">${esc(W.text)}</div><div class="s">${esc(W.place)}${W.sub?" · "+esc(W.sub):""}</div>${W.warn?`<div class="s" style="color:${W.warn_color||"var(--amb)"};font-weight:600">${esc(W.warn)}</div>`:""}</div>
  <div style="display:flex;gap:10px;width:100%;justify-content:space-between">${W.days.map(d=>`<div style="text-align:center;font-size:12px;flex:1"><div class="s" style="margin:0">${esc(d.label)}</div><div style="font-size:20px">${d.emoji}</div><b>${Math.round(d.max)}°</b> <span class="s">${Math.round(d.min)}°</span>${d.prob>=30?`<div style="color:#5ab0ff">💧${d.prob}%</div>`:""}</div>`).join("")}</div></div>`}
 h+=sec("Personen",S.persons.map(p=>`<div class="t ${p.home?"on":""}"><div class="n">${p.item?"🔑 ":""}${esc(p.name)}</div><div class="s">${p.home?"seit "+esc(p.since):(p.item?"weg seit ":"unterwegs seit ")+esc(p.since)}</div><div class="big" style="color:${p.home?"var(--ok)":"var(--mut)"}">${p.home?(p.item?"✔ da":"🏠 zu Hause"):(p.item?"weg":"unterwegs")}</div></div>`).join("")+(S.vacation&&R("smart")>=1?vacTile(S.vacation):""));
 if(S.agenda&&S.agenda.length){let a="",ld=null;for(const e of S.agenda){if(e.day!==ld){a+=`<div class="day">${esc(e.day)}</div>`;ld=e.day}
  a+=`<div class="ag"><span class="c" style="background:${esc(e.color||"#4f8ff7")}"></span><span class="tm">${esc(e.time)}</span><span>${esc(e.title)}</span></div>`}
  h+=sec("Termine",`<div class="t wide">${a}</div>`)}
 if(S.lists&&R("lists")){h+=sec("Listen",["einkauf","aufgaben"].filter(k=>S.lists[k]).map(k=>{const L=S.lists[k];return `<div class="t" onclick="openList('${k}')" style="cursor:pointer"><div class="n">${L.icon} ${esc(L.name)}</div><div class="big" style="font-size:18px">${L.open?L.open+" offen":"alles erledigt 👍"}</div><div class="s">${esc(L.top.join(" · "))}${L.open>L.top.length?" …":""}</div></div>`}).join(""))}
 if(S.plugins&&S.plugins.length){h+=sec("Erweiterungen",S.plugins.map(t=>`<div class="t" ${t.tap&&R("smart")>=2?`onclick="act({key:'${t.key}',action:'tap'})" style="cursor:pointer"`:""}><div class="n">${esc(t.icon)} ${esc(t.title)}</div><div class="big" style="font-size:17px;${t.color?"color:"+esc(t.color):""}">${esc(t.value)}</div>${t.sub?`<div class="s">${esc(t.sub)}${t.tap?" ›":""}</div>`:(t.tap?'<div class="s">antippen ›</div>':"")}</div>`).join(""))}
 if(S.calls){const C=S.calls;h+=sec("Telefon",`<div class="t" onclick="openCalls()" style="cursor:pointer"><div class="n">📞 Anrufe</div><div class="s" style="${C.missed_today?"color:var(--bad)":""}">${C.missed_today?C.missed_today+" verpasst heute":"heute nichts verpasst"}</div>${C.last?`<div class="big" style="font-size:15px">${C.last.icon} ${esc(C.last.who)}</div><div class="s">${esc(C.last.when)} ›</div>`:`<div class="s">${C.error?"⚠ Anrufliste nicht lesbar ›":"noch keine Anrufe"}</div>`}</div>`)}
 h+=sec("Szenen",S.scenes.map(s=>`<div class="t" onclick="act({key:'${s.key}',action:'run'},'▶ ${esc(s.name)} gestartet')"><div class="n">${esc(s.name)}</div><div class="s">Szene · antippen zum Starten</div><div class="big">▶</div></div>`).join(""));
 h+=sec("Kameras",S.cams.map(c=>`<div class="t cam" onclick="openCam('${c.key}')"><img data-cam="${c.key}" src="${camSrc[c.key]||""}" alt=""><div class="cap"><b>${esc(c.name)}</b><span class="s" style="margin:0">${esc(c.status)}${c.ptz?" · PTZ":""}</span></div></div>`).join("")+(S.cams.length?`<div class="t" onclick="openRec('')" style="cursor:pointer"><div class="n">🎥 Aufnahmen</div><div class="s">alle Kameras</div><div class="big" style="font-size:16px">ansehen</div></div>`:""));
 h+=sec("3D-Druck",S.printers.map(p=>{const busy=["RUNNING","PREPARE","PAUSE"].includes(p.state);
  let b="";if(p.connected){if(p.state==="PAUSE")b+=`<button class="b acc" onclick="act({key:'${p.key}',action:'resume'})">Fortsetzen</button>`;
   else if(["RUNNING","PREPARE"].includes(p.state))b+=`<button class="b" onclick="act({key:'${p.key}',action:'pause'})">Pause</button>`;
   if(busy)b+=`<button class="b red" onclick="if(confirm('Druck wirklich abbrechen?'))act({key:'${p.key}',action:'stop'})">Stopp</button>`;
   if(p.light!==null)b+=`<button class="b" onclick="act({key:'${p.key}',action:'light',value:${!p.light}})">Licht ${p.light?"aus":"an"}</button>`;
   if(p.tl&&p.tl.mode=="ask"){const on=p.tl.armed||p.tl.rec!=null;b+=`<button class="b ${on?"acc":""}" onclick="act({key:'${p.key}',action:'timelapse',value:${!on}})">🎬 Zeitraffer ${on?"an":"aus"}</button>`}}
  if(p.tl&&(p.tl.mode!="off"||p.tl.rec!=null))b+=`<button class="b view" onclick="openTL()">🎬 Videos</button>`;
  return `<div class="t wide ${busy?"on":""}"><div class="n">${esc(p.name)}</div><div class="s">${esc(p.connected?p.state_text:(p.error||"nicht verbunden"))}${p.file?" · "+esc(p.file):""}</div>
  ${p.connected?`<div class="bar"><i style="width:${p.percent||0}%"></i></div><div class="row" style="justify-content:space-between"><span class="big" style="margin:0">${p.percent||0} %</span><span class="s">${esc(p.eta||"")}<br>${esc(p.layers?("Schicht "+p.layer+" / "+p.layers):"")}</span></div>
  <div class="temps">${p.temps.map(t=>`<div>${esc(t[0])}<b>${esc(t[1])}</b></div>`).join("")}</div>${p.ams.length?`<div class="ams">${p.ams.map(a=>`<i class="${a.active?"a":""}" style="background:${a.color||"transparent"}"></i>`).join("")}</div>`:""}
  ${p.cost?`<div class="s" style="margin-top:8px;color:var(--amb)">${esc(p.cost)}</div>`:""}`:""}${p.spaghetti?`<div class="s" style="margin-top:8px;color:var(--bad);font-weight:600">🍝 Die KI im Drucker meldet einen möglichen Fehldruck – bitte nachsehen</div>`:""}${p.ai?`<div class="s" style="margin-top:6px;${p.ai_off?"color:var(--amb)":""}">${esc(p.ai)}${p.ai_off?" – einschalten am Drucker oder in Bambu Studio (Druckoptionen)":""}</div>`:""}${p.tl&&p.tl.rec!=null?`<div class="s" style="margin-top:6px;color:var(--bad)">● 🎬 Zeitraffer läuft · ${p.tl.rec} Bilder</div>`:(p.tl&&p.tl.armed?`<div class="s" style="margin-top:6px">🎬 Zeitraffer startet mit dem Druck</div>`:(p.tl&&p.tl.mode=="always"?`<div class="s" style="margin-top:6px">🎬 Zeitraffer bei jedem Druck</div>`:""))}${amsHtml(p)}<div class="row">${b}${R("calc")?'<button class="b view" onclick="openCalc()">🧮 Kosten</button>':""}</div></div>`}).join("")+(S.filament?filTile(S.filament):""));
 h+=sec("Smart Home",(S.energy?`<div class="t" ${R("power")?'onclick="openPower()" style="cursor:pointer"':""}><div class="n">Verbrauch jetzt</div><div class="s" style="${S.energy.over_budget?"color:var(--bad)":""}">${S.energy.month_eur!=null?"Monat "+ceur(S.energy.month_eur)+" · ≈ "+ceur(S.energy.prog_eur,0)+(S.energy.over_budget?" ⚠":""):S.energy.count+(S.energy.count==1?" Messsteckdose":" Messsteckdosen")}</div><div class="big" style="color:var(--amb)">${fmt(S.energy.power)} W</div><div class="s">${R("power")?"⚡ Stromkosten ansehen ›":""}</div></div>`:"")+(S.appliances||[]).map(applTile).join("")+(S.battery?batTile(S.battery):"")+(S.heating?`<div class="t" onclick="openHeat()" style="cursor:pointer"><div class="n">🔥 Heizungs-Check</div><div class="s">Hinweise aus dem Temperaturverlauf</div><div class="big" style="font-size:16px">ansehen ›</div></div>`:"")+S.smart.map(d=>{
  if(!d.present)return `<div class="t off"><div class="n">${esc(d.name)}</div><div class="s">nicht erreichbar</div></div>`;
  if(d.thermostat)return `<div class="t"><div class="n">${esc(d.name)}</div><div class="s">${d.window?"Fenster offen · ":""}Soll ${esc(d.soll_txt)}${d.battery_low?" · 🔋 schwach":""}</div><div class="row" style="justify-content:space-between"><span class="big" style="margin:0;color:var(--heat)">${d.ist!=null?fmt(d.ist)+" °C":"–"}</span><span><button class="b" onclick="setT('${d.key}',${d.soll},-0.5)">−</button> <button class="b" onclick="setT('${d.key}',${d.soll},0.5)">+</button></span></div></div>`;
  const MC={bad:"var(--bad)",warn:"var(--amb)",ok:"var(--ok)",mut:"var(--mut)"};
  if(d.sensor_txt&&d.power==null)return `<div class="t ${d.sensor_mood=="bad"?"alarm":""}"><div class="n">${esc(d.name)}</div><div class="s">${esc(d.kind)}${d.vendor?" · "+esc(d.vendor):""}${d.battery_low?" · 🔋 schwach":""}</div><div class="big" style="color:${MC[d.sensor_mood]||"var(--fg)"}">${esc(d.sensor_txt)}</div>${d.temp!=null?`<div class="s">${fmt(d.temp)} °C</div>`:""}</div>`;
  if(d.media){const k=d.media.keys||[];return `<div class="t ${d.on?"on":""}">${d.switchable?`<button class="sw ${d.on?"on":""}" onclick="act({key:'${d.key}',action:'toggle'},'${esc(d.name)} ${d.on?"aus":"an"}')"></button>`:""}<div class="n">${esc(d.name)}</div><div class="s">${d.on?"An":"Aus"}${d.vendor&&d.vendor!=d.name?" · "+esc(d.vendor):""}</div><div class="big" style="font-size:${(d.media.now||"").length>12?17:22}px;color:${d.on?"var(--amb)":"var(--mut)"}">${esc(d.media.now||(d.on?"läuft":"aus"))}</div><div class="row">${d.on&&d.media.box=="sky"?`<button class="b" onclick="mkey('${d.key}','pause')">❚❚</button><button class="b" onclick="mkey('${d.key}','play')">▶</button>`:(d.on&&k.includes("vol_up")?`<button class="b" onclick="mkey('${d.key}','vol_down')">🔉</button><button class="b" onclick="mkey('${d.key}','vol_up')">🔊</button>`:"")}<button class="b view" onclick="openRemote('${d.key}')">🎛 Fernbedienung</button></div></div>`}
  const lv=d.level!=null&&d.power==null;
  return `<div class="t ${d.on?"on":""}">${d.switchable?`<button class="sw ${d.on?"on":""}" onclick="act({key:'${d.key}',action:'toggle'},'${esc(d.name)} ${d.on?"aus":"an"}')"></button>`:""}<div class="n">${esc(d.name)}</div><div class="s">${d.switchable?(d.on?"An":"Aus"):esc(d.kind)}${d.vendor?" · "+esc(d.vendor):""}${d.energy!=null?" · "+fmt(d.energy)+" kWh":""}</div>
  ${lv?`<div class="row" style="justify-content:space-between"><span class="big" style="margin:0;color:${d.on?"var(--amb)":"var(--mut)"}">${d.on?d.level+" %":"aus"}</span><span><button class="b" onclick="act({key:'${d.key}',action:'level',value:${Math.max(0,(d.on?d.level:0)-10)}})">−</button> <button class="b" onclick="act({key:'${d.key}',action:'level',value:${Math.min(100,(d.on?d.level:0)+10)}})">+</button></span></div>`:`<div class="big" style="color:${d.power>0.5?"var(--amb)":"var(--fg)"}">${d.power!=null?fmt(d.power)+" W":(d.temp!=null?fmt(d.temp)+" °C":"")}</div>`}</div>`}).join(""));
 const n=S.net||{};h+=sec("Netzwerk",`<div class="t"><div class="n">Internet</div><div class="s">${n.online?"Online":"Offline"}</div><div class="big" style="font-size:16px">↓ ${esc(n.down||"–")}<br>↑ ${esc(n.up||"–")}</div></div>
  <div class="t"><div class="n">Geräte</div><div class="s">im Heimnetz</div><div class="big">${n.hosts??"–"} online</div></div>`+(S.guest?guestTile(S.guest):"")+(S.devices||[]).map(d=>{const off=(d.children||[]).filter(k=>!k.online).map(k=>k.name);return `<div class="t ${d.online?"on":""}"><div class="n">${esc(d.name)}</div><div class="s">${esc(d.category||"Gerät")}</div><div class="big" style="font-size:16px;color:${d.online?"var(--ok)":"var(--mut)"}">${d.online?"● online":"○ offline"}</div>${(d.children||[]).length||(d.radio||[]).length?`<div class="s" style="${off.length?"color:var(--amb)":""}">${esc(off.length?"⚠ offline: "+off.join(", "):d.sub)}</div>`:""}</div>`}).join("")+(S.wol||[]).map(w=>`<div class="t ${w.online?"on":""}"><div class="n">${esc(w.name)}</div><div class="s">Wake-on-LAN</div>${w.online?'<div class="big" style="color:var(--ok)">● an</div>':(w.busy?'<div class="big" style="color:var(--amb);font-size:16px">weckt …</div>':`<div class="row"><button class="b acc" onclick="act({key:'${w.key}',action:'wake'})">⏻ Aufwecken</button></div>`)}</div>`).join("")+S.wlan.map(w=>`<div class="t ${w.enabled?"on":""}"><button class="sw ${w.enabled?"on":""}" onclick="if(confirm('WLAN ${esc(w.ssid)} (${esc(w.band)}) ${w.enabled?"ausschalten":"einschalten"}?'))act({key:'${w.key}',action:'wlan',value:${!w.enabled}})"></button><div class="n">${esc(w.ssid)}</div><div class="s">${esc(w.box?w.box+" · ":"")}${esc(w.band)}</div><div class="big" style="font-size:16px">${w.clients??"–"} Geräte</div></div>`).join(""));
 $("#main").innerHTML=h||'<p class="s" style="padding:20px">Noch keine Daten – läuft Hauskern auf dem PC?</p>';renderDiag();refreshCams();if(RMK&&document.activeElement!==$("#rmch"))renderRemote()}
let H={id:"",range:0};
const WDS=["So","Mo","Di","Mi","Do","Fr","Sa"];
function nf(v,d){return v==null?"–":v.toFixed(d).replace(".",",")}
function vfmt(v,u){if(v==null)return"–";const a=Math.abs(v);if(u==="kWh"&&a<1)return Math.round(v*1000)+" Wh";const d=a>=100?0:(a>=10||["°C","W","ms","%"].includes(u)?1:2);return nf(v,d)+(u?" "+u:"")}
function ticks(lo,hi){if(hi<=lo)hi=lo+1;const raw=(hi-lo)/4,m=Math.pow(10,Math.floor(Math.log10(raw)));const st=[1,2,2.5,5,10].map(x=>x*m).find(x=>x>=raw);const a=Math.floor(lo/st+1e-9)*st,b=Math.ceil(hi/st-1e-9)*st;const out=[];for(let v=a;v<=b+st*.01;v+=st)out.push(+v.toFixed(10));return{t:out,st}}
function tfmt(v,st){let d=st<1?Math.max(0,-Math.floor(Math.log10(st)+1e-9)):0;if(st<1&&(st*Math.pow(10,d))%1)d++;if(st>=1&&st%1)d=1;return v.toFixed(d).replace(".",",")}
function dlab(ts,span){const d=new Date(ts*1000);if(span<=2*86400)return String(d.getHours()).padStart(2,"0")+":00";if(span<=40*86400)return WDS[d.getDay()]+" "+d.getDate()+".";return (d.getMonth()+1)+"/"+String(d.getFullYear()).slice(2)}
function dfull(ts,b,month){const d=new Date(ts*1000);if(month)return (d.getMonth()+1)+"/"+d.getFullYear();let s=WDS[d.getDay()]+" "+d.getDate()+"."+(d.getMonth()+1)+".";if(b<86400)s+=" "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");return s}
function xticks(t0,t1){const span=t1-t0;const out=[];if(span<=2*86400){let d=new Date(t0*1000);d.setMinutes(0,0,0);const step=span<=86400?6:12;while(d.getTime()/1000<=t1){if(d.getTime()/1000>=t0&&d.getHours()%step===0)out.push(d.getTime()/1000);d.setHours(d.getHours()+1)}}
 else if(span<=40*86400){let d=new Date(t0*1000);d.setHours(0,0,0,0);const step=span<=8*86400?1:7;let i=0;while(d.getTime()/1000<=t1){if(d.getTime()/1000>=t0&&(step===1||d.getDay()===1))out.push(d.getTime()/1000);d.setDate(d.getDate()+1);if(++i>400)break}}
 else{let d=new Date(t0*1000);d.setHours(0,0,0,0);d.setDate(1);let i=0;while(d.getTime()/1000<=t1){if(d.getTime()/1000>=t0&&d.getMonth()%3===0)out.push(d.getTime()/1000);d.setMonth(d.getMonth()+1);if(++i>30)break}}return out}
function chart(c,o){const W=Math.max(280,c.clientWidth-12),Hh=o.h||190,L=36,R=8,T=8,B=20;const x0=L,x1=W-R,y0=T,y1=Hh-B;
 const vals=[];o.series.forEach(s=>s.pts.forEach(p=>{if(p[1]!=null)vals.push(p[1])}));
 if(!vals.length){c.querySelector(".plot").innerHTML=`<div class="s" style="padding:40px 10px;text-align:center">Noch keine Daten für diesen Zeitraum</div>`;return}
 let lo=Math.min(...vals),hi=Math.max(...vals);if(o.kind==="bars"||o.zero)lo=Math.min(0,lo);const pad=(hi-lo)*.08||(Math.abs(hi)*.1||1);
 const tk=ticks((o.kind==="bars"||o.zero)&&lo>=0?lo:lo-pad,hi+pad);lo=tk.t[0];hi=tk.t[tk.t.length-1];
 const X=t=>x0+(t-o.t0)/(o.t1-o.t0)*(x1-x0),Y=v=>y1-(v-lo)/(hi-lo||1)*(y1-y0);
 let g=`<svg viewBox="0 0 ${W} ${Hh}" height="${Hh}">`;
 tk.t.forEach(t=>{g+=`<line x1="${x0}" x2="${x1}" y1="${Y(t)}" y2="${Y(t)}" stroke="#30333c"/><text x="${x0-5}" y="${Y(t)+4}" fill="#8e96a3" font-size="10" text-anchor="end">${tfmt(t,tk.st)}</text>`});
 xticks(o.t0,o.t1).forEach(t=>{g+=`<line x1="${X(t)}" x2="${X(t)}" y1="${y0}" y2="${y1}" stroke="#30333c" stroke-dasharray="2 4"/><text x="${X(t)}" y="${Hh-5}" fill="#8e96a3" font-size="10" text-anchor="middle">${dlab(t,o.t1-o.t0)}</text>`});
 if(o.kind==="bars"){o.series[0].pts.forEach(p=>{const a=Math.max(x0,X(p[0])+.5),b=Math.min(x1,X(p[0]+o.bucket)-.5);g+=`<rect x="${a}" y="${Y(p[1])}" width="${Math.max(1,b-a)}" height="${Math.max(0,Y(0)-Y(p[1]))}" fill="${o.series[0].color}" rx="1.5"/>`})}
 else o.series.forEach(s=>{const gap=Math.max(3*o.bucket,300);let d="",prev=null,py=null;s.pts.forEach(p=>{if(p[1]==null)return;const x=X(p[0]+o.bucket/2),y=Y(p[1]);
  if(prev===null||p[0]-prev>gap)d+=`M${x.toFixed(1)} ${y.toFixed(1)}`;else{if(s.step)d+=`L${x.toFixed(1)} ${py.toFixed(1)}`;d+=`L${x.toFixed(1)} ${y.toFixed(1)}`}prev=p[0];py=y});
  g+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" ${s.dash?'stroke-dasharray="5 3"':""}/>`});
 g+=`<line class="cur" x1="0" x2="0" y1="${y0}" y2="${y1}" stroke="#6a6f7b" style="display:none"/></svg>`;
 c.querySelector(".plot").innerHTML=g;const svg=c.querySelector("svg"),tip=c.querySelector(".tip"),cur=svg.querySelector(".cur");
 const show=ev=>{const r=svg.getBoundingClientRect();const px=((ev.touches?ev.touches[0].clientX:ev.clientX)-r.left)*W/r.width;if(px<x0||px>x1){return}
  const ts=o.t0+(px-x0)/(x1-x0)*(o.t1-o.t0);let rows=[],tx=null;
  o.series.forEach(s=>{let best=null;s.pts.forEach(p=>{if(p[1]==null)return;if(o.kind==="bars"){if(p[0]<=ts&&ts<p[0]+o.bucket)best=p}else{const c=p[0]+o.bucket/2;if(!best||Math.abs(c-ts)<Math.abs(best[0]+o.bucket/2-ts))best=p}});
   if(best&&(o.kind==="bars"||Math.abs(best[0]+o.bucket/2-ts)<=Math.max(3*o.bucket,300))){rows.push((s.name?esc(s.name)+": ":"")+"<b>"+vfmt(best[1],o.unit)+"</b>");tx=tx||best[0]}});
  if(!rows.length){tip.style.display="none";cur.style.display="none";return}
  const cx=o.kind==="bars"?px:X(tx+o.bucket/2);cur.setAttribute("x1",cx);cur.setAttribute("x2",cx);cur.style.display="";
  tip.innerHTML=`<span class="s" style="margin:0">${dfull(tx,o.bucket,o.month)}</span><br>`+rows.join("<br>");tip.style.display="block";tip.style.top=(c.querySelector(".plot").offsetTop+6)+"px";
  const left=cx*r.width/W+6;tip.style.left=(left+tip.offsetWidth+16>r.width?left-tip.offsetWidth-14:left+12)+"px"};
 svg.addEventListener("touchstart",show,{passive:true});svg.addEventListener("touchmove",show,{passive:true});svg.addEventListener("mousemove",show);
 svg.addEventListener("mouseleave",()=>{tip.style.display="none";cur.style.display="none"})}
function chartBox(title,series){const lg=series.length>1||series[0]&&series[0].name?`<div class="lg">${series.map(s=>`<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`:"";
 return `<div class="ch"><div class="ti">${esc(title)}</div>${lg}<div class="plot"></div><div class="tip"></div></div>`}
function openHist(){$("#hv").style.display="block";document.body.style.overflow="hidden";loadHist(H.id,H.range)}
function closeHist(){$("#hv").style.display="none";document.body.style.overflow=""}
async function loadHist(id,rng){H.id=id;H.range=rng;$("#hbody").insertAdjacentHTML("afterbegin",'<div class="s" id="hload" style="padding:8px 4px">lädt …</div>');
 let d;try{const r=await api("/api/history?id="+encodeURIComponent(id||"")+"&range="+rng);d=await r.json()}catch(e){return}
 if(d.error&&!d.series){$("#hbody").innerHTML=`<p class="s">${esc(d.error)}</p>`;return}
 H.id=d.id;$("#hsrc").innerHTML=d.sources.map(s=>`<option value="${esc(s.id)}" ${s.id===d.id?"selected":""}>${esc(s.label)}</option>`).join("");
 $("#hrg").innerHTML=d.ranges.map((r,i)=>`<button class="${i===d.range?"on":""}" onclick="loadHist(H.id,${i})">${esc(r)}</button>`).join("");
 let h=`<div class="st">${(d.stats||[]).map(s=>`<div><span class="s" style="margin:0">${esc(s[0])}</span><b>${esc(s[1])}</b></div>`).join("")}</div>`+chartBox(d.title,d.series);
 const L=d.lower;if(L){if(L.kind==="table")h+=`<div class="ch"><div class="ti">${esc(L.title)}</div><div class="out">${L.rows.length?L.rows.map(r=>`<div><span>${esc(r[1])}<br><span class="s" style="margin:0">${esc(r[0])} · ${esc(r[2])} · ${esc(r[3])}</span></span><span style="white-space:nowrap">${esc(r[4])}</span></div>`).join(""):'<div>Keine Einträge in diesem Zeitraum</div>'}</div></div>`;
  else if(L.kind==="outages")h+=`<div class="ch"><div class="ti">${esc(L.title)}</div><div class="out">${L.rows.length?L.rows.map(r=>`<div><span>${dfull(r[0],60)}</span><span class="s" style="margin:0">${fmtDur(r[1]-r[0])}</span></div>`).join(""):'<div>Keine Ausfälle in diesem Zeitraum 🎉</div>'}</div></div>`;
  else h+=chartBox(L.title,L.series)}
 $("#hbody").innerHTML=h;const boxes=document.querySelectorAll("#hbody .ch");
 chart(boxes[0],{series:d.series,t0:d.t0,t1:d.t1,unit:d.unit||"",bucket:d.bucket,zero:d.y_zero,kind:d.chart,month:d.bucket>=28*86400,h:210});
 if(L&&L.kind!=="outages"&&L.kind!=="table")chart(boxes[1],{series:L.series,t0:d.t0,t1:d.t1,unit:L.unit||"",bucket:L.bucket||d.bucket,kind:L.chart,month:L.month,h:150})}
function fmtDur(s){s=Math.round(s);if(s<3600)return Math.max(1,Math.round(s/60))+" Min";if(s<86400)return Math.floor(s/3600)+" Std "+Math.round(s%3600/60)+" Min";return Math.floor(s/86400)+" T "+Math.floor(s%86400/3600)+" Std"}
function amsHtml(p){if(!p.ams_units||!p.ams_units.length)return"";return p.ams_units.map(u=>`<div class="amsu"><div class="hd"><b>${esc(u.name)}</b><span class="s" style="margin:0">${esc([u.hum,u.temp?Math.round(u.temp)+" °C":"",u.drying_min?"trocknet "+Math.floor(u.drying_min/60)+":"+String(u.drying_min%60).padStart(2,"0")+" h":""].filter(x=>x).join(" · "))}</span></div>
 <div class="spools">${u.trays.map(t=>{const f=t.remain==null?100:t.remain;const c=t.color||"#555a66";return `<div class="sp"><div class="tube ${t.active?"a":""}">${t.empty?'<div class="pc" style="bottom:24px;color:var(--mut)">leer</div>':`<div class="fill" style="height:calc(${f}% - 6px);background:${c};${t.remain==null?"opacity:.45":""}"></div><div class="pc">${t.remain==null?"?":t.remain+"%"}</div>`}</div><b>${esc(t.empty?"–":t.type)}</b><span class="s" style="margin:0">${t.grams!=null?"~"+t.grams+" g":esc(t.brand||t.slot)}</span></div>`}).join("")}</div></div>`).join("")}
let C=null,cTimer=null,cJob=null;
const eur=v=>(Math.round(v*100)/100).toLocaleString("de-DE",{minimumFractionDigits:2,maximumFractionDigits:2})+" €";
function ceur(v,d=2){return v==null?"–":(+v).toLocaleString("de-DE",{minimumFractionDigits:d,maximumFractionDigits:d})+" €"}
function kwh(v,d=1){return v==null?"–":(+v).toLocaleString("de-DE",{minimumFractionDigits:d,maximumFractionDigits:d})+" kWh"}
function closeOv(id){$("#"+id).style.display="none";document.body.style.overflow=""}
async function openPower(){$("#pv").style.display="block";document.body.style.overflow="hidden";$("#pbody").innerHTML='<p class="s" style="padding:20px 0">lädt …</p>';
 const P=await (await api("/api/power")).json();if(!P.ready){$("#pbody").innerHTML='<p class="s" style="padding:20px 0">Die Auswertung läuft noch – bitte gleich nochmal öffnen.</p>';return}
 const t=P.total,mx=Math.max(0.5,...P.months.map(m=>m.eur||0));
 let h=`<div class="pc"><div class="t"><div class="n">${esc(t.cur_name)}</div><div class="s">bisher · Tag ${t.day}/${t.days}</div><div class="big" style="font-size:22px">${ceur(t.month_eur)}</div><div class="s">${kwh(t.month_kwh)}</div></div>
 <div class="t"><div class="n">Hochrechnung</div><div class="s" style="${t.over_budget?"color:var(--bad)":""}">${t.budget_eur?(t.over_budget?"⚠ über Budget "+ceur(t.budget_eur,0):"✓ Budget "+ceur(t.budget_eur,0)):"ganzer Monat"}</div><div class="big" style="font-size:22px;color:var(--amb)">≈ ${ceur(t.prog_eur,0)}</div><div class="s">≈ ${kwh(t.prog_kwh,0)}</div></div>
 <div class="t"><div class="n">${esc(t.prev_name)}</div><div class="s">letzter Monat</div><div class="big" style="font-size:22px">${ceur(t.prev_eur)}</div><div class="s">${kwh(t.prev_kwh)}</div></div>
 <div class="t"><div class="n">12 Monate</div><div class="s">${t.standby_eur_year>=1?"Grundlast ≈ "+ceur(t.standby_eur_year,0)+"/Jahr":"alle Messsteckdosen"}</div><div class="big" style="font-size:22px">${ceur(t.year_eur,0)}</div><div class="s">${kwh(t.year_kwh,0)}</div></div></div>
 <div class="s">Kosten pro Monat · ${(+P.price_ct).toLocaleString("de-DE")} ct/kWh</div><div class="mb">${P.months.map((m,i)=>`<div class="${i==P.months.length-1?"cur":""}"><span>${m.eur?Math.round(m.eur):""}</span><i style="height:${Math.round((m.eur||0)/mx*120)}px"></i>${esc(m.m)}</div>`).join("")}</div>`;
 h+=P.devices.map(d=>`<div class="dv"><div style="display:flex;justify-content:space-between"><b>${esc(d.name)}</b><span style="color:var(--amb)">${d.now_w==null?"":fmt(d.now_w)+" W"}</span></div>
 <div class="r"><span>Monat bisher</span><span>${kwh(d.month_kwh)} · ${ceur(d.month_eur)}</span></div><div class="r"><span>Hochrechnung</span><span>≈ ${ceur(d.prog_eur)}</span></div><div class="r"><span>Vormonat</span><span>${ceur(d.prev_eur)}</span></div>
 <div class="r"><span>Trend</span><span style="${d.warn?"color:var(--bad)":""}">${esc(d.trend)}</span></div>${d.standby_w!=null?`<div class="r"><span>Grundlast</span><span>${fmt(d.standby_w)} W · ${ceur(d.standby_eur_year)}/Jahr</span></div>`:""}</div>`).join("");
 $("#pbody").innerHTML=h}
function guestTile(g){const off=g.off_at?" · aus um "+new Date(g.off_at*1000).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"}):"";
 return `<div class="t ${g.enabled?"on":""}" onclick="if(!event.target.closest('.sw'))openGuest()" style="cursor:pointer"><button class="sw ${g.enabled?"on":""}" onclick="act({key:'guest:wlan',action:'guest',value:${!g.enabled}})"></button><div class="n">Gast-WLAN</div><div class="s">${esc(g.ssid)}${esc(off)}</div><div class="big" style="font-size:16px;${g.enabled?"color:var(--amb)":""}">${g.enabled?(g.clients==null?"an":g.clients+(g.clients==1?" Gast":" Gäste")):"aus"}</div><div class="s">▣ QR-Code zeigen ›</div></div>`}
let gPw=false;
function openGuest(){$("#gv").style.display="block";document.body.style.overflow="hidden";renderGuest()}
function renderGuest(){const g=S.guest;if(!g){$("#gbody").innerHTML='<p class="s">Kein Gast-WLAN gefunden.</p>';return}
 const hs=[[0,"nicht automatisch aus"],[1,"nach 1 Std. aus"],[3,"nach 3 Std. aus"],[6,"nach 6 Std. aus"],[12,"nach 12 Std. aus"],[24,"nach 24 Std. aus"],[72,"nach 3 Tagen aus"]];
 const off=g.off_at?"schaltet um "+new Date(g.off_at*1000).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"})+" Uhr ab":"";
 $("#gbody").innerHTML=`<div class="np" style="display:flex;align-items:center;justify-content:space-between;margin-top:14px"><div><b style="color:${g.enabled?"var(--amb)":"var(--mut)"}">${g.enabled?"● eingeschaltet":"○ ausgeschaltet"}</b><div class="s">${esc(off)}</div></div>
 <button class="b ${g.enabled?"":"acc"}" onclick="guestSet(${!g.enabled})">${g.enabled?"Ausschalten":"Einschalten"}</button></div>
 <select id="ghrs" class="np" style="width:100%;margin-top:10px">${hs.map(([h,t])=>`<option value="${h}" ${h==g.hours?"selected":""}>Beim Einschalten: ${t}</option>`).join("")}</select>
 ${g.qr?`<div class="qrb">${g.qr}</div><p class="gk"><span class="l">Kamera aufs Handy halten und „Verbinden“ tippen</span></p>`:'<p class="s" style="margin:20px 0">QR-Code wird geladen …</p>'}
 <div class="gk"><div class="l">WLAN-Name</div><div class="v">${esc(g.ssid)}</div></div>
 <div class="gk" onclick="gPw=!gPw;renderGuest()"><div class="l">Passwort</div><div class="v">${g.password?(gPw?esc(g.password):"••••••••  <span class='s np'>(antippen)</span>"):"–"}</div></div>
 <div class="row np" style="justify-content:center;margin-top:16px"><button class="b" onclick="gPw=true;renderGuest();setTimeout(print,100)">🖨 Drucken</button><button class="b" onclick="if(confirm('Neues Passwort vergeben? Verbundene Gäste müssen sich neu verbinden.'))act({key:'guest:wlan',action:'guest_pw'}).then(()=>setTimeout(()=>load().then(renderGuest),5000))">Neues Passwort</button></div>`}
async function guestSet(on){await act({key:'guest:wlan',action:'guest',value:on,hours:+$("#ghrs").value});setTimeout(()=>{if($("#gv").style.display=="block")renderGuest()},1500)}
/* ---------- Unterwegs: letzter Stand, wenn Hauskern nicht erreichbar ist ---------- */
let OFF=false,lastSave=0;
function cacheState(){if(Date.now()-lastSave<60000)return;lastSave=Date.now();try{const c=JSON.parse(JSON.stringify(S));if(c.guest){c.guest.password="";c.guest.qr=""}localStorage.setItem("hk_last",JSON.stringify({t:Date.now(),S:c}))}catch(e){}}
function goneOffline(){let c=null;try{c=JSON.parse(localStorage.getItem("hk_last")||"null")}catch(e){}
 OFF=true;document.body.classList.add("offline");
 if((!S||!S.me)&&c){S=c.S;$("#login").style.display="none";$("#setup").style.display="none";$("#app").style.display="block";render()}
 const t=c?new Date(c.t):null,when=t?(t.toDateString()==new Date().toDateString()?t.toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"}):t.toLocaleDateString("de-DE",{day:"2-digit",month:"2-digit"})+" "+t.toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"})):"";
 $("#off").innerHTML="📴 Keine Verbindung zu Hauskern"+(when?" – du siehst den Stand von "+when:"")+".<br><b>Unterwegs? VPN einschalten ›</b>";$("#off").style.display="block"}
function backOnline(){if(!OFF)return;OFF=false;document.body.classList.remove("offline");$("#off").style.display="none"}
function vpnHelp(){$("#vh").style.display="block";document.body.style.overflow="hidden";$("#vh_b").innerHTML=`
 <p class="hint" style="font-size:15px;color:var(--fg)">Hauskern ist nur im Heimnetz erreichbar. Unterwegs verbindet ein <b>VPN</b> dein Handy sicher mit zu Hause – Hauskern bleibt trotzdem privat.</p>
 <div class="li" style="cursor:default;display:block"><div class="tt">1 · VPN einrichten (einmalig)</div><div class="su" style="line-height:1.5">
 <b>FRITZ!Box:</b> fritz.box → Internet → Freigaben → VPN (WireGuard) → „VPN-Verbindung hinzufügen“ → „Einzelgerät verbinden“. Es erscheint ein QR-Code.<br>
 <b>iPhone:</b> App „WireGuard“ aus dem App Store laden → ＋ → „Aus QR-Code erstellen“ → Code scannen.<br>
 <b>Anderer Router:</b> Viele Router können WireGuard ebenfalls – sonst übernimmt das später der Heimserver.</div></div>
 <div class="li" style="cursor:default;display:block"><div class="tt">2 · Automatisch einschalten lassen</div><div class="su" style="line-height:1.5">
 In der WireGuard-App den Tunnel antippen → <b>Bearbeiten</b> → <b>On-Demand / „Bei Bedarf aktivieren“</b>:<br>• <b>Mobilfunk</b> einschalten<br>• <b>WLAN</b> einschalten, aber dein <b>Heim-WLAN als Ausnahme</b> eintragen<br>
 Dann ist das VPN unterwegs automatisch an und zu Hause aus – du musst nie daran denken.</div></div>
 <div class="li" style="cursor:default;display:block"><div class="tt">3 · Hauskern öffnen</div><div class="su" style="line-height:1.5">Einfach wie gewohnt über das Symbol auf dem Home-Bildschirm. Ist die Seite schon offen, aktualisiert sie sich von selbst.</div></div>
 <p class="hint">Hinweis: Wird Hauskern unterwegs ganz neu geöffnet, während das VPN aus ist, zeigt Safari nur einen Fehler – der gespeicherte Stand erscheint, wenn die Seite schon offen war.</p>`}

/* ---------- Batterien ---------- */
function batCol(b){return b.low||b.pct<=20?"var(--bad)":(b.pct<=40?"var(--amb)":"var(--ok)")}
function batTile(B){const w=B.worst;return `<div class="t" onclick="openBat()" style="cursor:pointer"><div class="n">🔋 Batterien</div><div class="s">${B.count} Geräte${B.low?` · <b style="color:var(--bad)">${B.low} schwach</b>`:""}</div><div class="big" style="font-size:16px;color:${batCol(w)}">${esc(w.name)} ${w.pct} %</div><div class="s">${esc(w.left)} ›</div></div>`}
async function openBat(){$("#bv").style.display="block";document.body.style.overflow="hidden";const j=await aGet("/api/battery");
 $("#bbody").innerHTML=`<p class="hint">Sortiert nach „wird zuerst leer“. Die Schätzung wird mit der Zeit genauer – Hauskern merkt sich jeden Tag den Stand.</p>`+(j.items||[]).map(b=>`<div class="bat"><div class="row" style="justify-content:space-between"><b>${esc(b.name)}</b><b style="color:${batCol(b)}">${b.pct} %</b></div><div class="bar"><i style="width:${Math.max(3,b.pct)}%;background:${batCol(b)}"></i></div><div class="s" style="margin-top:6px">${esc(b.left)}${b.changed?" · gewechselt am "+esc(b.changed):""}${b.present?"":" · nicht erreichbar"}</div></div>`).join("")||'<p class="hint">Keine Geräte mit Batterie gefunden.</p>'}

/* ---------- Was ist neu? ---------- */
function seenVer(){try{return localStorage.getItem("hk_seen_ver")||""}catch(e){return ""}}
function checkVer(){if(!S.version)return;const s=seenVer();if(s==S.version){$("#wn").style.display="none";return}
 $("#wn").innerHTML=`✨ <b>Neu in Hauskern ${esc(S.version)}</b> – antippen zum Ansehen`;$("#wn").style.display="block"}
async function openWN(){$("#wv").style.display="block";document.body.style.overflow="hidden";$("#wbody").innerHTML='<p class="hint">lade …</p>';
 const j=await aGet("/api/whatsnew?since="+encodeURIComponent(seenVer()));
 $("#wbody").innerHTML=(j.sections||[]).map(x=>`<div class="wns"><h3>Version ${esc(x.v)}: ${esc(x.title)}</h3>${x.html}</div>`).join("")||'<p class="hint">Keine Neuigkeiten.</p>'}
function closeWN(){try{localStorage.setItem("hk_seen_ver",S.version||"")}catch(e){}$("#wv").style.display="none";document.body.style.overflow="";$("#wn").style.display="none"}

/* ---------- Einkaufsliste & Aufgaben ---------- */
let LS={cur:"einkauf",data:null};
async function openList(k){LS.cur=k||"einkauf";$("#lsv").style.display="block";document.body.style.overflow="hidden";const j=await aGet("/api/lists");LS.data=j.lists;renderList()}
function renderList(){const D=LS.data;if(!D)return;const L=D[LS.cur];const ed=R("lists")>=2;
 $("#lst").textContent=L.icon+" "+L.name;$("#lsadd").style.display=ed?"flex":"none";
 $("#lsin").placeholder=LS.cur=="einkauf"?"z. B. Milch, Eier, Brot":"z. B. Müll rausbringen";
 $("#lstabs").innerHTML=Object.keys(D).map(k=>`<button class="b ${k==LS.cur?"acc":""}" onclick="LS.cur='${k}';renderList()">${D[k].icon} ${esc(D[k].name)} ${D[k].items.filter(i=>!i.done).length||""}</button>`).join("");
 const open=L.items.filter(i=>!i.done),done=L.items.filter(i=>i.done);
 let h=open.map(i=>`<div class="li2" onclick="${ed?`lsOp('done','${i.id}')`:""}"><span class="ck"></span><span class="tx2">${esc(i.text)}</span>${i.by?`<span class="by">${esc(i.by)}</span>`:""}</div>`).join("")||'<p class="hint">Nichts offen 👍</p>';
 if(done.length){h+=`<div class="row" style="justify-content:space-between;margin:16px 0 4px"><span class="s">Erledigt</span>${ed?`<button class="b" onclick="lsOp('clear')">Erledigte löschen</button>`:""}</div>`+
  done.map(i=>`<div class="li2 d"><span class="ck" onclick="${ed?`lsOp('undone','${i.id}')`:""}">✓</span><span class="tx2" onclick="${ed?`lsOp('undone','${i.id}')`:""}">${esc(i.text)}</span>${ed?`<span class="x" onclick="lsOp('remove','${i.id}')">✕</span>`:""}</div>`).join("")}
 $("#lsbody").innerHTML=h+'<p class="hint" style="margin-top:14px">Tippen = abhaken. Mehrere auf einmal mit Komma. Mit Siri: „Milch und Eier auf die Liste“. Erledigte verschwinden nach 7 Tagen von selbst.</p>'}
async function lsOp(op,id){const j=await aPost("/api/lists",{op,list:LS.cur,id});if(j.lists){LS.data=j.lists;renderList()}if(!j.ok)toast("⚠ "+(j.msg||"Fehler"));if(navigator.vibrate)navigator.vibrate(10)}
async function lsAdd(){const v=$("#lsin").value.trim();if(!v)return;const j=await aPost("/api/lists",{op:"add",list:LS.cur,text:v});if(j.lists){LS.data=j.lists;$("#lsin").value="";renderList()}toast(j.ok?"✓ "+j.msg:"⚠ "+(j.msg||"Fehler"))}

/* ---------- Anrufe ---------- */
async function openCalls(){$("#clv").style.display="block";document.body.style.overflow="hidden";const j=await aGet("/api/calls");
 let h=j.error?`<p class="hint" style="color:var(--amb)">⚠ ${esc(j.error)}</p>`:`<p class="hint">Direkt von der FRITZ!Box. Auf 📲 tippen ruft zurück.</p>`;
 h+=(j.items||[]).map(c=>{const m=c.type=="missed"||c.type=="tam";const tel=(c.tel||"").replace(/[^0-9+*#]/g,"");return `<div class="call ${m?"m":""}"><div style="font-size:20px">${c.icon}</div><div class="w"><b>${esc(c.who)}</b><div class="s">${esc(c.when)} · ${esc(c.label)}${c.msg?" · 📼 Nachricht":""}${c.min?" · "+c.min+" Min.":""}${c.number?" · "+esc(c.number):""}</div></div>${tel?`<a href="tel:${tel}" aria-label="Zurückrufen">📲</a>`:""}</div>`}).join("")||(j.error?"":'<p class="hint">Noch keine Anrufe.</p>');
 $("#clbody").innerHTML=h}

/* ---------- Heizungs-Check ---------- */
async function openHeat(){$("#hcv").style.display="block";document.body.style.overflow="hidden";$("#hcbody").innerHTML='<p class="hint">rechne …</p>';const j=await aGet("/api/heating");
 const ic={warn:"⚠️",tip:"💡",ok:"✅",info:"ℹ️"};const s=j.summary||{};
 let h=`<p class="hint">Letzte ${j.days} Tage · ${s.tips?s.tips+" Hinweis"+(s.tips==1?"":"e"):"keine Auffälligkeiten 👍"}. Hauskern stellt dabei nichts um – du entscheidest.${j.persons?"":" Mit Personen (Anwesenheit) sieht der Check auch, ob geheizt wird, wenn niemand da ist."}</p>`;
 h+=(j.rooms||[]).map(r=>`<div class="bat"><div class="row" style="justify-content:space-between"><b>${esc(r.name)}</b><span class="s">${r.avg_ist!=null?"Ø "+fmt(r.avg_ist)+" °C":""}${r.heat_h_week!=null?" · "+r.heat_h_week+" Std./Woche geheizt":""}</span></div>${r.findings.map(f=>`<div class="hf"><i>${ic[f.level]||"•"}</i><span class="${f.level=="ok"||f.level=="info"?"s":""}">${esc(f.text)}</span></div>`).join("")||'<div class="s" style="margin-top:6px">Nichts Auffälliges.</div>'}</div>`).join("");
 $("#hcbody").innerHTML=h}

/* ---------- Filament-Lager ---------- */
function filTile(F){return `<div class="t" onclick="openFil()" style="cursor:pointer"><div class="n">🧵 Filament-Lager</div><div class="s">${F.count?F.count+" Spulen · "+String((F.grams/1000).toFixed(1)).replace(".",",")+" kg":"noch leer"}</div><div class="big" style="font-size:16px;${F.low.length?"color:var(--amb)":""}">${F.low.length?"⚠ "+F.low.length+" knapp":(F.count?"alles gut":"＋ Spulen eintragen")}</div>${F.low.length?`<div class="s">${esc(F.low.slice(0,2).join(", "))}</div>`:""}</div>`}
function openFil(){$("#sv").style.display="block";document.body.style.overflow="hidden";SV.stack=[];SV.flow=null;svSection("filament")}

/* ---------- Zeitraffer ---------- */
async function openTL(){$("#tv").style.display="block";document.body.style.overflow="hidden";const j=await aGet("/api/timelapse");
 const ed=R("printers")>=2;let h=`<p class="hint">${j.videos.length} Videos · belegt ${esc(j.usage)} von höchstens ${esc(j.limit)} – ältere werden automatisch gelöscht.</p>`;
 h+=j.videos.map((v,i)=>{const u="/api/timelapse/file?name="+encodeURIComponent(v.name);return `<div class="vid"><b>${esc(v.name.replace(/\.mp4$/,""))}</b><div class="s">${esc(v.size)}</div>
  <div class="row" style="margin-top:8px"><button class="b acc" onclick="this.parentNode.insertAdjacentHTML('afterend','<video controls playsinline autoplay src=&quot;${u}&quot;></video>');this.remove()">▶ Abspielen</button><a class="b" href="${u}" download="${esc(v.name)}" style="text-decoration:none">⬇ Laden</a>${ed?`<button class="b danger" onclick="delTL('${encodeURIComponent(v.name)}')">Löschen</button>`:""}</div></div>`}).join("")||'<p class="hint">Noch keine Videos. Zeitraffer einschalten: ⚙ → 🎬 Zeitraffer, oder beim Drucker „🎬 Zeitraffer“ antippen.</p>';
 $("#tbody").innerHTML=h}
async function delTL(n){if(!confirm("Video löschen?"))return;const j=await aPost("/api/timelapse/delete",{name:decodeURIComponent(n)});toast(j.msg||"");openTL()}

/* ---------- Unwetterwarnungen ---------- */
let RMK=null;const RML=[["power","⏻","acc"],["home","⌂ Start"],["guide","Programm"],["info","ℹ"],["ch_up","Sender +"],["up","▲"],[""],["vol_up","🔊 +"],["left","◀"],["ok","OK","acc"],["right","▶"],["mute","🔇"],["ch_down","Sender −"],["down","▼"],["back","↩"],["vol_down","🔉 −"],["rew","⏪"],["play","▶ Play"],["pause","❚❚"],["ffw","⏩"],["stop","■ Stopp"],["record","● Aufn."],["source","Eingang"],[""]];
function mkey(key,k,v){act({key:key,action:"key",k:k,value:v||""},null)}
function openRemote(key){RMK=key;renderRemote();$("#rmv").style.display="block";document.body.style.overflow="hidden"}
function renderRemote(){if(!RMK)return;const d=(S.smart||[]).find(x=>x.key==RMK);if(!d||!d.media){closeOv("rmv");return}const k=d.media.keys||[];
 $("#rmt").textContent="📺 "+d.name;
 $("#rmbody").innerHTML=`<div class="s">${d.on?"An":"Aus"}${d.vendor?" · "+esc(d.vendor):""}</div><div class="rmn" style="color:${d.on?"var(--amb)":"var(--mut)"}">${esc(d.media.now||(d.on?"läuft":"aus"))}</div>
 <div class="rm">${RML.map(x=>k.includes(x[0])?`<button class="b ${x[2]||""}" onclick="mkey('${d.key}','${x[0]}')">${x[1]}</button>`:"<span></span>").join("").replace(/(<span><\/span>)+$/,"")}</div>
 <label class="s">Sender direkt wählen</label><div class="chi"><input id="rmch" type="number" inputmode="numeric" placeholder="z. B. 102"><button class="b acc" onclick="const v=$('#rmch').value;if(v)mkey('${d.key}','channel',v)">Umschalten</button></div>
 ${d.media.box=="tv"?'<p class="hint">Reagiert der Fernseher nicht? Im TV-Menü „Virtuelle Fernbedienung“ einschalten.</p>':'<p class="hint">Lautstärke regelt der Fernseher – den kannst du zusätzlich einbinden.</p>'}`}
function openWxa(){const WA=(S.weather&&S.weather.alerts)||[];$("#wxv").style.display="block";document.body.style.overflow="hidden";
 $("#wxbody").innerHTML=(WA.map(a=>`<div class="vid" style="border-left:4px solid ${a.color}"><b>${a.icon} ${esc(a.headline)}</b><div class="s">${esc(a.level_name)}${a.when?" · "+esc(a.when):""}${a.pre?" · Vorabinformation":""}</div>${a.desc?`<p style="margin:8px 0 0;line-height:1.45">${esc(a.desc)}</p>`:""}${a.instr?`<p style="margin:8px 0 0;line-height:1.45;color:var(--amb)">💡 ${esc(a.instr)}</p>`:""}</div>`).join("")||'<p class="hint">Gerade keine Warnungen für deinen Ort. 🌤</p>')+'<p class="hint">Quelle: Deutscher Wetterdienst (amtliche Warnungen für deine Gemeinde).</p>'}

/* ---------- Urlaubsmodus & Waschmaschine ---------- */
function openSec(s){$("#sv").style.display="block";document.body.style.overflow="hidden";SV.stack=[];SV.flow=null;svSection(s)}
function vacTile(V){const ed=R("smart")>=2;return `<div class="t ${V.active?"on":""}" ${ed?`onclick="openSec('vacation')" style="cursor:pointer"`:""}><div class="n">🏖 Urlaubsmodus</div><div class="s">${V.active?esc(V.text):"Licht, Heizung, Kamera"}</div><div class="big" style="font-size:18px;color:${V.active?"var(--amb)":"var(--mut)"}">${V.active?"aktiv":"aus"}</div>${ed&&!V.active?'<div class="s">antippen zum Einschalten ›</div>':""}</div>`}
function applTile(a){const ed=R("smart")>=2,run=a.state=="läuft",done=a.state=="fertig";return `<div class="t ${run?"on":""}" ${ed?`onclick="openSec('appliances')" style="cursor:pointer"`:""}><div class="n">${a.icon} ${esc(a.name)}</div><div class="s">${a.watt!=null?fmt(a.watt)+" W":"Steckdose nicht erreichbar"}</div><div class="big" style="font-size:18px;color:${done?"var(--ok)":run?"var(--amb)":"var(--mut)"}">${done?"✔ fertig":run?"läuft":"bereit"}</div>${a.sub?`<div class="s">${esc(a.sub)}</div>`:""}</div>`}

/* ---------- Kamera-Aufnahmen ---------- */
let RC="";
async function openRec(k){RC=k||"";if(ovKey)closeCam();$("#rcv").style.display="block";document.body.style.overflow="hidden";$("#rcbody").innerHTML='<p class="hint">Lade …</p>';
 const j=await aGet("/api/clips"+(RC?"?cam="+encodeURIComponent(RC):""));if(!j||!j.ok){$("#rcbody").innerHTML='<p class="hint">Konnte die Aufnahmen nicht laden.</p>';return}
 $("#rctitle").textContent="🎥 "+(j.cam?"Aufnahmen · "+j.cam:"Aufnahmen");
 const ed=R("cams")>=2;let h=`<p class="hint">${j.count} Aufnahmen · ${esc(j.usage)}${j.cam?" (alle Kameras: "+esc(j.total)+")":""} – ältere werden automatisch gelöscht (Grenze: ${esc(j.limit)}).</p>`;
 h+=j.videos.map(v=>{const u="/api/clips/file?name="+encodeURIComponent(v.name);
  const when=tShort(v.ts);return `<div class="vid"><b>${esc(j.cam?when:(v.cam||"Kamera")+" · "+when)}</b><div class="s">${esc(v.size)}</div>
  <div class="row" style="margin-top:8px"><button class="b acc" onclick="this.parentNode.insertAdjacentHTML('afterend','<video controls playsinline autoplay src=&quot;${u}&quot;></video>');this.remove()">▶ Abspielen</button><a class="b" href="${u}" download="${esc(v.name)}" style="text-decoration:none">⬇ Laden</a>${ed?`<button class="b danger" onclick="delRec('${encodeURIComponent(v.name)}')">Löschen</button>`:""}</div></div>`}).join("")||'<p class="hint">Noch keine Aufnahmen. Aufnehmen lässt sich am PC im Kamerafenster (● Aufnahme) oder per Ablauf „Kamera-Aufnahme“, z. B. wenn eine Person erkannt wird.</p>';
 $("#rcbody").innerHTML=h}
async function delRec(n){if(!confirm("Aufnahme löschen?"))return;const j=await aPost("/api/clips/delete",{name:decodeURIComponent(n)});toast(j.msg||"");openRec(RC)}

/* ---------- Logbuch ---------- */
const LIC={"Anwesenheit":"🏠","Drucker":"🖨️","3D-Druck":"🖨️","Kamera":"📷","Netzwerk":"📶","Gerät":"🔌","Sprache":"🎙","Handy":"📱","Automation":"🤖","App":"⚙️"};
let LG={kind:"",seen:0},newsT=0;
function logSeen(){try{return +localStorage.getItem("hk_log_seen")||0}catch(e){return 0}}
function tShort(ts){const d=new Date(ts*1000),n=new Date();const t=d.toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"});if(d.toDateString()==n.toDateString())return t;const y=new Date(n-864e5);if(d.toDateString()==y.toDateString())return "gestern "+t;return d.toLocaleDateString("de-DE",{day:"2-digit",month:"2-digit"})+" "+t}
async function checkNews(){if(OFF||Date.now()-newsT<60000)return;newsT=Date.now();try{const j=await aGet("/api/log?limit=60");const seen=logSeen();
 if(!seen){try{localStorage.setItem("hk_log_seen",String(Math.floor(Date.now()/1000)))}catch(e){}return}
 const n=j.entries.filter(e=>e.ts>seen);if(n.length){const k={};n.forEach(e=>k[e.kind]=(k[e.kind]||0)+1);
  $("#news").innerHTML=`📜 <b>${n.length} Neuigkeit${n.length>1?"en":""}</b> seit ${tShort(seen)} · ${Object.entries(k).slice(0,4).map(([a,b])=>(LIC[a]||"•")+" "+b).join(" ")} ›`;$("#news").style.display="block"}else $("#news").style.display="none"}catch(e){}}
function openLog(){LG.seen=logSeen();$("#lv").style.display="block";document.body.style.overflow="hidden";loadLog()}
function closeLog(){$("#lv").style.display="none";document.body.style.overflow="";try{localStorage.setItem("hk_log_seen",String(Math.floor(Date.now()/1000)))}catch(e){}$("#news").style.display="none"}
async function loadLog(){const q=$("#lq").value.trim();const j=await aGet("/api/log?limit=300&kind="+encodeURIComponent(LG.kind)+"&q="+encodeURIComponent(q));
 $("#lchips").innerHTML=[["","Alle"],...j.kinds.map(k=>[k,(LIC[k]||"")+" "+k])].map(([k,n])=>`<span class="${LG.kind==k?"on":""}" onclick="LG.kind='${k}';loadLog()">${esc(n)}</span>`).join("");
 let h="",day="";for(const e of j.entries){const d=new Date(e.ts*1000).toLocaleDateString("de-DE",{weekday:"long",day:"2-digit",month:"2-digit"});if(d!==day){h+=`<div class="day">${d}</div>`;day=d}
  h+=`<div class="le ${LG.seen&&e.ts>LG.seen?"new":""}"><div class="ic">${LIC[e.kind]||"•"}</div><div class="tx"><b>${esc(e.name)}</b><div class="s">${esc(e.text)}</div></div><div class="tm">${new Date(e.ts*1000).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"})}</div></div>`}
 $("#lbody").innerHTML=h||'<p class="hint">Keine Einträge.</p>'}

/* ---------- Ersteinrichtung (Heimserver ohne Bildschirm) ---------- */
function showSetup(){$("#app").style.display="none";$("#login").style.display="none";$("#setup").style.display="block"}
async function doSetup(){const n=$("#su_name").value.trim(),p=$("#su_pin").value.trim(),p2=$("#su_pin2").value.trim();$("#su_err").textContent="";
 if(!n||!/^\d{4,12}$/.test(p)){$("#su_err").textContent="Bitte Name und eine PIN aus 4–12 Ziffern";return}
 if(p!==p2){$("#su_err").textContent="Die PINs sind nicht gleich";return}
 const r=await fetch("/api/setup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:n,pin:p}),credentials:"same-origin"});const j=await r.json();
 if(!j.ok){$("#su_err").textContent=j.msg||"Fehler";return}
 $("#setup").style.display="none";await load();startFlow()}

/* ---------- Einstellungen & Automationen ---------- */
let SV={stack:[],flow:null,fi:0,menu:null};
function canSettings(){return !!(S&&S.me)}
async function openSettings(){$("#sv").style.display="block";document.body.style.overflow="hidden";SV.stack=[];SV.flow=null;await svMenu()}
function svClose(){$("#sv").style.display="none";document.body.style.overflow="";SV.flow=null;load()}
function svBack(){SV.stack.pop();const v=SV.stack.pop();if(!v){svMenu();return}v()}
function svShow(title,html,again){$("#sv_t").textContent=title;$("#sv_b").innerHTML=html;$("#sv").scrollTop=0;if(again)SV.stack.push(again);$("#sv_back").style.visibility=SV.stack.length>1||SV.flow?"visible":"hidden"}
async function aGet(p){const r=await api(p);return r.json()}
async function aPost(p,b){const r=await api(p,b);return r.json()}
async function svMenu(){SV.stack=[];let h="";
 h+=`<div class="li" onclick="svSection('siri')"><div class="tx"><div class="tt">🗣 Siri-Kurzbefehle</div><div class="su">„Hey Siri, Schreibtisch aus“ – einmal einrichten</div></div><div class="ch">›</div></div>`;
 h+=`<div class="li" onclick="svSection('twofa')"><div class="tx"><div class="tt">🔐 Zwei-Faktor-Anmeldung</div><div class="su">${S.me&&S.me.tfa?"✅ an – Code aus der Authenticator-App":"Zusätzlich zur PIN ein Code vom Handy"}</div></div><div class="ch">›</div></div>`;
 h+=`<div class="li" onclick="svSection('bugreport')"><div class="tx"><div class="tt">🐞 Problem melden</div><div class="su">Etwas klappt nicht? Hier melden</div></div><div class="ch">›</div></div>`;
 if(R("automations")>=1){h+=`<div class="li" onclick="autoList()"><div class="tx"><div class="tt">🤖 Automationen</div><div class="su">Wenn … dann … – ansehen${R("automations")>=2?", anlegen und ändern":""}</div></div><div class="ch">›</div></div>`}
 if(S.me&&S.me.admin){SV.menu=SV.menu||await aGet("/api/admin?s=menu");
  h+=`<div class="li" onclick="startFlow()"><div class="tx"><div class="tt">✨ Geführte Einrichtung</div><div class="su">Router, Personen, Drucker, Kameras, Push – Schritt für Schritt</div></div><div class="ch">›</div></div><h2>Bereiche</h2>`;
  h+=SV.menu.sections.map(x=>`<div class="li" onclick="svSection('${x.id}')"><div class="tx"><div class="tt">${esc(x.title)}</div><div class="su">${esc(x.sub)}</div></div><div class="ch">›</div></div>`).join("")}
 svShow("Einstellungen",h,svMenu)}
function startFlow(){$("#sv").style.display="block";document.body.style.overflow="hidden";SV.stack=[];aGet("/api/admin?s=menu").then(m=>{SV.menu=m;SV.flow=m.flow;SV.fi=0;svSection(SV.flow[0])})}
function flowBar(){if(!SV.flow)return"";return`<div class="flow">${SV.flow.map((x,i)=>`<i class="${i<=SV.fi?"on":""}"></i>`).join("")}</div><div class="s">Schritt ${SV.fi+1} von ${SV.flow.length}</div>`}
function flowNext(){if(!SV.flow)return"";const last=SV.fi>=SV.flow.length-1;return`<div class="btns"><button class="b acc" onclick="flowGo()">${last?"Fertig 🎉":"Weiter ›"}</button></div>`}
function flowGo(){SV.fi++;if(SV.fi>=SV.flow.length){SV.flow=null;toast("Fertig eingerichtet 🎉");svClose();return}SV.stack=[];svSection(SV.flow[SV.fi])}

/* Formularfelder: text, password, number, time, choice, multi, bool, days */
function fHtml(fs,v,pre){pre=pre||"f";return fs.map((f,i)=>{const id=pre+"_"+i,val=v[f.key];let x="";
 if(f.kind=="choice")x=`<select id="${id}">${(f.opts||[]).map((o,j)=>`<option value="${j}" ${String(o[0])===String(val)?"selected":""}>${esc(o[1])}</option>`).join("")}</select>`;
 else if(f.kind=="multi")x=`<div class="ml" id="${id}">${(f.opts||[]).map((o,j)=>`<label class="ck"><input type="checkbox" data-j="${j}" ${(val||[]).map(String).includes(String(o[0]))?"checked":""}>${esc(o[1])}</label>`).join("")||'<div class="s">– nichts verfügbar –</div>'}</div>`;
 else if(f.kind=="bool")x=`<label class="ck"><input type="checkbox" id="${id}" ${val?"checked":""}>${esc(f.label)}</label>`;
 else if(f.kind=="longtext")x=`<textarea id="${id}" rows="6">${esc(val||"")}</textarea>`;
 else if(f.kind=="info")x="";
 else if(f.kind=="qr")x=`<div class="qrb">${val||""}</div>`;
 else if(f.kind=="code")x=`<input id="${id}" inputmode="numeric" autocomplete="one-time-code" maxlength="12" placeholder="123456" style="font-size:22px;letter-spacing:.3em;text-align:center">`;
 else if(f.kind=="copy")x=`<div class="row" style="flex-wrap:nowrap"><input id="${id}" readonly value="${esc(val||"")}" style="font-size:13px" onfocus="this.select()"><button class="b acc" type="button" onclick="cpy('${id}')">Kopieren</button></div>`;
 else if(f.kind=="color")x=`<div class="row"><input id="${id}" type="color" value="${esc(val||"#888888")}" style="width:64px;height:40px;padding:0;border:none;background:none" data-set="${val?1:0}" oninput="this.dataset.set=1"><span class="s">${val?"":"noch keine Farbe gewählt"}</span></div>`;
 else if(f.kind=="link")return`<a class="b" style="display:block;text-align:center;padding:12px;margin-top:10px;text-decoration:none" href="${esc(f.hint||"#")}" ${(f.hint||"").startsWith("/api/")?"download":'target="_blank" rel="noopener"'}>${esc(f.label)}</a>`;
 else if(f.kind=="days")x=`<div class="dy" id="${id}">${(S._days||["Mo","Di","Mi","Do","Fr","Sa","So"]).map((d,j)=>`<span data-j="${j}" class="${!val||!val.length||val.includes(j)?"on":""}" onclick="this.classList.toggle('on')">${d}</span>`).join("")}</div>`;
 else x=`<input id="${id}" type="${f.kind=="password"?"password":f.kind=="time"?"time":f.kind=="date"?"date":"text"}" ${f.kind=="number"?'inputmode="decimal"':""} value="${esc(val==null?"":String(f.kind=="number"?String(val).replace(".",","):val))}" autocomplete="off">`;
 return (f.kind=="bool"?"":`<label>${esc(f.label)}</label>`)+x+(f.hint?`<div class="fh">${esc(f.hint)}</div>`:"")}).join("")}
function cpy(id){const e=$("#"+id);e.removeAttribute("readonly");e.focus();e.setSelectionRange(0,99999);let ok=false;try{ok=document.execCommand("copy")}catch(x){}
 e.setAttribute("readonly","");e.blur();if(!ok&&navigator.clipboard){navigator.clipboard.writeText(e.value).then(()=>toast("📋 Kopiert"),()=>toast("Bitte lange drücken → Kopieren"));return}toast(ok?"📋 Kopiert":"Bitte lange drücken → Kopieren")}
function fRead(fs,pre){pre=pre||"f";const o={};fs.forEach((f,i)=>{const e=$("#"+pre+"_"+i);if(!e)return;
 if(f.kind=="choice")o[f.key]=(f.opts||[])[+e.value]?f.opts[+e.value][0]:"";
 else if(f.kind=="multi")o[f.key]=[...e.querySelectorAll("input:checked")].map(c=>f.opts[+c.dataset.j][0]);
 else if(f.kind=="bool")o[f.key]=e.checked;
 else if(f.kind=="days")o[f.key]=[...e.querySelectorAll("span.on")].map(s=>+s.dataset.j);
 else if(f.kind=="number")o[f.key]=e.value.trim().replace(",",".");
 else if(f.kind=="color")o[f.key]=e.dataset.set=="1"?e.value:"";
 else if(f.kind=="copy"||f.kind=="qr")return;
 else o[f.key]=e.value});return o}

/* Admin-Bereiche (Liste → Formular) */
async function svSection(s){const d=await aGet("/api/admin?s="+s);if(!d.ok&&d.msg){toast("⚠ "+d.msg);return}
 if(d.kind=="list"){let h=flowBar()+(d.hint?`<p class="hint">${esc(d.hint)}</p>`:"")+d.items.map(it=>`<div class="li" onclick="svEdit('${s}','${esc(it.id)}')"><div class="tx"><div class="tt">${esc(it.title)}</div><div class="su">${esc(it.sub||"")}</div></div><div class="ch">›</div></div>`).join("");
  if(!d.items.length)h+=`<p class="hint">${esc(d.empty||"Noch nichts eingerichtet.")}</p>`;
  if(d.new)h+=`<div class="btns"><button class="b" onclick="svEdit('${s}','')">${esc(d.new_label||"＋ Hinzufügen")}</button></div>`;
  svShow(d.title||"Einstellungen",h+flowNext(),()=>svSection(s))}
 else svForm(s,"",d,()=>svSection(s))}
async function svEdit(s,id){const d=await aGet("/api/admin?s="+s+"&id="+encodeURIComponent(id));svForm(s,id,d,()=>svEdit(s,id))}
let FORM=null;
function svForm(s,id,d,again){FORM={s,id,d};const listy=["persons","devices","printers","cameras","users","backup","filament","appliances","integrations"].includes(s);
 let h=(listy?"":flowBar())+(d.hint?`<p class="hint">${esc(d.hint)}</p>`:"")+`<div class="fm">${fHtml(d.fields,d.values)}</div><div class="btns">${d.nosave?"":'<button class="b acc" onclick="svSave()">Speichern</button>'}`
  +(d.buttons||[]).map(b=>`<button class="b" onclick="svOp('${b.op}')">${esc(b.label)}</button>`).join("")
  +(listy&&id?`<button class="b danger" onclick="svDel()">Entfernen</button>`:"")+`</div>`+(listy?"":flowNext());
 svShow(d.title||"Einstellungen",h,again)}
async function svSave(){await svOp("save")}
async function svOp(op){const v=fRead(FORM.d.fields);const j=await aPost("/api/admin",{s:FORM.s,op,id:FORM.id||null,values:v});
 toast((j.ok?"":"⚠ ")+(j.msg||"OK"));
 if(FORM.s=="twofa"&&j.ok){if(S&&S.me)S.me.tfa=op!="off";SV.stack.pop();await svSection("twofa");
  if(j.preview){const pv=document.createElement("div");pv.id="svpv";pv.className="pv";pv.style.cssText="font-size:15px;line-height:1.6;color:var(--fg);font-family:ui-monospace,Menlo,monospace;border:2px solid var(--acc);margin:0 0 14px";pv.textContent=j.preview;$("#sv_b").prepend(pv);$("#sv").scrollTop=0}return}
 if(j.preview!=null){let pv=$("#svpv");if(!pv){pv=document.createElement("div");pv.id="svpv";pv.className="pv";$("#sv_b").appendChild(pv)}pv.textContent=j.preview;pv.scrollIntoView({behavior:"smooth"});return}
 if(FORM.s=="bugreport"&&op=="send"&&j.ok){if(j.open){let pv=$("#svpv");if(!pv){pv=document.createElement("div");pv.id="svpv";pv.className="pv";$("#sv_b").appendChild(pv)}pv.innerHTML=`<a class="b acc" style="display:inline-block;text-decoration:none" target="_blank" rel="noopener" href="${esc(j.open)}">🌐 Meldung auf GitHub öffnen</a>`;pv.scrollIntoView({behavior:"smooth"});return}svClose();return}
 if(j.set){Object.assign(FORM.d.values,v,j.set);svForm(FORM.s,FORM.id,FORM.d);SV.stack.pop();return}
 if(j.ok&&(op=="save"||op=="now")){const listy=["persons","devices","printers","cameras","users","backup","filament","appliances","integrations"].includes(FORM.s);if(op=="now"){setTimeout(()=>{if(SV.stack.length)svSection(FORM.s)},4000)}if(listy){SV.stack.pop();SV.stack.pop();svSection(FORM.s)}else if(SV.flow)flowGo()}}
async function svDel(){if(!confirm("Wirklich entfernen?"))return;const j=await aPost("/api/admin",{s:FORM.s,op:"delete",id:FORM.id});toast((j.ok?"":"⚠ ")+(j.msg||""));if(j.ok){SV.stack.pop();SV.stack.pop();svSection(FORM.s)}}

/* Automationen */
let AU=null;
async function autoList(){AU=await aGet("/api/auto");S._days=AU.schema.days;const ed=R("automations")>=2;
 let h=`<p class="hint">Hauskern legt nichts von selbst an – hier baust du dir deine eigenen Regeln.</p>`;
 h+=AU.rules.map(r=>`<div class="li" onclick="${ed?`autoEdit('${r.id}')`:""}" style="align-items:flex-start"><div class="tx"><div class="tt">${esc(r.name)}</div>
  <div class="su">${esc(r.manual?"▶ Szene (von Hand)":"⚡ "+r.trigger)}${r.condition?" · "+esc(r.condition):""}</div><div class="su">→ ${esc(r.actions.join(" · "))}</div></div>
  ${r.manual&&R("smart")>=2?`<button class="b" onclick="event.stopPropagation();autoRun('${r.id}')">▶</button>`:""}
  ${ed&&!r.manual?`<button class="sw ${r.enabled?"on":""}" onclick="event.stopPropagation();autoToggle('${r.id}',${!r.enabled})"></button>`:(!r.manual?`<span class="s">${r.enabled?"an":"aus"}</span>`:"")}</div>`).join("")||`<p class="hint">Noch keine Automationen.</p>`;
 if(ed)h+=`<div class="btns"><button class="b acc" onclick="autoEdit('')">＋ Neue Automation</button></div>`;
 svShow("Automationen",h,autoList)}
async function autoToggle(id,on){const j=await aPost("/api/auto",{op:"toggle",id,enabled:on});toast(j.msg||"");SV.stack.pop();autoList()}
async function autoRun(id){const j=await aPost("/api/auto",{op:"run",id});toast((j.ok?"":"⚠ ")+(j.msg||""))}
let ED=null;
function autoEdit(id){const r=id?AU.rules.find(x=>x.id==id).rule:{id:"",name:"",enabled:true,trigger:{type:"presence",...AU.schema.triggers.presence.defaults},condition:{},actions:[]};
 ED=JSON.parse(JSON.stringify(r));renderEd();SV.stack.push(()=>autoEdit(id))}
function edSync(){if(!ED||!$("#ed_name"))return;ED.name=$("#ed_name").value;ED.enabled=$("#ed_en").checked;const tt=ED.trigger.type;
 Object.assign(ED.trigger,fRead(AU.schema.triggers[tt].fields,"et"));ED.trigger.type=tt;
 ED.condition={from:$("#ec_tw").checked?$("#ec_from").value:"",to:$("#ec_tw").checked?$("#ec_to").value:"",days:[...document.querySelectorAll("#ec_days span.on")].map(s=>+s.dataset.j),presence:AU.schema.presence[+$("#ec_pres").value][0],device:(AU.schema.devices||[["",""]])[+(($("#ec_dev")||{}).value||0)][0]};
 ED.actions=ED.actions.map((a,i)=>Object.assign({type:a.type},fRead(AU.schema.actions[a.type].fields,"ea"+i)))}
function renderEd(){const sc=AU.schema,t=ED.trigger,c=ED.condition||{};const pres=c.presence&&c.presence.who?c.presence.who+":"+c.presence.state:(typeof c.presence=="string"?c.presence:"");const dvk=c.device&&c.device.ain?c.device.ain+"|"+c.device.state:(typeof c.device=="string"?c.device:"");
 let h=`<div class="fm"><label>Name</label><input id="ed_name" value="${esc(ED.name||"")}" placeholder="z. B. Alle weg → Licht aus">
 <label class="ck" style="margin-top:10px"><input type="checkbox" id="ed_en" ${ED.enabled!==false?"checked":""}>Automation ist aktiv</label>
 <h2 style="margin-top:18px">Wenn …</h2><select id="ed_tt" onchange="edSync();ED.trigger={type:this.value,...AU.schema.triggers[this.value].defaults};renderEd()">${Object.entries(sc.triggers).map(([k,v])=>`<option value="${k}" ${k==t.type?"selected":""}>${esc(v.label)}</option>`).join("")}</select>
 ${fHtml(sc.triggers[t.type].fields,t,"et")}
 <h2 style="margin-top:18px">Nur wenn … <span class="s" style="font-weight:400">(optional)</span></h2>
 <label class="ck"><input type="checkbox" id="ec_tw" ${c.from&&c.to?"checked":""} onchange="document.getElementById('ec_twb').style.display=this.checked?'flex':'none'">Nur in einem Zeitfenster</label>
 <div id="ec_twb" style="display:${c.from&&c.to?"flex":"none"};gap:8px"><input type="time" id="ec_from" value="${esc(c.from||"22:00")}"><input type="time" id="ec_to" value="${esc(c.to||"06:00")}"></div>
 <label>An diesen Tagen</label><div class="dy" id="ec_days">${sc.days.map((d,j)=>`<span data-j="${j}" class="${!c.days||!c.days.length||c.days.includes(j)?"on":""}" onclick="this.classList.toggle('on')">${d}</span>`).join("")}</div>
 <label>Anwesenheit</label><select id="ec_pres">${sc.presence.map((o,j)=>`<option value="${j}" ${o[0]===pres?"selected":""}>${esc(o[1])}</option>`).join("")}</select>
 ${(sc.devices||[]).length>1?`<label>Gerät</label><select id="ec_dev">${sc.devices.map((o,j)=>`<option value="${j}" ${o[0]===dvk?"selected":""}>${esc(o[1])}</option>`).join("")}</select>`:""}
 <h2 style="margin-top:18px">… dann</h2>`;
 h+=ED.actions.map((a,i)=>`<div class="ac"><div class="hd"><span>${i+1}. ${esc(sc.actions[a.type].label)}</span><button class="b" onclick="edMove(${i},-1)">↑</button><button class="b" onclick="edMove(${i},1)">↓</button><button class="b danger" onclick="edDel(${i})">✕</button></div>${fHtml(sc.actions[a.type].fields,a,"ea"+i)}</div>`).join("");
 h+=`<select id="ed_add" onchange="if(this.value){edSync();ED.actions.push({type:this.value,...AU.schema.actions[this.value].defaults});renderEd()}"><option value="">＋ Aktion hinzufügen …</option>${Object.entries(sc.actions).map(([k,v])=>`<option value="${k}">${esc(v.label)}</option>`).join("")}</select></div>
 <div class="btns"><button class="b acc" onclick="edSave()">Speichern</button>${ED.id?`<button class="b danger" onclick="edDelete()">Löschen</button>`:""}</div>`;
 svShow(ED.id?"Automation bearbeiten":"Neue Automation",h)}
function edMove(i,d){edSync();const j=i+d;if(j<0||j>=ED.actions.length)return;[ED.actions[i],ED.actions[j]]=[ED.actions[j],ED.actions[i]];renderEd()}
function edDel(i){edSync();ED.actions.splice(i,1);renderEd()}
async function edSave(){edSync();const j=await aPost("/api/auto",{op:"save",rule:ED});toast((j.ok?"":"⚠ ")+(j.msg||""));if(j.ok){SV.stack.pop();SV.stack.pop();autoList()}}
async function edDelete(){if(!confirm("Automation „"+(ED.name||"")+"“ löschen?"))return;const j=await aPost("/api/auto",{op:"delete",id:ED.id});toast(j.msg||"");if(j.ok){SV.stack.pop();SV.stack.pop();autoList()}}
async function openCalc(){$("#cv").style.display="block";document.body.style.overflow="hidden";const r=await api("/api/calc");C=await r.json();if(!cJob)newCalc();else renderCalc()}
function closeCalc(){$("#cv").style.display="none";document.body.style.overflow=""}
function newCalc(){cJob={name:"",qty:1,hours:0,items:[{filament:C&&C.filaments[0]?C.filaments[0].id:"",grams:""}],labor_min:"",extras:"",margin:""};renderCalc()}
function hm(h){h=+h||0;let a=Math.floor(h),m=Math.round((h-a)*60);if(m==60){a++;m=0}return a+":"+String(m).padStart(2,"0")}
function ph(t){t=String(t||"").replace(",",".");if(t.includes(":")){const[a,b]=t.split(":");return(+a||0)+(+b||0)/60}return +t||0}
function renderCalc(){const j=cJob,F=C.filaments;while(j.items.length<4)j.items.push({filament:"",grams:""});
 const opt=sel=>`<option value="">– kein –</option>`+F.map(f=>`<option value="${esc(f.id)}" ${f.id===sel?"selected":""}>${esc(f.name)}</option>`).join("");
 let h=`<div class="fm"><label>Bezeichnung</label><input id="c_name" value="${esc(j.name)}" placeholder="z. B. Handyhalter">
 <div class="two"><div><label>Stückzahl</label><input id="c_qty" inputmode="numeric" value="${esc(j.qty)}"></div><div><label>Druckzeit (Std:Min)</label><input id="c_time" value="${hm(j.hours)}"></div></div>
 <label>Filament · Gramm</label>${j.items.slice(0,4).map((it,i)=>`<div class="fr"><select id="c_f${i}">${opt(it.filament)}</select><input id="c_g${i}" inputmode="decimal" placeholder="g" value="${it.grams===""?"":String(it.grams).replace(".",",")}"></div>`).join("")}
 <div class="two"><div><label>Arbeitszeit (Min.)</label><input id="c_lab" inputmode="numeric" value="${esc(j.labor_min)}" placeholder="${C.defaults.labor_min||0}"></div><div><label>Sonstiges (€)</label><input id="c_ext" inputmode="decimal" value="${esc(j.extras)}" placeholder="0"></div></div>
 <label>Gewinn (%)</label><input id="c_mar" inputmode="decimal" value="${esc(j.margin)}" placeholder="${C.defaults.margin_pct}"></div><div id="cres" class="res"><div class="s">rechne …</div></div>
 <div class="row"><button class="b acc" onclick="saveCalc()">Kalkulation speichern</button><button class="b" onclick="copyCalc()">Text kopieren</button></div>`;
 if(C.saved.length)h+=`<h2>Gespeichert</h2><div class="t wide out" style="padding:4px 12px">${C.saved.map(c=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--card2)"><span onclick="loadCalc(${c.id})" style="flex:1">${esc(c.name)}<br><span class="s" style="margin:0">${esc(c.date)}</span></span><b style="margin-right:10px">${eur(c.price)}</b><button class="b" style="padding:6px 10px" onclick="delCalc(${c.id})">✕</button></div>`).join("")}</div>`;
 $("#cbody").innerHTML=h;$("#cbody").querySelectorAll("input,select").forEach(e=>e.addEventListener("input",()=>{readCalc();clearTimeout(cTimer);cTimer=setTimeout(calcNow,300)}));calcNow()}
function readCalc(){const j=cJob;j.name=$("#c_name").value;j.qty=$("#c_qty").value||1;j.hours=ph($("#c_time").value);j.labor_min=$("#c_lab").value;j.extras=$("#c_ext").value.replace(",",".");j.margin=$("#c_mar").value.replace(",",".");
 j.items=[0,1,2,3].map(i=>({filament:$("#c_f"+i).value,grams:($("#c_g"+i).value||"").replace(",",".")}))}
let cText="";
async function calcNow(){const j=Object.assign({},cJob,{items:cJob.items.filter(i=>i.filament&&+i.grams>0).map(i=>({filament:i.filament,grams:+i.grams}))});
 const r=await api("/api/calc",{job:j});const d=await r.json();if(!d.result)return;const x=d.result;cText=d.text;
 const L=(a,b,c)=>`<div class="ln ${c||""}"><span>${a}</span><span>${eur(b)}</span></div>`;
 let h=`<div class="s" style="margin:0">Verkaufspreis ${x.small_business?"(ohne MwSt., Kleinunternehmer)":"inkl. "+String(x.vat_pct).replace(".",",")+" % MwSt."}</div><div class="pr">${eur(x.price)}</div>${x.qty>1?`<div class="s" style="margin:0">${eur(x.price_each)} je Stück</div>`:""}<div style="height:10px"></div>`;
 x.material_lines.forEach(m=>h+=L("Material "+esc(m.label),m.eur));if(!x.material_lines.length)h+=`<div class="s">Noch kein Filament eingetragen</div>`;
 h+=L(`Strom (${String(Math.round(x.kwh*100)/100).replace(".",",")} kWh ${x.kwh_measured?"gemessen":"geschätzt"})`,x.power)+L("Abschreibung Drucker",x.depreciation)+L("Verschleiß",x.wear)+L("Fehldruck-Risiko",x.risk);
 if(x.labor)h+=L(`Arbeitszeit (${Math.round(x.labor_min)} Min.)`,x.labor);if(x.extras)h+=L("Verpackung / Sonstiges",x.extras);
 h+=`<div style="height:6px"></div>`+L("Selbstkosten"+(x.qty>1?` (${eur(x.cost_each)}/Stk.)`:""),x.cost,"b")+L(`+ Gewinn (Ziel ${String(x.margin_pct).replace(".",",")} %)`,x.profit)+L("= Netto",x.net,"b");
 if(!x.small_business)h+=L(`+ MwSt. ${String(x.vat_pct).replace(".",",")} %`,x.vat);if(x.fee)h+=L(`+ Plattformgebühr`,x.fee);
 h+=L("Verkaufspreis",x.price,"b")+L("Bleibt dir übrig",x.earn,"g");$("#cres").innerHTML=h}
async function saveCalc(){readCalc();if(!cJob.name){const n=prompt("Bezeichnung der Kalkulation:");if(!n)return;cJob.name=n}
 const j=Object.assign({},cJob,{items:cJob.items.filter(i=>i.filament&&+i.grams>0).map(i=>({filament:i.filament,grams:+i.grams}))});
 await api("/api/calc/save",{job:j});toast("Gespeichert ✓");const r=await api("/api/calc");C=await r.json();renderCalc()}
function copyCalc(){if(navigator.clipboard)navigator.clipboard.writeText(cText).then(()=>toast("Kopiert ✓"),()=>toast("Kopieren nicht möglich"));else toast("Kopieren nicht möglich")}
function loadCalc(id){const c=C.saved.find(x=>x.id===id);if(c){cJob=JSON.parse(JSON.stringify(c.job));cJob.items=(cJob.items||[]).map(i=>({filament:i.filament,grams:i.grams}));renderCalc();window.scrollTo(0,0);$("#cv").scrollTo(0,0)}}
async function delCalc(id){if(!confirm("Kalkulation löschen?"))return;await api("/api/calc/delete",{id});const r=await api("/api/calc");C=await r.json();renderCalc()}
function pickPrint(){if(!C.prints.length){toast("Noch keine Drucke aufgezeichnet");return}
 const h=`<h2>Aus fertigem Druck</h2><div class="t wide out" style="padding:4px 12px">${C.prints.map((p,i)=>`<div onclick="usePrint(${i})" style="padding:10px 0;border-bottom:1px solid var(--card2)">${esc(p.label)}</div>`).join("")}</div><div class="row"><button class="b" onclick="renderCalc()">Zurück</button></div>`;$("#cbody").innerHTML=h}
function usePrint(i){const j=JSON.parse(JSON.stringify(C.prints[i].job));const F=C.filaments;
 j.items=(j.items||[]).map(it=>{let f=it.filament||"";if(!f){const m=F.find(x=>x.type&&x.type.toUpperCase()===(it.type||"").toUpperCase())||F[0];f=m?m.id:""}return{filament:f,grams:it.grams}});cJob=j;renderCalc()}
async function up3mf(inp){const f=inp.files[0];if(!f)return;toast("Lese "+f.name+" …");
 try{const buf=await f.arrayBuffer();const r=await fetch("/api/calc/3mf?name="+encodeURIComponent(f.name),{method:"POST",body:buf,headers:{"Content-Type":"application/octet-stream"},credentials:"same-origin"});
  const d=await r.json();if(!r.ok){toast("⚠ "+(d.error||"Fehler"));return}
  cJob={name:d.name,qty:1,hours:d.hours,items:d.items.map(i=>({filament:i.filament,grams:i.grams})),labor_min:"",extras:"",margin:""};renderCalc();
  if(d.plates>1)toast(d.plates+" Druckplatten – alle eingerechnet")}catch(e){toast("⚠ Datei nicht lesbar")}inp.value=""}
function healthTile(){const h=S.health;if(!h||R("system")<1)return"";const bad=h.problems.length;
 const bk=h.backup_last?("💾 Sicherung "+(h.backup_ok?"":"⚠ ")+tShort(h.backup_last)):"💾 noch keine Sicherung";
 return `<div class="t wide"><div class="n">${h.pi?"🍓 "+esc(h.model||"Raspberry Pi"):"🖥 "+esc(h.model||"Heimserver")}</div><div class="s">${esc(h.storage)} · ${String(h.free_gb).replace(".",",")} GB frei${h.disk?" · SSD "+esc(h.disk):""}${h.temp!=null?" · "+String(h.temp).replace(".",",")+" °C":""} · läuft seit ${esc(h.uptime)}</div>
 ${bad?h.problems.map(p=>`<div class="s" style="color:var(--${p.level=="bad"?"bad":"amb"});margin-top:4px">${esc(p.title)}</div>`).join(""):`<div class="big" style="font-size:16px;color:var(--ok)">alles in Ordnung</div>`}<div class="s" style="margin-top:4px">${bk}</div></div>`}
function renderDiag(){checkNews();if(R("system")<1&&R("history")<1&&!S.me){$("#diag").innerHTML="";return}const d=S.diag||{};const c=d.summary;let t=`<div class="t wide"><div class="n">🩺 Diagnose${d.probe?" (Probelauf)":""}</div>`;
 t+=d.running?`<div class="s">läuft … ${esc(d.progress||"")}</div>`:(c?`<div class="s">zuletzt ${esc(d.time_text)}</div><div class="big" style="font-size:18px">${c.ok} ✅ · ${c.warn} ⚠️ · ${c.bad} ❌</div>`:`<div class="s">Prüft FRITZ!Box, Drucker, Kameras, Kalender, Push …</div>`);
 t+=`<div class="row"><button class="b acc" onclick="act({key:'sys:diag',action:'run'})" ${d.running?"disabled":""}>Jetzt prüfen</button>${c&&!d.running?`<button class="b" onclick="showDiag()">Details</button>`:""}</div><div id="dgl"></div></div>`;
 if(R("system")<1)t="";t=healthTile()+t;
 t=`<div class="t wide" onclick="openLog()" style="cursor:pointer"><div class="n">📜 Logbuch</div><div class="s">Was passiert ist – wer kam und ging, Drucke, Internet, Schaltungen</div><div class="big" style="font-size:16px">Logbuch öffnen ›</div></div>`+t;
 if(R("history"))t=`<div class="t wide" onclick="openHist()"><div class="n">📈 Verlauf</div><div class="s">Internet, Heizung, Steckdosen und Ping als Diagramm</div><div class="big" style="font-size:16px">Diagramme ansehen ›</div></div>`+t;
 if(!$("#diag").dataset.open)$("#diag").innerHTML=sec("System",t)}
async function showDiag(){const r=await api("/api/diag");const d=await r.json();const ic={ok:"✅",warn:"⚠️",bad:"❌",info:"ℹ️"};let h="",g=null;
 for(const x of d.results){if(x.group!==g){h+=`<div class="day">${esc(x.group)}</div>`;g=x.group}
  h+=`<div class="dg"><span>${ic[x.status]}</span><div><b>${esc(x.name)}</b> <span class="s">${esc(x.detail)}</span>${x.hint&&(x.status=="warn"||x.status=="bad")?`<div class="h w">→ ${esc(x.hint)}</div>`:""}</div></div>`}
 $("#diag").dataset.open="1";$("#dgl").innerHTML=h+`<div class="row"><button class="b" onclick="delete $('#diag').dataset.open;renderDiag()">Zuklappen</button></div>`}
function setT(k,cur,d){const v=Math.max(8,Math.min(28,(typeof cur==="number"?cur:20)+d));act({key:k,action:"temp",value:v},"Soll "+fmt(v)+" °C")}
function refreshCams(){document.querySelectorAll("img[data-cam]").forEach(i=>{const u="/api/cam?key="+encodeURIComponent(i.dataset.cam)+"&t="+Date.now();const n=new Image();n.onload=()=>{if(n.naturalWidth){i.src=n.src;camSrc[i.dataset.cam]=n.src}};n.src=u})}
function openCam(k){const c=S.cams.find(x=>x.key===k);ovKey=k;$("#ovname").textContent=c.name;$("#ov").style.display="flex";
 let p="";if(c.ptz){p=`<div class="pad">${[["↖",-1,1],["↑",0,1],["↗",1,1],["←",-1,0],["⌂",0,0],["→",1,0],["↙",-1,-1],["↓",0,-1],["↘",1,-1]].map(a=>a[1]||a[2]?`<button class="b" onclick="act({key:'${k}',action:'ptz',x:${a[1]},y:${a[2]}},' ')">${a[0]}</button>`:`<button class="b" disabled style="opacity:.4">•</button>`).join("")}</div>`;
  p+=`<div class="row" style="justify-content:center"><button class="b" onclick="act({key:'${k}',action:'zoom',z:-1},' ')">Zoom −</button><button class="b" onclick="act({key:'${k}',action:'zoom',z:1},' ')">Zoom +</button></div>`;
  if(c.presets&&c.presets.length)p+=`<div class="row" style="justify-content:center">${c.presets.map(x=>`<button class="b" onclick="act({key:'${k}',action:'preset',value:'${esc(x.token)}'},'→ ${esc(x.name)}')">${esc(x.name)}</button>`).join("")}</div>`}
 else p='<div class="s" style="text-align:center">Diese Kamera kann nicht geschwenkt werden</div>';
 $("#ptz").innerHTML=p;const img=$("#ovimg");const tick=()=>{const n=new Image();n.onload=()=>{if(n.naturalWidth)img.src=n.src};n.src="/api/cam?key="+encodeURIComponent(k)+"&t="+Date.now()};tick();clearInterval(camTimer);camTimer=setInterval(tick,1000)}
function closeCam(){$("#ov").style.display="none";clearInterval(camTimer);ovKey=null}
load();setInterval(load,3000);setInterval(()=>{if(!ovKey)refreshCams()},2500);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)load()});
</script></body></html>
"""
