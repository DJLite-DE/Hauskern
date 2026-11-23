# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
applog.py – Fehlerprotokoll der App.

* schreibt nach %APPDATA%\\Hauskern\\fehlerprotokoll.log (max. 3 × 1 MB, ältere werden überschrieben)
* fängt auch Fehler ab, die sonst still verschwinden würden (Hintergrund-Threads, Tk-Callbacks, pythonw)
* schwärzt Passwörter, Zugangscodes und Sitzungs-IDs, bevor etwas in die Datei geschrieben wird
* support_package() packt Protokoll + Systeminfos in eine ZIP zum Verschicken
"""
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import zipfile

log = logging.getLogger("fm")
LOG_NAME = "fehlerprotokoll.log"
_path = None

_REDACT = [
    (re.compile(r"(://)[^/@\s:]+:[^/@\s]+@"), r"\1***:***@"),                       # rtsp://user:pass@
    (re.compile(r"(sid=)[0-9a-fA-F]{8,}"), r"\1***"),                                # FRITZ!Box-Session
    (re.compile(r"(Authorization[\"']?\s*[:=]\s*[\"']?)(Basic|Digest|Bearer)\s+\S+", re.I), r"\1\2 ***"),
    (re.compile(r"((?:password|passwort|pw|code|response|access_code)(?:_plain)?[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+", re.I),
     r"\1***"),
    (re.compile(r"(/private-)[0-9a-f]+(/)", re.I), r"\1***\2"),                       # Google-Privatadresse
    (re.compile(r"(/bot)\d+:[\w-]+"), r"\1***"),                                       # Telegram-Bot-Token
]


def redact(text):
    text = str(text)
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup(folder, version="?"):
    """Richtet das Protokoll ein. Darf mehrfach aufgerufen werden."""
    global _path
    if _path:
        return _path
    os.makedirs(folder, exist_ok=True)
    _path = os.path.join(folder, LOG_NAME)
    h = logging.handlers.RotatingFileHandler(_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s [%(threadName)s] %(message)s",
                                     "%Y-%m-%d %H:%M:%S"))
    h.addFilter(_RedactFilter())
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False

    def excepthook(t, v, tb):
        log.critical("Unbehandelter Fehler", exc_info=(t, v, tb))
    sys.excepthook = excepthook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.error("Fehler im Hintergrund (%s)", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook
    log.info("=" * 60)
    log.info("Start %s %s · Python %s · %s %s", "Hauskern", version, platform.python_version(),
             platform.system(), platform.release())
    return _path


def path():
    return _path


class Throttle:
    """Verhindert, dass derselbe Fehler alle paar Sekunden ins Protokoll geschrieben wird."""

    def __init__(self, seconds=600):
        self.seconds, self.last = seconds, {}

    def ok(self, key):
        now = time.time()
        if now - self.last.get(key, 0) >= self.seconds:
            self.last[key] = now
            return True
        return False


def support_package(target_dir, info_lines):
    """Erstellt eine ZIP mit Protokoll(en) und Infos. Gibt den Pfad zurück."""
    os.makedirs(target_dir, exist_ok=True)
    zpath = os.path.join(target_dir, f"Hauskern-Fehlerprotokoll-{time.strftime('%Y-%m-%d_%H-%M')}.zip")
    for h in log.handlers:
        try:
            h.flush()
        except Exception:
            pass
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        if _path:
            for suffix in ("", ".1", ".2"):
                p = _path + suffix
                if os.path.exists(p):
                    z.write(p, os.path.basename(p))
        z.writestr("info.txt", redact("\n".join(info_lines)))
    return zpath
