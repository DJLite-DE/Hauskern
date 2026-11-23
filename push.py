# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
push.py – Push-Nachrichten aufs Handy über ntfy, Pushover oder Telegram (nur Standardbibliothek).

Hinweise, die Hauskern sowieso erzeugt (Druck fertig, Internet weg …), gehen zusätzlich aufs Handy –
auch wenn Hauskern auf dem Raspberry Pi ohne Bildschirm läuft.
"""
import json
import re
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVICES = {"ntfy": "ntfy (kostenlos)", "pushover": "Pushover", "telegram": "Telegram"}


class PushError(Exception):
    pass


def _multipart(fields, files):
    """files: [(feldname, dateiname, bytes, mime)]"""
    boundary = "----hauskern" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for name, fname, data, mime in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _open(req, timeout=20):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode(errors="ignore")
        raise PushError(f"HTTP {e.code}: {body or e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise PushError(f"nicht erreichbar ({getattr(e, 'reason', e)})")


def _img_mime(data):
    return ("image/png", "png") if data[:4] == b"\x89PNG" else ("image/jpeg", "jpg")


# --------------------------------------------------------------------------- #
# Dienste
# --------------------------------------------------------------------------- #
def send_ntfy(cfg, title, text, prio, click, image):
    server = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
    topic = (cfg.get("topic") or "").strip()
    if not topic:
        raise PushError("Kein Thema (Topic) eingetragen")
    headers = {}
    if cfg.get("token"):
        headers["Authorization"] = "Bearer " + cfg["token"]
    if image:
        # Bild hochladen: Optionen als URL-Parameter (UTF-8 sicher), Bild als Inhalt
        mime, ext = _img_mime(image)
        params = {"title": title, "message": text or title, "priority": str(prio), "tags": "house",
                  "filename": f"hauskern.{ext}"}
        if click:
            params["click"] = click
        url = f"{server}/{urllib.parse.quote(topic)}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=image, headers=headers, method="PUT")
    else:
        body = {"topic": topic, "title": title, "message": text or title, "priority": prio, "tags": ["house"]}
        if click:
            body["click"] = click
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(server + "/", data=json.dumps(body).encode("utf-8"), headers=headers,
                                     method="POST")
    _open(req)


def send_pushover(cfg, title, text, prio, click, image):
    token, user = (cfg.get("token") or "").strip(), (cfg.get("user") or "").strip()
    if not (token and user):
        raise PushError("API-Token und User-Key eintragen")
    fields = {"token": token, "user": user, "title": title, "message": text or title,
              "priority": str({1: -1, 3: 0, 4: 1, 5: 1}.get(prio, 0))}
    if click:
        fields.update(url=click, url_title="Hauskern öffnen")
    if image and len(image) < 2_500_000:
        mime, ext = _img_mime(image)
        body, ctype = _multipart(fields, [("attachment", f"hauskern.{ext}", image, mime)])
    else:
        body, ctype = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=body,
                                 headers={"Content-Type": ctype})
    _open(req)


def send_telegram(cfg, title, text, prio, click, image):
    token, chat = _clean_token(cfg.get("token")), str(cfg.get("chat_id") or "").strip()
    if not (token and chat):
        raise PushError("Bot-Token und Chat-ID eintragen")
    msg = f"<b>{_esc(title)}</b>" + (f"\n{_esc(text)}" if text else "") + (f"\n<a href=\"{click}\">Hauskern öffnen</a>"
                                                                           if click else "")
    base = f"https://api.telegram.org/bot{token}"
    if image:
        mime, ext = _img_mime(image)
        body, ctype = _multipart({"chat_id": chat, "caption": msg[:1000], "parse_mode": "HTML",
                                  "disable_notification": "true" if prio <= 1 else "false"},
                                 [("photo", f"hauskern.{ext}", image, mime)])
        req = urllib.request.Request(base + "/sendPhoto", data=body, headers={"Content-Type": ctype})
    else:
        data = json.dumps({"chat_id": chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True,
                           "disable_notification": prio <= 1}).encode()
        req = urllib.request.Request(base + "/sendMessage", data=data, headers={"Content-Type": "application/json"})
    _open(req)


def _clean_token(token):
    """Token aus dem eingefügten Text holen (BotFather-Nachricht, Leerzeichen, Zeilenumbrüche …)."""
    t = re.sub(r"\s+", "", str(token or ""))
    m = re.search(r"(\d{6,12}:[A-Za-z0-9_-]{30,})", t)
    return m.group(1) if m else t


def telegram_find_chat(token):
    """Liefert (Chat-ID, Name) der letzten Nachricht an den Bot (vorher dem Bot „Hallo“ schreiben)."""
    tok = _clean_token(token)
    if not tok:
        raise PushError("Bitte zuerst den Bot-Token eintragen")
    base = f"https://api.telegram.org/bot{tok}"
    try:
        me = json.loads(_open(urllib.request.Request(base + "/getMe"))[1]).get("result") or {}
    except PushError as e:
        if "401" in str(e) or "404" in str(e):
            raise PushError("Der Token stimmt nicht – bitte nochmal komplett aus der BotFather-Nachricht kopieren") from e
        raise
    bot = "@" + (me.get("username") or "?")
    try:
        raw = _open(urllib.request.Request(base + "/getUpdates?limit=100&timeout=0"))[1]
    except PushError as e:
        if "409" not in str(e):
            raise
        # Bot hängt an einem „Webhook“ (z. B. von einem anderen Dienst) → abschalten, dann geht getUpdates
        _open(urllib.request.Request(base + "/deleteWebhook"))
        raw = _open(urllib.request.Request(base + "/getUpdates?limit=100&timeout=0"))[1]
    res = json.loads(raw).get("result", [])
    for upd in reversed(res):
        msg = (upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
               or (upd.get("callback_query") or {}).get("message") or upd.get("my_chat_member") or {})
        chat = msg.get("chat")
        if chat:
            return str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
    raise PushError(f"Bei deinem Bot {bot} ist noch keine Nachricht angekommen. In Telegram genau diesen Bot "
                    f"öffnen (Suche: {bot}), auf „Starten“ tippen, „Hallo“ schreiben – dann nochmal versuchen.")


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


SENDERS = {"ntfy": send_ntfy, "pushover": send_pushover, "telegram": send_telegram}


# --------------------------------------------------------------------------- #
# Warteschlange (nie die App blockieren)
# --------------------------------------------------------------------------- #
def in_quiet_hours(cfg, now=None):
    q = cfg.get("quiet") or {}
    if not q.get("enabled"):
        return False
    try:
        a = [int(x) for x in q.get("from", "22:00").split(":")]
        b = [int(x) for x in q.get("to", "07:00").split(":")]
    except ValueError:
        return False
    lt = time.localtime(now or time.time())
    cur, start, end = lt.tm_hour * 60 + lt.tm_min, a[0] * 60 + a[1], b[0] * 60 + b[1]
    return start <= cur < end if start < end else (cur >= start or cur < end)


class Pusher:
    """get_cfg() -> dict der Push-Einstellungen; on_result(ok, text) für Logbuch/Protokoll."""

    def __init__(self, get_cfg, on_result=lambda ok, msg: None):
        self.get_cfg, self.on_result = get_cfg, on_result
        self.q = queue.Queue()
        self._recent = {}
        threading.Thread(target=self._run, name="Push", daemon=True).start()

    def send(self, title, text="", important=False, click="", image_fn=None, force=False):
        cfg = self.get_cfg()
        if not force:
            if not cfg.get("enabled"):
                return False
            if in_quiet_hours(cfg) and not important:
                return False
            key = title + "|" + text
            if time.time() - self._recent.get(key, 0) < 60:  # nicht doppelt innerhalb einer Minute
                return False
            self._recent[key] = time.time()
        self.q.put((dict(cfg), title, text, 4 if important else 3, click, image_fn))
        return True

    def send_now(self, cfg, title, text, important=False, click="", image=None):
        """Direkt senden (z. B. Test-Knopf) – wirft PushError."""
        SENDERS[cfg.get("service", "ntfy")](cfg.get(cfg.get("service", "ntfy"), {}), title, text,
                                             4 if important else 3, click, image)

    def _run(self):
        while True:
            cfg, title, text, prio, click, image_fn = self.q.get()
            image = None
            if image_fn:
                try:
                    image = image_fn()
                except Exception:
                    image = None
            svc = cfg.get("service", "ntfy")
            for attempt in range(2):
                try:
                    SENDERS[svc](cfg.get(svc, {}), title, text, prio, click, image)
                    self.on_result(True, title)
                    break
                except PushError as e:
                    if attempt == 1 or str(e).startswith("HTTP 4"):
                        self.on_result(False, f"{SERVICES.get(svc, svc)}: {e}")
                        break
                    time.sleep(5)
                except Exception as e:  # pragma: no cover
                    self.on_result(False, f"{type(e).__name__}: {e}")
                    break
