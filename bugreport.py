# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
bugreport.py – „🐞 Problem melden“.

* Familien-Version: schickt die Meldung direkt an Pascal (Telegram über meldeziel.json, sonst ntfy).
* Consumer-Version: nichts wird von selbst verschickt – Hauskern öffnet eine vorausgefüllte Fehlermeldung
  (GitHub-Issue) im Browser. Weil die öffentlich ist, werden IP-/MAC-Adressen und Namen vorher entfernt.

Was verschickt wird, sieht man vorher (preview). Passwörter, Zugangscodes und Schlüssel werden geschwärzt.
Zusätzlich bleibt eine Kopie (ZIP mit Protokoll) auf dem eigenen Rechner.
"""
import io
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import applog
import edition

REPORT_SERVER = "https://ntfy.sh"
try:   # nur in der Familien-Version vorhanden
    from privat import REPORT_TOPIC
except ImportError:
    REPORT_TOPIC = ""
PUBLIC = not edition.is_family()
MAX_URL_BODY = 3000      # GitHub-Link mit vorausgefülltem Text darf nicht zu lang werden
MAX_BODY = 3800          # ntfy: Nachrichten bis 4096 Zeichen
MAX_ATTACH = 8_000_000   # ntfy.sh: Anhänge bis 15 MB
AREAS = [("allgemein", "Allgemein / weiß nicht"), ("setup", "Installation & Einrichtung"), ("phone", "Handy"),
         ("router", "Router / FRITZ!Box / WLAN"), ("smart", "Smart Home (Steckdosen, Heizung, Lampen)"),
         ("vendors", "Weitere Hersteller (Shelly, Hue, Sky, Fernseher …)"), ("cams", "Kameras"),
         ("printer", "3D-Drucker"), ("auto", "Automationen"), ("siri", "Siri / Sprache"),
         ("update", "Update"), ("server", "Heimserver"), ("other", "Sonstiges")]
_last = {"t": 0.0}


class ReportError(Exception):
    pass


def system_text():
    return f"{platform.system()} {platform.release()} · Python {platform.python_version()}"


def log_tail(n=25):
    """Letzte Warnungen/Fehler aus dem Protokoll (schon geschwärzt)."""
    p = applog.path()
    if not p or not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-2000:]
    except OSError:
        return []
    keep = [ln.rstrip() for ln in lines if re.search(r"\b(WARNING|ERROR|CRITICAL|Traceback)\b", ln)]
    return [applog.redact(ln)[:240] for ln in keep[-n:]]


_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_QUOTED = re.compile(r"\(['\"][^'\"]*['\"]\)|['\"][^'\"]{1,40}['\"]")
_PATH = re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/)[^\s,;)]*")


def public_safe(line):
    """Für öffentliche Meldungen: Adressen, Mails, Pfade und Namen in Anführungszeichen entfernen."""
    line = applog.redact(line)
    line = _MAC.sub("xx:xx:xx:xx:xx:xx", line)
    line = _IP.sub("x.x.x.x", line)
    line = _MAIL.sub("<e-mail>", line)
    line = _PATH.sub("<pfad>", line)
    return _QUOTED.sub("'…'", line)


def build(who, area, text, context_lines, version, public=None):
    """→ (Titel, Text) genau so, wie er verschickt wird (public: ohne persönliche Angaben, für GitHub)."""
    public = PUBLIC if public is None else public
    who = (who or "").strip()[:40] or "Unbekannt"
    area_t = dict(AREAS).get(area, "Allgemein")
    text = (text or "").strip()
    if len(text) < 5:
        raise ReportError("Bitte kurz beschreiben, was nicht klappt")
    if public:
        text = _MAIL.sub("<e-mail>", _IP.sub("x.x.x.x", _MAC.sub("xx:xx:xx:xx:xx:xx", text)))
        first = re.sub(r"\s+", " ", text)[:60]
        title = f"[{area_t.split(' ')[0]}] {first}"
        head = [f"**Bereich:** {area_t}", f"**Version:** Hauskern {version}", f"**System:** {system_text()}", "",
                "**Was ist passiert?**", text[:1500], "", "**Technik** (automatisch, ohne Adressen/Namen)", "```"]
        body = "\n".join(head + [public_safe(x) for x in context_lines][:30] + ["```"])
        return title, body[:MAX_URL_BODY]
    title = f"🐞 Hauskern {version}: Problem von {who}"
    head = [f"Bereich: {area_t}", f"Wann: {time.strftime('%d.%m.%Y %H:%M')}", f"System: {system_text()}", "",
            text[:1500], "", "— Technik —"]
    body = "\n".join(head + [applog.redact(x) for x in context_lines])
    tail = log_tail()
    if tail:
        body += "\n\n— Letzte Fehler im Protokoll —\n" + "\n".join(tail)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 40] + "\n… (gekürzt – Rest steht im Anhang)"
    return title, body


def package(info_lines, title, body):
    """ZIP mit Meldung + Protokollen (für den Anhang und die Kopie auf dem eigenen Rechner)."""
    for h in applog.log.handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meldung.txt", title + "\n\n" + body)
        z.writestr("info.txt", applog.redact("\n".join(info_lines)))
        p = applog.path()
        if p:
            for suffix in ("", ".1", ".2"):
                if os.path.exists(p + suffix):
                    with open(p + suffix, "r", encoding="utf-8", errors="replace") as f:
                        z.writestr(os.path.basename(p + suffix), applog.redact(f.read()))
    return b.getvalue()


def _post(url, data, headers, method="POST", timeout=30):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": "Hauskern"}, **headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ReportError("Zu viele Meldungen auf einmal – bitte in ein paar Minuten nochmal") from e
        raise ReportError(f"Der Meldedienst antwortet mit Fehler {e.code}") from e
    except (OSError, ValueError) as e:
        raise ReportError("Kein Internet? Die Meldung konnte nicht verschickt werden – sie liegt aber als Datei "
                          "bereit (siehe unten)") from e


TARGET_FILE = "meldeziel.json"   # liegt im Programmordner – Pascal gibt es mit dem Familien-Paket weiter


def read_target_file(app_dir):
    try:
        with open(os.path.join(app_dir, TARGET_FILE), "r", encoding="utf-8") as f:
            t = json.load(f)
        tg = t.get("telegram") or {}
        return t if tg.get("token") and tg.get("chat_id") else None
    except (OSError, ValueError, AttributeError):
        return None


def write_target_file(app_dir, token, chat_id):
    t = {"telegram": {"token": token.strip(), "chat_id": str(chat_id).strip()},
         "hinweis": "Ziel für „Problem melden“ – nicht löschen, nicht weitergeben außer an die Familie."}
    with open(os.path.join(app_dir, TARGET_FILE), "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    return t


def _tg_call(token, method, data, ctype, timeout=30):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": "Hauskern"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ReportError("Zu viele Meldungen auf einmal – bitte in ein paar Minuten nochmal") from e
        raise ReportError(f"Telegram antwortet mit Fehler {e.code}") from e
    except (OSError, ValueError) as e:
        raise ReportError("Kein Internet? Die Meldung konnte nicht verschickt werden – sie liegt aber als Datei "
                          "bereit (siehe unten)") from e


def send_telegram(token, chat_id, title, body, attachment=None):
    import push as P
    tok = P._clean_token(token)
    text = (title + "\n\n" + body)[:4000]
    _tg_call(tok, "sendMessage", json.dumps({"chat_id": chat_id, "text": text,
                                             "disable_web_page_preview": True}).encode(), "application/json")
    if attachment:
        if len(attachment) > 45_000_000:
            return "Meldung verschickt – das Protokoll war zu groß für den Anhang"
        fn = f"Hauskern-Meldung-{time.strftime('%Y-%m-%d_%H-%M')}.zip"
        data, ctype = P._multipart({"chat_id": str(chat_id), "caption": "📎 Protokoll zu: " + title[:900]},
                                   [("document", fn, attachment, "application/zip")])
        try:
            _tg_call(tok, "sendDocument", data, ctype, timeout=90)
        except ReportError:
            return "Meldung verschickt – nur der Anhang ging nicht durch"
    return "✔ Danke! Die Meldung ist bei Pascal angekommen 🙂"


def issue_url(title, body, repo=None):
    """Link zu einer vorausgefüllten GitHub-Fehlermeldung (Consumer-Version) – oder "" ohne Projektseite."""
    repo = (repo if repo is not None else edition.GITHUB_REPO).strip().strip("/")
    if not repo:
        return ""
    q = urllib.parse.urlencode({"title": title, "body": body, "labels": "bug"})
    return f"https://github.com/{repo}/issues/new?{q}"


def send(title, body, attachment=None, topic=None, server=None, target=None):
    """Meldung (und ggf. ZIP) an Pascal schicken – per Telegram, wenn ein Meldeziel eingerichtet ist, sonst ntfy.
    Nur in der Familien-Version. Wirft ReportError mit verständlichem Text."""
    if PUBLIC:
        raise ReportError("In dieser Version wird nichts von selbst verschickt")
    if time.time() - _last["t"] < 60:
        raise ReportError("Gerade eben wurde schon eine Meldung geschickt – bitte kurz warten")
    tg = (target or {}).get("telegram") or {}
    if tg.get("token") and tg.get("chat_id"):
        msg = send_telegram(tg["token"], tg["chat_id"], title, body, attachment)
        _last["t"] = time.time()
        return msg
    server = (server or REPORT_SERVER).rstrip("/")
    topic = (topic or REPORT_TOPIC).strip()
    if not topic:
        raise ReportError("Kein Meldeziel eingerichtet – bitte die Meldung als Datei weitergeben (siehe unten)")
    url = f"{server}/{urllib.parse.quote(topic)}"
    # Optionen als URL-Parameter – so kommen Umlaute und Emojis im Titel sicher an
    _post(url + "?" + urllib.parse.urlencode({"title": title, "tags": "bug", "priority": "4"}), body.encode("utf-8"),
          {"Content-Type": "text/plain; charset=utf-8"})
    _last["t"] = time.time()
    if attachment:
        if len(attachment) > MAX_ATTACH:
            return "Meldung verschickt – das Protokoll war zu groß für den Anhang"
        fn = f"Hauskern-Meldung-{time.strftime('%Y-%m-%d_%H-%M')}.zip"
        try:
            _post(url + "?" + urllib.parse.urlencode({"filename": fn, "title": "📎 Protokoll zu: " + title,
                                                      "tags": "paperclip"}), attachment, {}, method="PUT", timeout=60)
        except ReportError:
            return "Meldung verschickt – nur der Anhang ging nicht durch"
    return "✔ Danke! Die Meldung ist bei Pascal angekommen 🙂"


def save_copy(folder, data):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, f"Hauskern-Meldung-{time.strftime('%Y-%m-%d_%H-%M')}.zip")
    with open(p, "wb") as f:
        f.write(data)
    return p
