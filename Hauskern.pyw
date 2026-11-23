# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
Hauskern – Netzwerk-Überblick für FRITZ!Box & FRITZ!DECT
Start: Doppelklick auf diese Datei (oder Start.bat). Benötigt nur Python 3.9+.
"""
import base64
import collections
import ctypes
import json
import os
import queue
import re
import sys
import copy
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fritz_api import FritzClient, FritzError, magic_packet, ping  # noqa: E402
from dashboard import Dashboard, sparkline, fnum  # noqa: E402
import bambu  # noqa: E402
from bambu import BambuPrinter  # noqa: E402
from camera import CameraStream, bambu_url, find_ffmpeg, grab_frame  # noqa: E402
from push import Pusher  # noqa: E402
from motion import MotionWatcher  # noqa: E402
import weather as wx  # noqa: E402
import pricing  # noqa: E402
import printwatch  # noqa: E402
import bambu_files  # noqa: E402
import updater  # noqa: E402
import edition  # noqa: E402
from onvif import OnvifCamera, OnvifError, with_credentials  # noqa: E402
from automations import Engine  # noqa: E402
import diagnose  # noqa: E402
from pages import (CostPage, DiscoveryPage, HistoryPage, DiagnosePage, ProbeDialog, AutomationsPage, BoxesDialog, PersonsDialog, PiDialog, PushDialog, CalendarPage, CameraDialog, CamerasPage, CamView,  # noqa: E402
                   DiscoverDialog, LogbookPage, day_label, event_time)
import calendar_sync  # noqa: E402
from store import Store  # noqa: E402
from winsys import Tray, get_autostart, make_icon, set_autostart  # noqa: E402
import datetime as dt  # noqa: E402
import applog  # noqa: E402
import socket  # noqa: E402
from webui import WebServer, check_pin, hash_pin  # noqa: E402
from applog import log  # noqa: E402


def M_channel(v):
    """Sendernummer aus Eingabe („102“, 102.0) → „102“ oder „“."""
    from prov_media import channel_keys, ProviderError
    try:
        return "".join(channel_keys(v))
    except ProviderError:
        return ""


def _media_keys():
    """Fernbedienungstasten (Fernseher, Sky) → Beschriftung."""
    from prov_media import KEYS
    return KEYS

APP = "Hauskern"
VERSION = "8.13.1"
OLD_APPS = ("Heimora", "Heimzentrale", "FritzMonitor")  # frühere Namen – Einstellungen werden übernommen
HERE = os.path.dirname(os.path.abspath(__file__))

BG, CARD, CARD2 = "#111318", "#1b1e25", "#272b34"
FG, MUTED = "#eceef2", "#8e96a3"
ACCENT, OK, BAD, WARN = "#4f8ff7", "#3ecf8e", "#f2626b", "#e0a33a"
DOWN_C, UP_C = "#4f8ff7", "#f0883e"


# --------------------------------------------------------------------------- #
# Konfiguration & Passwort (Windows-DPAPI, an dein Windows-Konto gebunden)
# --------------------------------------------------------------------------- #
HEADLESS = "--headless" in sys.argv  # Hintergrundbetrieb (z. B. Raspberry Pi als Dienst)
PROBE = "--probe" in sys.argv  # Probelauf am PC: wie auf dem Heimserver, aber mit eigenem Datenordner
PROBE_PORT_OFFSET = 1
PROBE_MAX_S = 45 * 60


# Logbuch-Kategorie → Recht, das man zum Sehen braucht
LOG_AREA = {"Anwesenheit": "home", "Drucker": "printers", "3D-Druck": "printers", "Kamera": "cams", "Netzwerk": "net",
            "Gerät": "smart", "Sprache": "smart", "Handy": "smart", "Automation": "automations", "App": "system",
            "Telefon": "calls"}


def _data_dir():
    forced = os.environ.get("HAUSKERN_DATA")
    if forced:  # Probelauf: eigener Ordner, keine Übernahme alter Daten
        os.makedirs(forced, exist_ok=True)
        return forced
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:  # Linux / Heimserver
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, APP)
    old = next((os.path.join(base, o) for o in OLD_APPS if os.path.isdir(os.path.join(base, o))), None)
    if not os.path.exists(d) and old:
        import shutil
        try:
            shutil.copytree(old, d)  # Umzug vom alten Namen: alles mitnehmen, alten Ordner als Sicherung lassen
        except OSError:
            pass
    os.makedirs(d, exist_ok=True)
    return d


def _cfg_path():
    return os.path.join(_data_dir(), "config.json")


DEFAULT_CFG = {"host": "fritz.box", "user": "", "password": "", "interval": 5, "smart_interval": 3,
               "ping_targets": [], "printers": [], "tile_images": {}, "hidden_tiles": [],
               "price_ct": 35.0, "tray": True, "notify": {}, "tray_hint_shown": False,
               "cameras": [], "automations": [], "rooms": [], "tile_room": {}, "tile_names": {},
               "calendars": [], "boxes": [], "persons": [],
               "web_enabled": False, "web_port": 8770, "web_pin": "", "web_sessions": [], "web_users": [],
               "wizard_done": False, "router": "", "router_name": "",
               "backup": {"enabled": True, "time": "03:30", "extra_dir": "", "pi_pull": True},
               "timelapse": {"modes": {}, "sources": {}},
               "spools": [], "filament_low": 150, "ams_low_pct": 10, "ams_warned": {}, "battery_warned": {},
               "calls": {"enabled": True, "last_id": 0},
               "vision": {"enabled": False, "sens": "mittel", "cams": {}},
               "lists": {},  # Einkaufsliste & Aufgaben  # Kamera-Erkennung – nur wenn eingeschaltet
               "weekly": {"enabled": False, "last": ""},  # Wochenrückblick – nur wenn selbst eingeschaltet
               "satellite": False, "pi_host": "", "pi_port": 8770,
               "print_cost": {}, "filaments": [],
               "update_token": "", "update_scan": True, "update_declined": "",
               "weather": {"enabled": True, "mode": "auto", "name": "", "lat": None, "lon": None}, "wol": [], "devices": [],
               "power": {}, "guest": {"index": 0, "off_at": 0, "hours": 0}, "power_sent": [],
               "push": {"enabled": False, "service": "ntfy", "printer_photo": True, "kinds": {},
                        "quiet": {"enabled": False, "from": "22:00", "to": "07:00"},
                        "ntfy": {"server": "https://ntfy.sh", "topic": "", "token": ""},
                        "pushover": {"token": "", "user": ""}, "telegram": {"token": "", "chat_id": ""}}}
PUSH_SECRETS = (("ntfy", "topic"), ("ntfy", "token"), ("pushover", "token"), ("pushover", "user"), ("telegram", "token"))

NAV = [("home", "⌂", "Zuhause"), ("energy", "⚡", "Energie"), ("history", "∿", "Verlauf"), ("costs", "€", "Druckkosten"), ("cameras", "◉", "Kameras"), ("calendar", "▦", "Kalender"),
       ("automations", "↻", "Automationen"), ("logbook", "☰", "Logbuch"), (None, "", "SYSTEM"),
       ("overview", "◎", "Netzwerk"), ("hosts", "▤", "Geräte"), ("wlan", "≋", "WLAN"),
       ("smart", "◈", "Smart-Home-Liste"), ("ping", "◌", "Ping-Monitor"),
       ("discover", "⌕", "Gerätesuche"), ("diagnose", "✓", "Diagnose")]
SIDE = "#0d0f13"

NOTIFY_KINDS = {"printer": "3D-Drucker", "internet": "Internet", "ping": "Ping-Ziele", "smart": "Smart Home",
                "presence": "Anwesenheit", "newdevice": "Neue Geräte im Netzwerk", "power": "Stromkosten",
                "guest": "Gast-WLAN", "health": "Heimserver-Gesundheit & Sicherung", "calls": "Verpasste Anrufe",
                "weekly": "Wochenrückblick", "plugin": "Erweiterungen", "vacation": "Urlaubsmodus",
                "appliance": "Waschmaschine & Co. fertig", "wxalert": "Unwetterwarnungen (DWD)",
                "alarm": "Alarm-Melder (Wasser, Rauch)", "contact": "Türen & Fenster"}
NOTIFY_DEFAULT_OFF = {"presence"}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        import users
        if users.migrate(cfg):  # alte Einzel-PIN → erster Handy-Nutzer
            save_cfg(cfg)
    except Exception:  # noqa: BLE001 – Einstellungen sollen trotzdem laden
        pass
    return cfg


_SAVE_LOCK = threading.Lock()


def save_cfg(cfg):
    """Atomar und threadsicher speichern (Webserver, Hintergrund und Oberfläche können gleichzeitig speichern)."""
    path = _cfg_path()
    with _SAVE_LOCK:
        for _ in range(5):  # falls ein anderer Thread gerade etwas einträgt
            try:
                data = json.dumps(cfg, indent=2, ensure_ascii=False)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            data = json.dumps(copy.deepcopy(cfg), indent=2, ensure_ascii=False)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        if os.name != "nt":
            os.chmod(tmp, 0o600)  # nur für den eigenen Benutzer lesbar
        for i in range(5):
            try:
                os.replace(tmp, path)  # atomar – ein Stromausfall beschädigt die Einstellungen nicht
                break
            except PermissionError:  # Windows: Virenscanner/Indexer hält die Datei kurz fest
                if i == 4:
                    raise
                time.sleep(0.1)


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, encrypt):
    buf = ctypes.create_string_buffer(data, len(data))
    bin_ = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bout = _BLOB()
    c = ctypes.windll.crypt32
    fn = c.CryptProtectData if encrypt else c.CryptUnprotectData
    if not fn(ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
        raise OSError("DPAPI-Fehler")
    try:
        return ctypes.string_at(bout.pbData, bout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(bout.pbData, ctypes.c_void_p))


def protect(pw):
    """Windows: mit DPAPI an das Windows-Konto gebunden. Linux/Heimserver: in der nur für dich lesbaren Datei (chmod 600)."""
    if not pw:
        return ""
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(pw.encode("utf-8"), True)).decode()
        except Exception:
            pass
    return "plain:" + base64.b64encode(pw.encode("utf-8")).decode()


def protect_plain(pw):
    return "plain:" + base64.b64encode(pw.encode("utf-8")).decode() if pw else ""


def unprotect(s):
    try:
        if s.startswith("dpapi:") and os.name == "nt":
            return _dpapi(base64.b64decode(s[6:]), False).decode("utf-8")
        if s.startswith("plain:"):
            return base64.b64decode(s[6:]).decode("utf-8")
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def fmt_dur(s):
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d} T {h} h {m} min" if d else f"{h} h {m} min"


def fmt_bits(bits):
    if bits >= 1e9:
        return f"{bits / 1e9:.2f} Gbit/s"
    if bits >= 1e6:
        return f"{bits / 1e6:.1f} Mbit/s"
    if bits >= 1e3:
        return f"{bits / 1e3:.0f} kbit/s"
    return f"{bits:.0f} bit/s"


def ip_key(ip):
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999,)


def fmt_c(v):
    if v is None:
        return "–"
    if isinstance(v, str):
        return v
    return f"{v:.1f} °C".replace(".", ",")


def update_tree(tree, rows):
    """rows: [(iid, values, tags)] – aktualisiert in place, Auswahl bleibt erhalten."""
    new_ids = [r[0] for r in rows]
    for iid in set(tree.get_children()) - set(new_ids):
        tree.delete(iid)
    for idx, (iid, values, tags) in enumerate(rows):
        if tree.exists(iid):
            tree.item(iid, values=values, tags=tags)
            tree.move(iid, "", idx)
        else:
            tree.insert("", idx, iid=iid, values=values, tags=tags)


# --------------------------------------------------------------------------- #
# Einstellungen-Dialog
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, cfg, password):
        super().__init__(master)
        self.title("Einstellungen")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        rk = cfg.get("router") or "fritz"
        hf = ttk.Frame(f)
        hf.grid(row=0, column=0, columnspan=2, sticky="we")
        ttk.Label(hf, text="Router", style="H1.TLabel").pack(side="left")
        self.RK = {"fritz": "FRITZ!Box", "generic": "Anderer Router", "none": "Ohne Router"}
        self.v_router = tk.StringVar(value=self.RK.get(rk, "FRITZ!Box"))
        rcb = ttk.Combobox(hf, textvariable=self.v_router, values=list(self.RK.values()), state="readonly", width=18)
        rcb.pack(side="left", padx=12)
        rcb.bind("<<ComboboxSelected>>", lambda e: self._router_mode())
        self.v_host = tk.StringVar(value=cfg["host"])
        self.v_user = tk.StringVar(value=cfg["user"])
        self.v_pw = tk.StringVar(value=password)
        self.v_save = tk.BooleanVar(value=bool(cfg.get("password")) or not cfg["user"])
        self.v_int = tk.StringVar(value=str(cfg.get("interval", 5)))
        self.v_price = tk.StringVar(value=str(cfg.get("price_ct", 35.0)).replace(".", ","))
        self.v_tray = tk.BooleanVar(value=cfg.get("tray", True))
        self.v_auto = tk.BooleanVar(value=get_autostart(APP))
        nt = cfg.get("notify", {})
        self.v_notify = {k: tk.BooleanVar(value=nt.get(k, k not in NOTIFY_DEFAULT_OFF)) for k in NOTIFY_KINDS}
        rows = [("Adresse", self.v_host, ""), ("Benutzername", self.v_user, ""), ("Passwort", self.v_pw, "•"),
                ("Aktualisieren alle (s)", self.v_int, "")]
        self.fritz_entries = []
        for i, (lbl, var, show) in enumerate(rows, start=1):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            e = ttk.Entry(f, textvariable=var, width=32, show=show)
            e.grid(row=i, column=1, sticky="we", pady=4)
            if i <= 3:
                self.fritz_entries.append(e)
            if (i == 2 and not var.get()) or (i == 3 and not var.get() and self.v_user.get()):
                e.focus_set()
        ttk.Checkbutton(f, text="Passwort verschlüsselt speichern", variable=self.v_save).grid(
            row=5, column=1, sticky="w", pady=(4, 8))
        self.hint_fritz = ("Der Benutzer braucht in der FRITZ!Box die Rechte „FRITZ!Box Einstellungen“\n"
                           "und „Smart Home“, außerdem: Heimnetz → Netzwerk → Netzwerkeinstellungen →\n"
                           "„Zugriff für Anwendungen zulassen“.")
        self.hint_generic = ("Anderer Router: Hauskern braucht keine Zugangsdaten. Internet-Status und Datenrate\n"
                             "kommen über UPnP (im Router einschalten), die Geräte über die Netzwerkliste des PCs.\n"
                             "WLAN schalten, Gast-WLAN und FRITZ!DECT gibt es nur mit FRITZ!Box.")
        self.l_hint = ttk.Label(f, text=self.hint_fritz, style="Muted.TLabel", justify="left")
        self.l_hint.grid(row=6, column=0, columnspan=2, sticky="w")
        self.after_idle(self._router_mode)

        ttk.Label(f, text="App & Energie", style="H1.TLabel").grid(row=7, column=0, columnspan=2, sticky="w",
                                                                  pady=(16, 4))
        ttk.Label(f, text="Strompreis (ct/kWh)").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_price, width=10).grid(row=8, column=1, sticky="w", pady=4)
        ttk.Checkbutton(f, text="Beim Schließen im Infobereich weiterlaufen", variable=self.v_tray).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(f, text="Mit Windows starten (im Infobereich)", variable=self.v_auto).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(f, text="Benachrichtigungen").grid(row=11, column=0, sticky="nw", pady=(8, 0))
        nf = ttk.Frame(f)
        nf.grid(row=11, column=1, sticky="w", pady=(8, 0))
        for i, (k, lbl) in enumerate(NOTIFY_KINDS.items()):
            ttk.Checkbutton(nf, text=lbl, variable=self.v_notify[k]).grid(row=i // 2, column=i % 2, sticky="w",
                                                                         padx=(0, 14), pady=1)
        ttk.Label(f, text="Handy-Zugriff", style="H1.TLabel").grid(row=12, column=0, columnspan=2, sticky="w",
                                                                 pady=(16, 4))
        self.v_web = tk.BooleanVar(value=cfg.get("web_enabled", False))
        ttk.Checkbutton(f, text="Handy-Oberfläche einschalten (nur Heimnetz / VPN)", variable=self.v_web).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(f, text="Port").grid(row=14, column=0, sticky="w", pady=4)
        self.v_wport = tk.StringVar(value=str(cfg.get("web_port", 8770)))
        ttk.Entry(f, textvariable=self.v_wport, width=8).grid(row=14, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Nutzer").grid(row=15, column=0, sticky="w", pady=4)
        self.v_wpin = tk.StringVar()
        pf = ttk.Frame(f)
        pf.grid(row=15, column=1, sticky="w", pady=4)
        n_users = len(cfg.get("web_users") or [])
        ttk.Button(pf, text="👥 Handy-Nutzer & Rechte …", command=lambda: master.users_dialog(parent=self)).pack(side="left")
        ttk.Label(pf, text=f"  {n_users} eingerichtet" if n_users else "  noch niemand – bitte anlegen",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(pf, text="📱 Adresse & QR-Code", command=lambda: master.phone_dialog(parent=self)).pack(
            side="left", padx=(10, 0))
        self.v_logout = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Alle angemeldeten Handys abmelden", variable=self.v_logout).grid(
            row=16, column=0, columnspan=2, sticky="w", pady=2)
        b = ttk.Frame(f)
        b.grid(row=17, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _router_mode(self):
        generic = self.v_router.get() != "FRITZ!Box"
        for e in self.fritz_entries[1:]:
            e.configure(state="disabled" if generic else "normal")
        self.l_hint.configure(text=self.hint_fritz if not generic else self.hint_generic
                              if self.v_router.get() == "Anderer Router" else
                              "Ohne Router: Hauskern zeigt nur 3D-Drucker, Kameras, Kalender und Wetter.")

    def _ok(self):
        try:
            interval = max(2, int(self.v_int.get()))
        except ValueError:
            interval = 5
        generic = self.v_router.get() != "FRITZ!Box"
        if generic and not self.v_host.get().strip():
            self.v_host.set("fritz.box")  # bleibt für einen späteren Wechsel stehen
        try:
            price = max(0.0, float(self.v_price.get().replace(",", ".")))
        except ValueError:
            messagebox.showwarning(APP, "Bitte einen Strompreis wie 35 oder 32,5 eingeben.", parent=self)
            return
        if not self.v_host.get().strip():
            messagebox.showwarning(APP, "Bitte eine Adresse angeben (z. B. fritz.box).", parent=self)
            return
        pin = self.v_wpin.get().strip()
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 12):
            messagebox.showwarning(APP, "Die PIN muss aus 4 bis 12 Ziffern bestehen.", parent=self)
            return
        try:
            wport = int(self.v_wport.get())
            assert 1024 <= wport <= 65535
        except (ValueError, AssertionError):
            messagebox.showwarning(APP, "Bitte einen Port zwischen 1024 und 65535 angeben (z. B. 8770).", parent=self)
            return
        self.web = {"enabled": self.v_web.get(), "port": wport, "pin": pin, "logout": self.v_logout.get()}
        self.result = {"host": self.v_host.get().strip(), "user": self.v_user.get().strip(),
                       "password": self.v_pw.get(), "save": self.v_save.get(), "interval": interval,
                       "price_ct": price, "tray": self.v_tray.get(), "autostart": self.v_auto.get(),
                       "notify": {k: v.get() for k, v in self.v_notify.items()},
                       "router": {v: k for k, v in self.RK.items()}.get(self.v_router.get(), "fritz")}
        self.destroy()


class PrinterDialog(tk.Toplevel):
    def __init__(self, master, cur, code, plugs):
        super().__init__(master)
        self.title("3D-Drucker hinzufügen" if not cur else "3D-Drucker bearbeiten")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        cur = cur or {}
        self.plugs = [("", "– keine –")] + plugs
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Bambu-Lab-Drucker (lokal)", style="H1.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.vars = {}
        fields = [("name", "Name", cur.get("name", "X2D"), ""), ("ip", "IP-Adresse", cur.get("ip", ""), ""),
                  ("serial", "Seriennummer", cur.get("serial", ""), ""), ("code", "LAN-Zugangscode", code, "•")]
        for i, (k, lbl, val, show) in enumerate(fields, start=1):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            v = tk.StringVar(value=val)
            ttk.Entry(f, textvariable=v, width=32, show=show).grid(row=i, column=1, sticky="we", pady=4)
            self.vars[k] = v
        ttk.Label(f, text="Steckdose (Stromkosten)").grid(row=5, column=0, sticky="w", pady=4, padx=(0, 12))
        names = [n for _, n in self.plugs]
        sel = next((n for a, n in self.plugs if a and a == cur.get("plug_ain")), names[0])
        self.v_plug = tk.StringVar(value=sel)
        ttk.Combobox(f, textvariable=self.v_plug, values=names, state="readonly", width=30).grid(
            row=5, column=1, sticky="we", pady=4)
        hint = ("IP, Zugangscode und Seriennummer findest du am Druckerdisplay unter\n"
                "Einstellungen → WLAN/Netzwerk bzw. Einstellungen → Gerät.\n"
                "Tipp: In der FRITZ!Box dem Drucker eine feste IP geben.\n"
                "Hängt der Drucker an einer FRITZ!DECT-Steckdose, wähle sie aus –\n"
                "dann rechnet die App Strom und Kosten pro Druck aus.\n"
                "Für Pause/Stopp braucht der Drucker meist „LAN-Modus“ + „Entwicklermodus“,\n"
                "für die Kamera „LAN Only Liveview“.")
        ttk.Label(f, text=hint, style="Muted.TLabel", justify="left").grid(row=6, column=0, columnspan=2, sticky="w",
                                                                           pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=7, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _ok(self):
        r = {k: v.get().strip() for k, v in self.vars.items()}
        if not (r["ip"] and r["serial"] and r["code"]):
            messagebox.showwarning(APP, "Bitte IP-Adresse, Seriennummer und Zugangscode eintragen.", parent=self)
            return
        r["name"] = r["name"] or "3D-Drucker"
        r["plug_ain"] = next((a for a, n in self.plugs if n == self.v_plug.get()), "")
        self.result = r
        self.destroy()


class DetailWindow(tk.Toplevel):
    """Großansicht eines DECT-Geräts mit Live-Verlauf."""

    def __init__(self, app, ain):
        super().__init__(app)
        self.app, self.ain = app, ain
        self.configure(bg=BG)
        self.geometry("620x360")
        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=12)
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        app.details.append(self)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()

    def _close(self):
        self.app.details.remove(self)
        self.destroy()

    def refresh(self):
        d = next((x for x in self.app.data["smart"] if x["ain"] == self.ain), None)
        c = self.canvas
        c.delete("all")
        if not d:
            return
        self.title(d["name"])
        w, h = c.winfo_width(), c.winfo_height()
        c.create_text(20, 26, text=d["name"], anchor="w", fill=FG, font=("Segoe UI Semibold", 15))
        c.create_text(20, 52, text=f"{d['kind']} · {d['product']} · AIN {d['ain']}", anchor="w", fill=MUTED,
                      font=("Segoe UI", 9))
        if d["power_w"] is not None:
            big, unit, col = f"{fnum(d['power_w'])} W", "Leistung", "#f6c15a"
            extra = f"Gesamtverbrauch {fnum(d['energy_kwh'] or 0, 2)} kWh"
        elif d["hkr"]:
            ist = d["hkr"]["ist"]
            big, unit, col = (f"{fnum(ist)} °C" if isinstance(ist, float) else "–"), "Raumtemperatur", "#ff7849"
            soll = d["hkr"]["soll"]
            extra = f"Soll {fnum(soll) + ' °C' if isinstance(soll, float) else soll}"
        else:
            big, unit, col = (f"{fnum(d['temp'])} °C" if d["temp"] is not None else "–"), "Temperatur", "#5ab0ff"
            extra = ""
        c.create_text(w - 20, 30, text=big, anchor="e", fill=col, font=("Segoe UI Semibold", 22))
        c.create_text(w - 20, 58, text=f"{unit}  {extra}", anchor="e", fill=MUTED, font=("Segoe UI", 9))
        hist = list(self.app.dash.power_hist.get(self.ain, []))
        if len(hist) >= 2:
            lo, hi = min(hist), max(hist)
            c.create_text(20, 90, text=f"max {fnum(hi)} · min {fnum(lo)}", anchor="w", fill=MUTED,
                          font=("Segoe UI", 9))
            sparkline(c, 20, 110, w - 20, h - 36, hist, col)
            secs = len(hist) * self.app.cfg.get("smart_interval", 3)
            c.create_text(20, h - 14, text=f"Verlauf der letzten {max(1, secs // 60)} min", anchor="w",
                          fill=MUTED, font=("Segoe UI", 8))
        else:
            c.create_text(w / 2, h / 2, text="Sammle Daten …", fill=MUTED)


# --------------------------------------------------------------------------- #
# Hauptfenster
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} · {edition.LABEL}" if edition.LABEL else APP)
        self.geometry("1180x740")
        self.minsize(900, 560)
        self.configure(bg=BG)
        self._style()

        self.cfg = load_cfg()
        self.password = unprotect(self.cfg.get("password", ""))
        self.client = None
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=16)
        self.hist = collections.deque(maxlen=120)
        self.data = {"hosts": [], "wlan": [], "smart": [], "ping": {}}
        self.sort = {}
        self.cols = {}
        self.last_seen = {}
        self.smart_pause_until = 0.0
        self.printers = {}
        self._temp_after = {}
        self.details = []
        self._pstate, self._perr, self._user_paused = {}, {}, set()
        self._prev_online = None
        self._ping_state, self._ping_fail = {}, {}
        self._dect_prev = {}
        self._sw_prev = {}
        self.camobjs = {}
        self.camviews = {}
        self._log_refresh_pending = False
        self.cal_events, self.cal_status, self._cal_last, self._cal_busy = [], {}, 0.0, False
        self.mesh_boxes, self.mesh_map = [], {}
        self.presence = {}
        self.web = None
        self._web_state = {}
        self._web_cam_demand = 0.0
        self._log_throttle, self._box_throttle = applog.Throttle(600), applog.Throttle(900)
        self._mesh_throttle, self._perr_txt = applog.Throttle(1800), {}
        self.extra_clients, self.extra_info = {}, {}
        self.main_info = {}
        self.known_macs, self.new_macs = None, {}
        self.motion, self.watchers, self._motion_log = {}, {}, applog.Throttle(120)
        import vision as _vision
        self.vision = _vision.Detector(os.path.join(_data_dir(), "modelle"))
        self.seen, self._vision_busy, self.vision_dl = {}, set(), None  # Kamera-Erkennung
        self._vac_applied, self._vac_t, self._vac_heat_t, self._vac_cam_t = {}, 0.0, {}, {}  # Urlaubsmodus
        self.appl_state = {}  # Waschmaschine & Co.
        self.wx_alerts, self._wxa_t, self._wxa_busy, self._wxa_err = [], 0.0, False, ""  # DWD-Warnungen
        import providers as _prov  # Geräte anderer Hersteller (Shelly, Hue, Tasmota, ESPHome, Zigbee)
        self.ext_devs, self.ext_err, self._fritz_devs, self._fritz_t, self._sensor_prev = {}, {}, [], 0.0, {}
        self.integrations = _prov.Manager(self._integration_cfgs,
                                          lambda pid, devs, err, urgent: self.results.put(
                                              ("ext_devs", (pid, devs, err, urgent))))
        self.weather, self._weather_busy, self._weather_err, self._weather_try = None, False, "", 0.0
        self._weather_dialog = None
        self.disc_results, self.disc_time, self.disc_running, self.disc_progress = [], 0.0, False, ""
        self._dev_last = {}  # Gerät-ID -> zuletzt online?
        self.guest = None  # Details zum Gast-WLAN (SSID, Passwort …)
        self._guest_fetched = 0.0
        self.power_rep, self.power_stats = None, self._load_power_stats()
        self._power_busy, self._qr_cache = False, None
        self._sess_lock = threading.Lock()
        self.voice_seen = 0.0  # wann die Matter-Bridge zuletzt gefragt hat
        self.health, self._health_sent, self._health_t = None, {}, 0.0
        self._backup_busy, self._pi_pull_t = False, 0.0
        self._tl_armed = set()
        self._bat_last, self._bat_cache, self._fil_unknown, self._plast_pct = {}, (0, []), {}, {}
        self.call_data, self.call_err, self._calls_busy = [], "", False
        self._heat_cache = None
        self._pw_seen = {}  # Fehldruck-Wächter: schon gemeldete Ereignisse pro Drucker und Druck
        import plugins as _plugins
        self.plugins = _plugins.Manager(self, os.path.join(_data_dir(), "erweiterungen"),
                                        os.path.join(HERE, "erweiterungen-beispiele"),
                                        post=lambda kind, payload: self.results.put(("plugin", (kind, payload))))
        self.power_rep = getattr(self, "power_rep", None)  # Drucker, bei denen der nächste/aktuelle Druck aufgenommen werden soll
        self.tl = self._make_timelapse()
        self.diag_results, self.diag_time, self.diag_running, self.diag_progress = [], 0.0, False, ""
        self.probe_proc, self.probe_dir, self.probe_started = None, "", 0.0
        self.engine = Engine(self, lambda: self.cfg.get("automations", []))
        self.pusher = Pusher(self.push_cfg, lambda ok, msg: self.results.put(("push_result", (ok, msg))))

        bundled = os.path.join(HERE, "assets", "app.ico")
        self.icon_path = bundled if os.path.exists(bundled) else make_icon(os.path.join(_data_dir(), "app.ico"))
        if os.name == "nt":
            try:
                self.iconbitmap(default=self.icon_path)
            except tk.TclError:
                pass
        try:
            self._icon_img = tk.PhotoImage(file=os.path.join(HERE, "assets", "icon-512.png")).subsample(8)
            self.iconphoto(True, self._icon_img)
        except (tk.TclError, OSError):
            pass
        # Autostart vom alten Namen übernehmen
        try:
            for old_name in (() if HEADLESS else OLD_APPS):
                if get_autostart(old_name):
                    set_autostart(old_name, os.path.abspath(__file__), False)
                    set_autostart(APP, os.path.abspath(__file__), True)
        except OSError:
            pass
        self.store = Store(os.path.join(_data_dir(), "verlauf.db"))
        if HEADLESS:  # kein Symbol im Infobereich – sonst hätte der Probelauf am PC ein zweites
            self.tray = _NoTray()
        else:
            self.tray = Tray(f"{APP} – dein Zuhause, lokal", self.icon_path, lambda ev: self.results.put(("tray", ev)))

        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(150, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close_window)
        self.start_printers()
        self.start_cameras()
        self.automations_changed(save=False)
        self.after(2000, self.refresh_energy)
        self.after(1000, self._cam_tick)
        self.after(1500, self._cal_tick)
        self.after(2500, self.start_web)
        self.after(20000, self._engine_tick)
        self.log("App", APP, "gestartet" + (" (Probelauf)" if PROBE else " (Hintergrundbetrieb)" if HEADLESS else ""))
        self._load_last_diag()
        self.after(60000, self._minute_tick)
        self.after(3000, self._weather_tick)
        self.after(15000, self._power_tick)
        self.after(25000, self._calls_tick)
        self.after(60000, self._weekly_tick)
        self.after(4000, self._lists_tile)
        self.after(90000, self._lists_prune)
        self.after(3000, self._plugins_start)
        self.after(1500, self._integrations_start)
        self.after(20000, lambda: self._health_tick(force=True))
        if not HEADLESS:
            self.after(4000, self.check_downloads)
            if self.cfg.get("pi_host"):
                self.after(25000, self.pi_sync)
        if HEADLESS:
            self.after(60000, lambda: self.run_diagnose("Start"))
            self.after(5000, self._probe_tick)
        self._update_sat_label()
        if HEADLESS:
            self.withdraw()
            log.info("Hintergrundbetrieb (--headless) – Bedienung über die Handy-Oberfläche")
        elif "--tray" in sys.argv and self.cfg["user"] and self.password and self.cfg.get("tray", True):
            self.withdraw()
            self.after(3000, lambda: None if self.tray.ok else self.deiconify())
        else:
            self._fade_in()
        if self.router_kind() == "generic" or (self.cfg["user"] and self.password):
            self.jobs.put(("connect",))
        elif HEADLESS:
            if self.router_kind() != "none":
                log.error("Keine FRITZ!Box-Zugangsdaten – bitte am PC einrichten und an den Heimserver senden")
        elif not self.cfg.get("wizard_done"):
            self.after(400, self.wizard)  # erster Start: Einrichtungs-Assistent
        elif self.router_kind() != "none":
            self.after(300, self.open_settings)
        self.after(2500, self._whats_new_check)
        if not edition.is_family() and not self.cfg.get("legal_ok"):
            self.after(900, self._legal_notice)

    def _legal_notice(self):
        """Öffentliche Version: einmal beim ersten Start auf Haftung & Sicherheit hinweisen."""
        self.cfg["legal_ok"] = time.strftime("%Y-%m-%d")
        self.save()
        messagebox.showinfo(APP, "Willkommen bei Hauskern 🏠\n\nHauskern ist freie Software (GPL-3.0) und kommt OHNE JEDE "
                                 "GEWÄHR. Es schaltet echte Geräte – bitte keine Geräte darüber steuern, bei denen "
                                 "unbeaufsichtigtes Ein- oder Ausschalten gefährlich sein kann.\n\nHauskern nie per "
                                 "Portfreigabe ins Internet stellen – von unterwegs nur per VPN.\n\nMehr dazu jederzeit "
                                 "unter „⋯ Mehr“ → „📜 Lizenz & Rechtliches“.", parent=self)

    def legal_dialog(self):
        p = os.path.join(HERE, "RECHTLICHES.txt")
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = "RECHTLICHES.txt fehlt im Programmordner. Hauskern ist freie Software unter der GPL-3.0 – ohne Gewähr."
        d = tk.Toplevel(self)
        d.title("Lizenz & Rechtliches")
        d.configure(bg=BG)
        t = tk.Text(d, width=100, height=34, bg=CARD, fg=FG, relief="flat", wrap="word", font=("Consolas", 9),
                    padx=12, pady=10)
        t.pack(fill="both", expand=True)
        t.insert("1.0", f"{APP} {VERSION}" + (f" · {edition.LABEL}" if edition.LABEL else "") + "\n\n" + text)
        t.configure(state="disabled")
        ttk.Button(d, text="Vollständiger Lizenztext (LICENSE.txt)",
                   command=lambda: self._open_file(os.path.join(HERE, "LICENSE.txt"))).pack(side="left", padx=10, pady=8)
        ttk.Button(d, text="Schließen", command=d.destroy).pack(side="right", padx=10, pady=8)

    def _open_file(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            else:
                webbrowser.open("file://" + path)
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo(APP, f"Datei: {path}\n({e})")

    def donate(self):
        if edition.DONATE_URL:
            webbrowser.open(edition.DONATE_URL)

    def report_callback_exception(self, exc, val, tb):
        """Fehler in Klick-/Timer-Funktionen: protokollieren statt still verschwinden."""
        if issubclass(exc, tk.TclError) and (self.stop.is_set() or "invalid command name" in str(val)):
            log.debug("Fenster schon geschlossen: %s", val)  # z. B. Kamerafenster zu, Antwort kam später
            return
        log.error("Fehler in der Oberfläche", exc_info=(exc, val, tb))
        try:
            self._on_error(f"{exc.__name__}: {val}")
        except Exception:
            pass

    # ---------------- Styling ----------------
    def _fade_in(self, step=0):
        """Fenster blendet beim Start weich ein (wo Windows/Linux das kann)."""
        try:
            self.attributes("-alpha", min(1.0, 0.15 + step * 0.17))
            if step < 5:
                self.after(28, self._fade_in, step + 1)
        except tk.TclError:
            pass

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        base = ("Segoe UI", 10)
        s.configure(".", background=BG, foreground=FG, fieldbackground=CARD2, font=base,
                    bordercolor=CARD2, lightcolor=CARD2, darkcolor=CARD2, troughcolor=CARD)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("H1.TLabel", font=("Segoe UI Semibold", 15))
        s.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        s.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("CardValue.TLabel", background=CARD, foreground=FG, font=("Segoe UI Semibold", 14))
        s.configure("CardSub.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        s.layout("Hidden.TNotebook.Tab", [])
        s.configure("Hidden.TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(16, 7), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background=CARD2, foreground=FG, relief="flat", padding=(6, 5),
                    font=("Segoe UI Semibold", 9))
        s.map("Treeview.Heading", background=[("active", "#343944")])
        s.map("Treeview", background=[("selected", "#2f5fae")], foreground=[("selected", "#ffffff")])
        s.configure("TButton", background=CARD2, foreground=FG, borderwidth=0, padding=(14, 7), focuscolor=CARD2)
        s.map("TButton", background=[("active", "#343944"), ("disabled", CARD)], foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        s.map("Accent.TButton", background=[("active", "#6aa2fa")])
        s.configure("TEntry", fieldbackground=CARD2, foreground=FG, insertcolor=FG, padding=5)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("Vertical.TScrollbar", background=CARD2, arrowcolor=MUTED, bordercolor=BG)

    # ---------------- Aufbau ----------------
    def _build(self):
        side = tk.Frame(self, bg=SIDE, width=214)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        logo = tk.Canvas(side, bg=SIDE, highlightthickness=0, height=64)
        logo.pack(fill="x")
        try:
            self._logo_img = tk.PhotoImage(file=os.path.join(HERE, "assets", "logo-40.png"))
            logo.create_image(34, 32, image=self._logo_img)
        except (tk.TclError, OSError):
            from dashboard import rrect as _rr
            _rr(logo, 14, 12, 54, 52, 11, fill=ACCENT, outline="")
            logo.create_polygon(34, 19, 47, 31, 44, 31, 44, 45, 24, 45, 24, 31, 21, 31, fill="#ffffff", outline="")
        t1 = logo.create_text(62, 25, text="Haus", anchor="w", fill=FG, font=("Segoe UI Semibold", 13))
        x2 = (logo.bbox(t1) or (0, 0, 106, 0))[2]
        logo.create_text(x2, 25, text="kern", anchor="w", fill=FG, font=("Segoe UI", 13))
        logo.create_text(62, 44, text="Dein Zuhause, lokal", anchor="w", fill=MUTED, font=("Segoe UI", 8))
        self.nav = {}
        for key, icon, label in NAV:
            if key is None:
                tk.Label(side, text=label, bg=SIDE, fg="#5d636e", font=("Segoe UI Semibold", 8), anchor="w",
                         padx=20).pack(fill="x", pady=(14, 2))
                continue
            lbl = tk.Label(side, text=f"{icon}   {label}", bg=SIDE, fg=MUTED, font=("Segoe UI", 11), anchor="w",
                           padx=18, pady=7, cursor="hand2")
            lbl.pack(fill="x", padx=8, pady=1)
            lbl.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            lbl.bind("<Enter>", lambda e, w=lbl, k=key: w.configure(bg="#181b22") if k != self.page else None)
            lbl.bind("<Leave>", lambda e, w=lbl, k=key: w.configure(bg=SIDE) if k != self.page else None)
            self.nav[key] = lbl
        st = tk.Label(side, text="⚙   Einstellungen", bg=SIDE, fg=MUTED, font=("Segoe UI", 11), anchor="w",
                      padx=18, pady=7, cursor="hand2")
        st.pack(side="bottom", fill="x", padx=8, pady=(2, 12))
        st.bind("<Button-1>", lambda e: self.open_settings())
        mo = tk.Label(side, text="⋯   Mehr", bg=SIDE, fg=MUTED, font=("Segoe UI", 11), anchor="w",
                      padx=18, pady=7, cursor="hand2")
        mo.pack(side="bottom", fill="x", padx=8)
        mo.bind("<Button-1>", lambda e: self._more_menu(mo))
        mo.bind("<Enter>", lambda e: mo.configure(bg="#181b22"))
        mo.bind("<Leave>", lambda e: mo.configure(bg=SIDE))
        self.l_sat = tk.Label(side, text="", bg=SIDE, fg="#e0a33a", font=("Segoe UI", 8), anchor="w", padx=20,
                              justify="left")
        self.l_sat.pack(side="bottom", fill="x")
        self.page = "home"

        main = ttk.Frame(self)
        main.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(main, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        self.l_page = ttk.Label(top, text="Zuhause", style="H1.TLabel")
        self.l_page.pack(side="left")
        self.l_title = ttk.Label(top, text="", style="Muted.TLabel")
        self.l_title.pack(side="left", padx=12)
        ttk.Button(top, text="Aktualisieren", style="Accent.TButton",
                   command=lambda: self.jobs.put(("refresh",))).pack(side="right", padx=6)
        self.l_status = ttk.Label(top, text="Nicht verbunden", style="Muted.TLabel")
        self.l_status.pack(side="right", padx=12)

        self.nb = ttk.Notebook(main, style="Hidden.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.dash = Dashboard(self.nb, self)
        self.dash.configure(padding=8)
        self.tab_home = self.dash
        self.nb.add(self.dash, text="Zuhause")
        self._tab_overview()
        ttk.Button(self.tab_overview, text="FRITZ!Boxen im Heimnetz …", command=self.boxes_dialog).pack(
            anchor="e", padx=5, pady=(4, 0))
        self._tab_hosts()
        self._tab_wlan()
        self._tab_smart()
        self._tab_energy()
        self._tab_ping()
        self.tab_cameras = CamerasPage(self.nb, self)
        self.nb.add(self.tab_cameras, text="Kameras")
        self.tab_automations = AutomationsPage(self.nb, self)
        self.nb.add(self.tab_automations, text="Automationen")
        self.tab_logbook = LogbookPage(self.nb, self)
        self.nb.add(self.tab_logbook, text="Logbuch")
        self.tab_calendar = CalendarPage(self.nb, self)
        self.nb.add(self.tab_calendar, text="Kalender")
        self.tab_history = HistoryPage(self.nb, self)
        self.nb.add(self.tab_history, text="Verlauf")
        self.tab_costs = CostPage(self.nb, self)
        self.nb.add(self.tab_costs, text="Druckkosten")
        self.tab_discover = DiscoveryPage(self.nb, self)
        self.nb.add(self.tab_discover, text="Gerätesuche")
        self.tab_diagnose = DiagnosePage(self.nb, self)
        self.nb.add(self.tab_diagnose, text="Diagnose")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._page_changed())
        self._page_changed()

    def _card(self, parent, title, r, c):
        f = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        f.grid(row=r, column=c, sticky="nsew", padx=5, pady=5)
        ttk.Label(f, text=title, style="CardTitle.TLabel").pack(anchor="w")
        v = tk.Label(f, text="–", bg=CARD, fg=FG, font=("Segoe UI Semibold", 14), anchor="w")
        v.pack(anchor="w", fill="x")
        sub = ttk.Label(f, text="", style="CardSub.TLabel")
        sub.pack(anchor="w")
        return v, sub

    def _tab_overview(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_overview = t
        self.nb.add(t, text="Übersicht")
        grid = ttk.Frame(t)
        grid.pack(fill="x")
        for c in range(4):
            grid.columnconfigure(c, weight=1, uniform="c")
        self.c = {}
        specs = [("box", "FRITZ!Box"), ("inet", "Internet"), ("ip", "Externe IP"), ("line", "Leitung"),
                 ("down", "Download aktuell"), ("up", "Upload aktuell"), ("hosts", "Geräte online"),
                 ("smart", "Smart Home")]
        for i, (k, title) in enumerate(specs):
            self.c[k] = self._card(grid, title, i // 4, i % 4)
        gf = ttk.Frame(t, style="Card.TFrame", padding=10)
        gf.pack(fill="both", expand=True, padx=5, pady=5)
        hdr = ttk.Frame(gf, style="Card.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Durchsatz (letzte Minuten)", style="CardTitle.TLabel").pack(side="left")
        tk.Label(hdr, text="■ Download", bg=CARD, fg=DOWN_C, font=("Segoe UI", 9)).pack(side="right", padx=6)
        tk.Label(hdr, text="■ Upload", bg=CARD, fg=UP_C, font=("Segoe UI", 9)).pack(side="right")
        self.graph = tk.Canvas(gf, bg=CARD, highlightthickness=0, height=220)
        self.graph.pack(fill="both", expand=True, pady=(6, 0))
        self.graph.bind("<Configure>", lambda e: self._draw_graph())

    def _make_tree(self, parent, cols, key):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.cols[key] = [c[0] for c in cols]
        tree = ttk.Treeview(wrap, columns=self.cols[key], show="headings", selectmode="browse")
        for cid, text, width, anchor in cols:
            tree.heading(cid, text=text, command=lambda c=cid: self._sort_by(key, c))
            tree.column(cid, width=width, anchor=anchor, stretch=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("on", foreground=FG)
        tree.tag_configure("off", foreground=MUTED)
        tree.tag_configure("bad", foreground=BAD)
        tree.tag_configure("warn", foreground=WARN)
        return tree

    def _tab_hosts(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_hosts = t
        self.nb.add(t, text="Geräte")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Suchen:").pack(side="left")
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *a: self._render_hosts())
        ttk.Entry(bar, textvariable=self.v_filter, width=28).pack(side="left", padx=6)
        self.v_online = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="nur online", variable=self.v_online,
                        command=self._render_hosts).pack(side="left", padx=6)
        self.l_hostcount = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_hostcount.pack(side="left", padx=10)
        ttk.Button(bar, text="Zum Ping-Monitor", command=self._host_to_ping).pack(side="right")
        ttk.Button(bar, text="Alle als bekannt merken", command=self.relearn_hosts).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="⏻ Aufwecken", command=self._host_wake).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="＋ Zu Hauskern hinzufügen …", command=self._host_add).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Im Browser öffnen", command=self._host_open).pack(side="right", padx=6)
        self.v_hview = tk.StringVar(value="Liste")
        cbv = ttk.Combobox(bar, textvariable=self.v_hview, values=["Liste", "Nach FRITZ!Box", "Netzwerkkarte"],
                           state="readonly", width=16)
        cbv.pack(side="right", padx=6)
        cbv.bind("<<ComboboxSelected>>", lambda e: self._render_hosts())
        ttk.Label(bar, text="Ansicht:").pack(side="right")
        cols = [("st", "", 34, "center"), ("name", "Name", 230, "w"), ("ip", "IP-Adresse", 130, "w"),
                ("mac", "MAC-Adresse", 150, "w"), ("conn", "Verbindung", 190, "w"), ("speed", "Geschwindigkeit", 120, "e")]
        self.t_hosts = self._make_tree(t, cols, "hosts")
        self.t_hosts.tag_configure("group", foreground=FG, font=("Segoe UI Semibold", 10))
        self.hosts_wrap = self.t_hosts.master
        mw = tk.Frame(t, bg=BG)
        self.hmap = tk.Canvas(mw, bg=BG, highlightthickness=0)
        msb = ttk.Scrollbar(mw, orient="vertical", command=self.hmap.yview)
        self.hmap.configure(yscrollcommand=msb.set)
        msb.pack(side="right", fill="y")
        self.hmap.pack(side="left", fill="both", expand=True)
        self.hmap.bind("<Configure>", lambda e: self._draw_hmap() if self.v_hview.get() == "Netzwerkkarte" else None)
        self.hmap_wrap = mw
        self._hview_mode = "Liste"
        self.sort["hosts"] = ("ip", False)
        self.t_hosts.bind("<Double-1>", lambda e: self._host_open())

    def _tab_wlan(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_wlan = t
        self.nb.add(t, text="WLAN")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Deine Funknetze – Gäste-WLAN ist meist das letzte in der Liste.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Ein/Aus schalten", command=self._wlan_toggle).pack(side="right")
        ttk.Button(bar, text="📶 Gast-WLAN & QR-Code …", style="Accent.TButton",
                   command=self.guest_dialog).pack(side="right", padx=6)
        cols = [("st", "", 34, "center"), ("nr", "Nr.", 50, "center"), ("band", "Band", 90, "w"),
                ("ssid", "SSID", 240, "w"), ("status", "Status", 110, "w"), ("ch", "Kanal", 70, "center"),
                ("std", "Standard", 90, "center"), ("cl", "Clients", 80, "center")]
        self.t_wlan = self._make_tree(t, cols, "wlan")
        self.l_wlan_fo = ttk.Label(t, text="", style="Muted.TLabel", justify="left")

    def _tab_smart(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_smart = t
        self.nb.add(t, text="Smart Home (DECT)")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 8))
        self.l_smart = ttk.Label(bar, text="Doppelklick: Steckdose schalten bzw. Temperatur einstellen",
                                 style="Muted.TLabel")
        self.l_smart.pack(side="left")
        ttk.Button(bar, text="Solltemperatur …", command=self._smart_temp).pack(side="right")
        ttk.Button(bar, text="Ein/Aus schalten", command=self._smart_toggle).pack(side="right", padx=6)
        cols = [("st", "", 34, "center"), ("name", "Name", 200, "w"), ("kind", "Typ", 130, "w"),
                ("prod", "Modell", 140, "w"), ("state", "Zustand", 80, "center"), ("temp", "Temperatur", 95, "e"),
                ("power", "Leistung", 85, "e"), ("energy", "Verbrauch", 95, "e"), ("hkr", "Ist / Soll", 130, "center"),
                ("bat", "Batterie", 75, "center")]
        self.t_smart = self._make_tree(t, cols, "smart")
        self.sort["smart"] = ("name", False)
        self.l_smart_fo = ttk.Label(t, text="", style="Muted.TLabel", justify="left")
        self.t_smart.bind("<Double-1>", lambda e: self._smart_double())

    def _tab_energy(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_energy = t
        self.nb.add(t, text="Energie")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Anzeigen für:").pack(side="left")
        self.v_edev = tk.StringVar(value="Alle Steckdosen")
        self.cb_edev = ttk.Combobox(bar, textvariable=self.v_edev, state="readonly", width=30,
                                    values=["Alle Steckdosen"])
        self.cb_edev.pack(side="left", padx=6)
        self.cb_edev.bind("<<ComboboxSelected>>", lambda e: self.refresh_energy(reschedule=False))
        self.l_price = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_price.pack(side="right")
        ttk.Button(bar, text="⚙ Strompreis & Warnungen …", command=self.power_settings).pack(side="right", padx=(0, 10))
        grid = ttk.Frame(t)
        grid.pack(fill="x")
        for c in range(5):
            grid.columnconfigure(c, weight=1, uniform="e")
        self.ec = {}
        for i, (k, title) in enumerate((("heute", "Heute"), ("Monat", "Dieser Monat"), ("prog", "Hochrechnung Monat"),
                                        ("prev", "Letzter Monat"), ("year", "Letzte 12 Monate"))):
            self.ec[k] = self._card(grid, title, 0, i)
        gf = ttk.Frame(t, style="Card.TFrame", padding=10)
        gf.pack(fill="both", expand=True, padx=5, pady=5)
        gh = ttk.Frame(gf, style="Card.TFrame")
        gh.pack(fill="x")
        self.l_echart = ttk.Label(gh, text="Verbrauch pro Tag (30 Tage)", style="CardTitle.TLabel")
        self.l_echart.pack(side="left")
        self.v_emode = tk.StringVar(value="Tage")
        for txt in ("12 Monate", "30 Tage"):
            ttk.Radiobutton(gh, text=txt, value=txt.split()[1], variable=self.v_emode,
                            command=self._draw_energy_chart).pack(side="right", padx=4)
        self.echart = tk.Canvas(gf, bg=CARD, highlightthickness=0, height=170)
        self.echart.pack(fill="both", expand=True, pady=(4, 0))
        self.echart.bind("<Configure>", lambda e: self._draw_energy_chart())
        self._edaily = []
        low = ttk.Frame(t)
        low.pack(fill="both", expand=True)
        low.columnconfigure(0, weight=2)
        low.columnconfigure(1, weight=1)
        low.rowconfigure(1, weight=1)
        ttk.Label(low, text="Geräte", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=5)
        ttk.Label(low, text="Letzte Drucke", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=5)
        f1, f2 = ttk.Frame(low), ttk.Frame(low)
        f1.grid(row=1, column=0, sticky="nsew", padx=5, pady=4)
        f2.grid(row=1, column=1, sticky="nsew", padx=5, pady=4)
        self.t_edev = self._make_tree(f1, [("name", "Gerät", 140, "w"), ("now", "Jetzt", 60, "e"),
                                           ("month", "Monat bisher", 125, "e"), ("prog", "Hochrechnung", 85, "e"),
                                           ("prev", "Vormonat", 70, "e"), ("trend", "Trend", 90, "w"),
                                           ("base", "Grundlast", 115, "e")], "edev")
        self.t_prints = self._make_tree(f2, [("date", "Datum", 110, "w"), ("file", "Datei", 170, "w"),
                                             ("dur", "Dauer", 80, "e"), ("kwh", "Strom", 75, "e"),
                                             ("cost", "Kosten", 70, "e"), ("res", "Ergebnis", 90, "w")], "prints")

    def eur(self, kwh):
        return f"{fnum(kwh * self.cfg.get('price_ct', 35.0) / 100, 2)} €"

    def refresh_energy(self, reschedule=True):
        try:
            devs = self.store.devices()
            names = ["Alle Steckdosen"] + [f"{n}" for n in devs.values()]
            self.cb_edev.configure(values=names)
            sel = self.v_edev.get()
            ain = next((a for a, n in devs.items() if n == sel), None)
            import power as P
            per = self.store.periods()
            today_kwh = self.store.kwh(ain, *per["heute"])
            self.l_price.configure(text=f"Strompreis {fnum(self.cfg.get('price_ct', 35.0))} ct/kWh")
            rep = self.power_rep
            rows_r = (rep or {}).get("devices", [])
            if ain:
                rows_r = [r for r in rows_r if r["ain"] == ain]
            tot = {k: sum((r.get(k) or 0) for r in rows_r) for k in ("month_kwh", "prog_kwh", "year_kwh")}
            prev_l = [r["prev_kwh"] for r in rows_r if r.get("prev_kwh") is not None]
            tot["prev_kwh"] = sum(prev_l) if prev_l else None
            pr_ = self.cfg.get("price_ct", 35.0)
            self.ec["heute"][0].configure(text=f"{fnum(today_kwh, 2)} kWh")
            self.ec["heute"][1].configure(text=self.eur(today_kwh))
            if rep:
                t_ = rep["total"]
                self.ec["Monat"][0].configure(text=P.fmt_eur(P.eur(tot["month_kwh"], pr_)))
                self.ec["Monat"][1].configure(text=f"{fnum(tot['month_kwh'], 1)} kWh · Tag {t_['day']} von {t_['days']}")
                self.ec["prog"][0].configure(text="≈ " + P.fmt_eur(P.eur(tot["prog_kwh"], pr_)))
                sub = f"≈ {fnum(tot['prog_kwh'], 0)} kWh · Jahr ≈ {P.fmt_eur(P.eur(tot['prog_kwh'] * 12, pr_))}"
                if t_["budget_eur"] and not ain:
                    sub = (f"⚠ über Budget ({P.fmt_eur(t_['budget_eur'])})" if t_["over_budget"]
                           else f"✓ Budget {P.fmt_eur(t_['budget_eur'])}")
                self.ec["prog"][1].configure(text=sub, foreground=BAD if t_["over_budget"] and not ain else MUTED)
                self.ec["prev"][0].configure(text=P.fmt_eur(None if tot["prev_kwh"] is None else
                                                           P.eur(tot["prev_kwh"], pr_)))
                self.ec["prev"][1].configure(text=f"{t_['prev_name']} · {fnum(tot['prev_kwh'] or 0, 1)} kWh"
                                             if tot["prev_kwh"] is not None else "noch keine Daten")
                self.ec["year"][0].configure(text=P.fmt_eur(P.eur(tot["year_kwh"], pr_)))
                sb = sum(r["standby_eur_year"] or 0 for r in rows_r)
                self.ec["year"][1].configure(text=f"{fnum(tot['year_kwh'], 0)} kWh" +
                                             (f" · Grundlast ≈ {P.fmt_eur(sb)}/Jahr" if sb >= 1 else ""))
            else:
                for k in ("Monat", "prog", "prev", "year"):
                    self.ec[k][0].configure(text="…")
                    self.ec[k][1].configure(text="wird berechnet")
            self._edaily = self.store.daily(ain, 30)
            self._emonths = [(k, sum((r.get("months_map") or {}).get(k) or 0 for r in rows_r))
                             for k, _ in (rep or {}).get("months", [])] if rep else []
            self._draw_energy_chart()
            live = {d["ain"]: d["power_w"] for d in self.data["smart"]}
            rows = []
            for r in (rep or {}).get("devices", []):
                now = live.get(r["ain"], r.get("now_w"))
                base = "–" if r["standby_w"] is None else \
                    f"{fnum(r['standby_w'])} W · {P.fmt_eur(r['standby_eur_year'])}/J."
                warn = r["trend"] is not None and r["trend"] >= 0.5
                rows.append((r["ain"], (r["name"], "–" if now is None else f"{fnum(now)} W",
                                        f"{fnum(r['month_kwh'], 1)} kWh · {P.fmt_eur(r['month_eur'])}",
                                        "≈ " + P.fmt_eur(r["prog_eur"]), P.fmt_eur(r["prev_eur"]),
                                        P.fmt_trend(r["trend"]), base), ("warn" if warn else "on",)))
            update_tree(self.t_edev, rows)
            prow = []
            for i, (printer, file, start, end, swh, ewh, res) in enumerate(self.store.prints()):
                kwh = (ewh - swh) / 1000 if swh is not None and ewh is not None and ewh >= swh else None
                dur = fmt_dur(end - start) if end and start else "–"
                prow.append((str(i), (time.strftime("%d.%m. %H:%M", time.localtime(start)), file or printer, dur,
                                      "–" if kwh is None else f"{fnum(kwh, 2)} kWh",
                                      "–" if kwh is None else self.eur(kwh), res or ""),
                             ("on" if res == "fertig" else "warn",)))
            update_tree(self.t_prints, prow)
        except Exception as e:
            log.exception("Energie-Auswertung")
            self._on_error(f"Energie: {e}")
        if reschedule:
            self.after(60000, self.refresh_energy)

    def _draw_energy_chart(self):
        c = self.echart
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if self.v_emode.get() == "Monate":
            return self._draw_month_chart(c, w, h)
        self.l_echart.configure(text="Verbrauch pro Tag (30 Tage)")
        data = self._edaily
        if w < 50 or not data:
            return
        mx = max([v for _, v in data] + [0.1])
        pad_b, pad_t = 20, 18
        bw = (w - 20) / len(data)
        total = sum(v for _, v in data)
        c.create_text(w - 6, 6, text=f"30 Tage: {fnum(total, 1)} kWh · {self.eur(total)}", anchor="ne",
                      fill=MUTED, font=("Segoe UI", 9))
        for i, (day, v) in enumerate(data):
            x = 10 + i * bw
            bh = (h - pad_b - pad_t) * v / mx
            col = "#f6c15a" if day == dt.date.today() else "#4f8ff7"
            if v > 0:
                c.create_rectangle(x + 2, h - pad_b - bh, x + bw - 2, h - pad_b, fill=col, outline="")
            if i % 5 == 0 or i == len(data) - 1:
                c.create_text(x + bw / 2, h - 8, text=day.strftime("%d.%m."), fill=MUTED, font=("Segoe UI", 8))
        if total == 0:
            c.create_text(w / 2, h / 2, text="Noch keine Daten – der Verlauf füllt sich ab jetzt jede Minute 🙂",
                          fill=MUTED, font=("Segoe UI", 10))

    def _draw_month_chart(self, c, w, h):
        import power as P
        self.l_echart.configure(text="Stromkosten pro Monat (12 Monate)")
        data = getattr(self, "_emonths", [])
        if w < 50 or not data:
            c.create_text(w / 2, h / 2, text="Monatswerte werden geladen …", fill=MUTED, font=("Segoe UI", 10))
            return
        price = self.cfg.get("price_ct", 35.0)
        vals = [P.eur(v, price) for _, v in data]
        mx = max(vals + [0.5])
        pad_b, pad_t = 20, 22
        bw = (w - 20) / len(data)
        cur = data[-1][0]
        for i, ((k, kwh), v) in enumerate(zip(data, vals)):
            x = 10 + i * bw
            bh = (h - pad_b - pad_t) * v / mx
            if v > 0:
                c.create_rectangle(x + 4, h - pad_b - bh, x + bw - 4, h - pad_b, fill="#f6c15a" if k == cur else "#4f8ff7",
                                   outline="")
                c.create_text(x + bw / 2, h - pad_b - bh - 8, text=fnum(v, 0) + " €", fill=MUTED, font=("Segoe UI", 8))
            c.create_text(x + bw / 2, h - 8, text=P.month_name(k, True) + ("" if k[5:] != "01" else " " + k[2:4]),
                          fill=FG if k == cur else MUTED, font=("Segoe UI", 8))
        c.create_text(w - 6, 6, text=f"12 Monate: {fnum(sum(vals), 2)} € · aktueller Monat bisher (gelb)", anchor="ne",
                      fill=MUTED, font=("Segoe UI", 9))

    # ---------------- Stromkosten ----------------
    def _power_path(self):
        return os.path.join(_data_dir(), "stromstatistik.json")

    def _load_power_stats(self):
        try:
            with open(self._power_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def power_settings(self):
        from pages import PowerSettingsDialog
        d = PowerSettingsDialog(self, self)
        self.wait_window(d)
        if d.result:
            price, ps = d.result
            self.cfg["price_ct"] = price
            self.cfg["power"] = ps
            self.save()
            self.analyze_power()

    def _power_tick(self):
        """Alle 30 Minuten: Tages-/Monatswerte der Steckdosen von der FRITZ!Box holen und auswerten."""
        if not self.stop.is_set():
            self.after(30 * 60 * 1000, self._power_tick)
        ains = [(d["ain"], d["name"]) for d in self.data.get("smart", [])
                if d.get("power_w") is not None and not d.get("is_group") and d.get("present")]
        if not ains or not self.client:
            self.analyze_power()
            return

        def fetch(c, ains=ains):
            out = {}
            for ain, name in ains:
                try:
                    st = c.energy_stats(ain)
                    out[ain] = {"name": name, "months": {k: v for k, v in st["months"] if v is not None},
                                "days": {d.isoformat(): v for d, v in st["days"] if v is not None}}
                except Exception as e:  # noqa: BLE001 – ältere Geräte/FRITZ!OS: dann eigene Aufzeichnung
                    log.info("Energie-Statistik %s: %s", name, e)
            self.results.put(("power_stats", out))
        self.jobs.put(("action", fetch))

    def _on_power_stats(self, out):
        for ain, st in out.items():
            old = self.power_stats.get(ain, {})
            months = dict(old.get("months", {}), **st["months"])  # ältere Monate behalten
            self.power_stats[ain] = {"name": st["name"], "months": months, "days": st["days"], "ts": time.time()}
        try:
            tmp = self._power_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.power_stats, f)
            os.replace(tmp, self._power_path())
        except OSError as e:
            log.warning("Stromstatistik speichern: %s", e)
        self.analyze_power()

    def analyze_power(self):
        """Auswertung im Hintergrund (liest die eigene Aufzeichnung) → _on_power_rep."""
        if self._power_busy:
            return
        self._power_busy = True
        price, settings = self.cfg.get("price_ct", 35.0), dict(self.cfg.get("power") or {})
        live = {d["ain"]: d for d in self.data.get("smart", []) if not d.get("is_group")}
        stats = json.loads(json.dumps(self.power_stats))

        def work():
            import power as P
            try:
                st = self.store
                names = dict(st.devices())
                for ain, d in live.items():
                    if d.get("power_w") is not None:
                        names.setdefault(ain, d["name"])
                for ain, s_ in stats.items():
                    names.setdefault(ain, s_.get("name", ain))
                per = st.periods()
                now = dt.datetime.now()
                cur = P.month_key(now)
                first = st.first_ts()
                devices = []
                for ain, name in names.items():
                    if ain in live:
                        name = live[ain]["name"]
                    months = dict(stats.get(ain, {}).get("months", {}))
                    if first:  # eigene Aufzeichnung, wo die Box nichts liefert
                        for k in [cur] + P.prev_months(cur, 11):
                            y, m = map(int, k.split("-"))
                            a = dt.datetime(y, m, 1)
                            b = dt.datetime(y + (m == 12), m % 12 + 1, 1)
                            if k in months or (k != cur and a.timestamp() < first - 3600):
                                continue  # angebrochene Monate vor Aufzeichnungsbeginn wären zu niedrig
                            months[k] = st.kwh(ain, int(a.timestamp()), int(b.timestamp()))
                    samples = st.power_samples(ain, time.time() - 7 * 86400)
                    box_cur = cur in stats.get(ain, {}).get("months", {})
                    devices.append({"ain": ain, "name": name, "now_w": (live.get(ain) or {}).get("power_w"),
                                    "since": None if box_cur else st.first_ts(ain),
                                    "today_kwh": st.kwh(ain, *per["heute"]), "months": months,
                                    "standby_w": P.percentile(samples, 0.1) if len(samples) > 600 else None})
                rep = P.analyze(devices, price, settings)
                mm = {d["ain"]: d["months"] for d in devices}
                for r in rep["devices"]:
                    r["months_map"] = mm.get(r["ain"], {})
                self.results.put(("power_rep", rep))
            except Exception:
                log.exception("Stromkosten-Auswertung")
                self.results.put(("power_rep", None))
        threading.Thread(target=work, daemon=True, name="Stromkosten").start()

    def _on_power_rep(self, rep):
        self._power_busy = False
        if rep is None:
            return
        self.power_rep = rep
        sent = list(self.cfg.get("power_sent", []))
        for a in rep["alerts"]:
            if a["id"] in sent:
                continue
            sent.append(a["id"])
            self.notify("power", a["title"], a["text"], warn=a["kind"] != "report")
        if sent != self.cfg.get("power_sent", []):
            self.cfg["power_sent"] = sent[-60:]
            self.save()
        t = self.dash.tiles.get("dect:_energy")
        if t:
            t.model.update(month_eur=rep["total"]["month_eur"], prog_eur=rep["total"]["prog_eur"],
                           over_budget=rep["total"]["over_budget"])
            t.redraw()
        if self.page == "energy":
            self.refresh_energy(reschedule=False)

    def web_power(self):
        import power as P
        rep = self.power_rep
        if not rep:
            return {"ready": False}
        t = rep["total"]
        return {"ready": True, "price_ct": rep["price_ct"], "total": {k: t[k] for k in (
            "month_eur", "month_kwh", "prog_eur", "prog_kwh", "prev_eur", "prev_kwh", "prev_name", "cur_name", "year_eur",
            "year_kwh", "today_eur", "today_kwh", "budget_eur", "over_budget", "standby_eur_year", "day", "days")},
            "months": [{"m": P.month_name(k, True), "eur": P.eur(v, rep["price_ct"]) if v is not None else None}
                       for k, v in rep["months"]],
            "devices": [{"name": r["name"], "now_w": r["now_w"], "month_eur": r["month_eur"], "month_kwh": r["month_kwh"],
                         "prog_eur": r["prog_eur"], "prev_eur": r["prev_eur"], "trend": P.fmt_trend(r["trend"]),
                         "warn": bool(r["trend"] is not None and r["trend"] >= 0.5),
                         "standby_w": r["standby_w"], "standby_eur_year": r["standby_eur_year"]}
                        for r in rep["devices"]]}

    # ---------------- Gast-WLAN ----------------
    def guest_index(self):
        """Welches WLAN der Haupt-Box ist das Gast-WLAN? Eingestellt oder automatisch erkannt."""
        idx = int((self.cfg.get("guest") or {}).get("index") or 0)
        main = [w for w in self.data.get("wlan", []) if w.get("box", "main") == "main"]
        if idx:
            return idx if any(w["index"] == idx for w in main) else None
        named = [w for w in main if re.search(r"gast|guest", w.get("ssid", ""), re.I)]
        if named:
            return named[0]["index"]
        if len(main) >= 3:  # AVM: das letzte WLAN ist das Gast-WLAN (3 bei Dual-, 4 bei Tri-Band)
            return max(w["index"] for w in main)
        return None

    def guest_wlan(self):
        idx = self.guest_index()
        return next((w for w in self.data.get("wlan", []) if w.get("box", "main") == "main" and w["index"] == idx), None)

    def fetch_guest(self, force=False):
        idx = self.guest_index()
        if not idx or not self.client or (not force and time.time() - self._guest_fetched < 600):
            return
        self._guest_fetched = time.time()

        def job(c, idx=idx):
            try:
                self.results.put(("guest", c.wlan_details(idx)))
            except Exception as e:  # noqa: BLE001
                self.results.put(("guest", {"index": idx, "error": str(e)}))
        self.jobs.put(("action", job))

    def _on_guest(self, g):
        if g.get("error"):
            log.warning("Gast-WLAN lesen: %s", g["error"])
            if self.guest and self.guest.get("index") == g["index"]:
                return
        self.guest = g
        self._guest_tile()
        dlg = getattr(self, "_guest_dialog", None)
        if dlg:
            from pages import ui_later
            ui_later(dlg, dlg.refresh)

    def guest_qr_text(self):
        import qr
        g = self.guest or {}
        if not g.get("ssid"):
            return None
        return qr.wifi_text(g["ssid"], g.get("password", ""), g.get("security", "WPA"))

    def guest_svg(self, size=None):
        import qr
        t = self.guest_qr_text()
        if not t:
            return ""
        c = self._qr_cache
        if not c or c[0] != t:
            c = self._qr_cache = (t, qr.encode(t))
        return qr.svg(c[1], size)

    def set_guest(self, on, hours=None, source=""):
        idx = self.guest_index()
        if not idx:
            raise RuntimeError("Kein Gast-WLAN gefunden – bitte in der FRITZ!Box einrichten (WLAN → Gastzugang)")
        g = self.cfg.setdefault("guest", {})
        if hours is not None:
            g["hours"] = hours
        hours = g.get("hours", 0) if hours is None else hours
        g["off_at"] = time.time() + hours * 3600 if on and hours else 0
        self.save()
        self.jobs.put(("action", lambda c: c.set_wlan(idx, on), "full"))
        w = self.guest_wlan()
        if w:
            w["enabled"] = on
        txt = "eingeschaltet" if on else "ausgeschaltet"
        if on and g["off_at"]:
            txt += f" – schaltet um {time.strftime('%H:%M', time.localtime(g['off_at']))} Uhr wieder ab"
        self.log("Netzwerk", "Gast-WLAN", txt + (f" ({source})" if source else ""))
        self._guest_tile()
        self.fetch_guest(force=True)
        return "Gast-WLAN " + txt

    def new_guest_password(self):
        import secrets
        alpha = "abcdefghjkmnpqrstuvwxyz23456789"
        pw = "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(3))
        idx = self.guest_index()
        if not idx:
            raise RuntimeError("Kein Gast-WLAN gefunden")
        self.jobs.put(("action", lambda c: c.set_wlan_password(idx, pw), "full"))
        if self.guest:
            self.guest["password"] = pw
        self.log("Netzwerk", "Gast-WLAN", "neues Passwort vergeben")
        self.after(4000, lambda: self.fetch_guest(force=True))
        self._guest_tile()
        return pw

    def _guest_check_timer(self):
        g = self.cfg.get("guest") or {}
        if g.get("off_at") and time.time() >= g["off_at"]:
            g["off_at"] = 0
            self.save()
            w = self.guest_wlan()
            if w and w["enabled"]:
                try:
                    self.set_guest(False, source="Zeit abgelaufen")
                    self.notify("guest", "Gast-WLAN ausgeschaltet", "Die eingestellte Zeit ist abgelaufen.")
                except RuntimeError as e:
                    log.warning("Gast-WLAN abschalten: %s", e)

    def _guest_tile(self):
        w = self.guest_wlan()
        if not w:
            self.dash.remove_missing("Netzwerk", "guest:", [])
            self.dash.commit()
            return
        g = self.guest or {}
        off = (self.cfg.get("guest") or {}).get("off_at", 0)
        self.dash.put("Netzwerk", "guest:wlan", {"type": "guest", "wlan": w, "ssid": g.get("ssid") or w["ssid"],
                                                 "off_at": off if w["enabled"] else 0, "has_qr": bool(g.get("ssid"))})
        self.dash.commit()

    def guest_dialog(self):
        from pages import GuestDialog
        if not self.guest_index():
            messagebox.showinfo(APP, "Ich finde kein Gast-WLAN auf deiner FRITZ!Box.\n\nRichte es in der FRITZ!Box unter "
                                     "WLAN → Gastzugang einmal ein – danach erscheint es hier automatisch.")
            return
        self.fetch_guest(force=True)
        d = GuestDialog(self, self)
        self._guest_dialog = d
        self.wait_window(d)
        self._guest_dialog = None

    def guest_print(self):
        import webbrowser
        g = self.guest or {}
        if not g.get("ssid"):
            messagebox.showinfo(APP, "Die Daten des Gast-WLANs sind noch nicht geladen – bitte kurz warten.")
            return
        from html import escape as esc
        page = f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Gast-WLAN</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;text-align:center;color:#111;margin:40px}}
.card{{display:inline-block;border:2px solid #222;border-radius:24px;padding:36px 48px}}
h1{{font-size:34px;margin:0 0 6px}}p{{font-size:18px;margin:4px 0;color:#444}}
.qr{{width:340px;height:340px;margin:22px auto}}.k{{font-size:22px;color:#111;font-weight:600}}
@media print{{button{{display:none}}}}</style></head><body><div class="card">
<h1>📶 Gast-WLAN</h1><p>Kamera aufs Handy halten und tippen – fertig.</p>
<div class="qr">{self.guest_svg(340)}</div>
<p>WLAN-Name</p><p class="k">{esc(g['ssid'])}</p>
<p style="margin-top:12px">Passwort</p><p class="k">{esc(g.get('password') or '(ohne Passwort)')}</p></div>
<p><button onclick="print()" style="font-size:18px;padding:10px 24px;margin-top:24px">Drucken</button></p>
</body></html>"""
        path = os.path.join(_data_dir(), "gast-wlan.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        webbrowser.open("file:///" + path.replace("\\", "/"))

    def web_guest(self):
        w = self.guest_wlan()
        if not w:
            return None
        g = self.guest or {}
        return {"key": "guest:wlan", "enabled": w["enabled"], "ssid": g.get("ssid") or w["ssid"],
                "password": g.get("password", ""), "clients": w.get("clients"),
                "off_at": (self.cfg.get("guest") or {}).get("off_at", 0) if w["enabled"] else 0,
                "hours": (self.cfg.get("guest") or {}).get("hours", 0), "qr": self.guest_svg()}

    # ---------------- Benachrichtigungen / Tray ----------------
    def notify(self, kind, title, text, warn=False, image_fn=None):
        if kind != "presence":  # Anwesenheit schreibt ihren eigenen Logbuch-Eintrag
            self.log({"printer": "Drucker", "internet": "Netzwerk", "ping": "Netzwerk", "smart": "Gerät",
                  "newdevice": "Netzwerk"}.get(kind, "App"),
                     "", f"{title} – {text}" if text else title)
        if self.cfg.get("notify", {}).get(kind, kind not in NOTIFY_DEFAULT_OFF):
            self.tray.notify(title, text, warn)
        self.push(kind, title, text, warn, image_fn)

    # ---------------- Navigation ----------------
    PAGE_TITLES = {k: label for k, _, label in NAV if k}

    def _page_frames(self):
        return {"home": self.tab_home, "energy": self.tab_energy, "cameras": self.tab_cameras,
                "calendar": self.tab_calendar,
                "automations": self.tab_automations, "logbook": self.tab_logbook, "overview": self.tab_overview,
                "hosts": self.tab_hosts, "wlan": self.tab_wlan, "smart": self.tab_smart, "ping": self.tab_ping,
                "diagnose": self.tab_diagnose, "history": self.tab_history, "discover": self.tab_discover,
                "costs": self.tab_costs}

    def show_page(self, key):
        self.nb.select(self._page_frames()[key])

    def _page_changed(self):
        cur = str(self.nb.select())
        key = next((k for k, f in self._page_frames().items() if str(f) == cur), "home")
        self.page = key
        for k, w in self.nav.items():
            w.configure(bg="#1d3253" if k == key else SIDE, fg="#ffffff" if k == key else MUTED,
                        font=("Segoe UI Semibold", 11) if k == key else ("Segoe UI", 11))
        self.l_page.configure(text=self.PAGE_TITLES.get(key, ""))
        if key == "logbook":
            self.tab_logbook.refresh()
        elif key == "automations":
            self.tab_automations.refresh()
        elif key == "cameras":
            self.tab_cameras.refresh()
        elif key == "calendar":
            self.tab_calendar.refresh()
        elif key == "energy":
            self.refresh_energy(reschedule=False)
        elif key == "history":
            self.tab_history.refresh()
        elif key == "costs":
            self.tab_costs.refresh()
        elif key == "discover":
            if not self.disc_results and not self.disc_running:
                self._load_discovery()
            self.tab_discover.refresh()
        elif key == "diagnose":
            self.tab_diagnose.refresh()
            if not self.diag_results and not self.diag_running:
                self.run_diagnose("Seite geöffnet")

    # ---------------- Logbuch ----------------
    def log(self, kind, name, text):
        """Thread-sicher."""
        try:
            self.store.log(kind, name, text)
        except Exception:
            return
        self.results.put(("logbook", None))

    def support_info(self):
        c = self.cfg
        lines = [f"{APP} {VERSION}", f"Erstellt: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"Python {sys.version.split()[0]} · {os.name}", f"Haupt-Box: {self.main_info.get('model', '?')} "
                 f"FRITZ!OS {self.main_info.get('firmware', '?')}", f"Verbunden: {bool(self.client)}",
                 f"Zusätzliche Boxen: {len(c.get('boxes', []))}", f"Mesh-Boxen: {[b['model'] for b in self.mesh_boxes]}",
                 f"Smart-Home-Geräte: {[(d['product'], d['kind'], d['present']) for d in self.data['smart']]}",
                 f"WLANs: {[(w['band'], w.get('box_name')) for w in self.data['wlan']]}",
                 f"Drucker: {[(p.name, p.connected, p.error, p.raw.get('gcode_state')) for p in self.printers.values()]}",
                 f"Kameras: {[(e['cfg'].get('brand'), e['cfg'].get('port'), e.get('status'), bool(e.get('onvif') and e['onvif'].has_ptz)) for e in self.camobjs.values()]}",
                 f"Kalender: {[(k.get('kind', k.get('type'))) for k in c.get('calendars', [])]} → {list(self.cal_status.values())}",
                 f"Personen: {len(c.get('persons', []))} → {[(st['state']) for st in self.presence.values()]}", f"Automationen: {len(c.get('automations', []))}",
                 f"ffmpeg: {bool(find_ffmpeg())}", f"Tray: {self.tray.ok}"]
        if self.diag_results:
            lines += ["", diagnose.as_text(self.diag_results, f"Letzte Diagnose ({time.strftime('%d.%m. %H:%M', time.localtime(self.diag_time))})")]
        return lines

    # ---------------- Problem melden ----------------
    def bug_context(self):
        """Kurze Technik-Infos für eine Fehlermeldung (ohne Passwörter; öffentliche Version auch ohne Namen)."""
        c = self.cfg
        if not edition.is_family():
            lines = [f"Hauskern {VERSION} · {'Heimserver' if HEADLESS else 'PC'}" + (" · Satellit" if c.get("satellite") else ""),
                     f"Router: {self.router_kind() or '?'} · verbunden: {bool(self.client)} · "
                     f"{self.main_info.get('model', '')} {self.main_info.get('firmware', '')}".rstrip()]
            for ic in c.get("integrations", []):
                st = str(self.integration_status(ic["id"])).split(" · FRITZ")[0]
                lines.append(f"Hersteller {ic.get('type')}: {st}")
            for e in self.camobjs.values():
                lines.append(f"Kamera ({e['cfg'].get('brand') or '?'}): {e.get('status', '?')}")
            for p in self.printers.values():
                lines.append(f"Drucker: {'verbunden' if p.connected else 'getrennt'} {p.error or ''}".rstrip())
            lines.append(f"Geräte: {len(self.data['smart'])} · Automationen: {len(c.get('automations', []))}")
            if self.diag_results:
                bad = [f"{diagnose.ICON[r['status']]} {r['name']}" for r in self.diag_results
                       if r.get("status") in (diagnose.WARN, diagnose.BAD)]
                lines += ["Diagnose: " + (", ".join(bad[:12]) or "alles in Ordnung")]
            return lines
        lines = [f"Hauskern {VERSION} · {'Heimserver' if HEADLESS else 'PC'}" + (" · Satellit" if c.get("satellite") else ""),
                 f"Router: {self.router_kind() or '?'} · verbunden: {bool(self.client)} · "
                 f"{self.main_info.get('model', '')} {self.main_info.get('firmware', '')}".rstrip()]
        for ic in c.get("integrations", []):
            lines.append(f"Hersteller {ic.get('type')} {ic.get('host')}: {self.integration_status(ic['id'])}")
        for e in self.camobjs.values():
            lines.append(f"Kamera {e['cfg'].get('name') or e['cfg'].get('brand') or '?'} ({e['cfg'].get('brand') or '?'}): {e.get('status', '?')}")
        for p in self.printers.values():
            lines.append(f"Drucker {p.name}: {'verbunden' if p.connected else 'getrennt'} {p.error or ''}".rstrip())
        lines.append(f"Geräte: {len(self.data['smart'])} · Automationen: {len(c.get('automations', []))} · "
                     f"Personen: {len(c.get('persons', []))}")
        if self.diag_results:
            bad = [f"{diagnose.ICON[r['status']]} {r['name']}: {r['detail']}" for r in self.diag_results
                   if r.get("status") in (diagnose.WARN, diagnose.BAD)]
            lines += ["Diagnose:"] + (bad[:12] or ["alles in Ordnung"])
        return lines

    def bug_report(self, who, area, text, attach=True, preview=False, done=None):
        """Meldung an Pascal. preview=True → nur (Titel, Text) zurück. Sonst wird im Hintergrund verschickt,
        done(ok, msg) meldet das Ergebnis. Eine Kopie landet immer auf dem eigenen Rechner."""
        import bugreport as BR
        title, body = BR.build(who, area, text, self.bug_context(), VERSION)
        if preview:
            return title, body
        data = BR.package(self.support_info(), title, body)
        if BR.PUBLIC:
            return self._bug_report_public(BR, who, area, text, title, body, data, done)
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            copy = BR.save_copy(desk if os.path.isdir(desk) and not HEADLESS else os.path.join(_data_dir(), "meldungen"),
                                data)
        except OSError:
            copy = ""
        if who and who.strip():
            self.cfg["bug_name"] = who.strip()[:40]
            self.save()
        self.log("System", "Problem melden", f"{dict(BR.AREAS).get(area, area)}: {text.strip()[:80]}")

        def run():
            try:
                msg = BR.send(title, body, data if attach else None, topic=self.cfg.get("bug_topic"),
                              target=self.bug_target())
                ok = True
            except BR.ReportError as e:
                ok, msg = False, f"⚠ {e}"
            if copy:
                msg += f"\nKopie gespeichert: {copy}"
            log.info("Problem melden: %s", msg.splitlines()[0])
            if done:
                done(ok, msg)
        threading.Thread(target=run, daemon=True, name="Problem melden").start()
        return title, body

    def _bug_report_public(self, BR, who, area, text, title, body, data, done):
        """Öffentliche Version: nichts verschicken – vorausgefüllte GitHub-Meldung öffnen, Protokoll als Datei."""
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            copy = BR.save_copy(desk if os.path.isdir(desk) and not HEADLESS else os.path.join(_data_dir(), "meldungen"),
                                data)
        except OSError:
            copy = ""
        url = BR.issue_url(title, body)
        self._last_bug = {"url": url, "copy": copy}
        self.log("System", "Problem melden", f"{dict(BR.AREAS).get(area, area)}: {text.strip()[:80]}")
        if url:
            msg = ("🌐 Die Fehlermeldung öffnet sich auf GitHub. Kurz prüfen (sie ist öffentlich sichtbar), "
                   "ggf. anmelden und auf „Create“ klicken – danke! 🙂")
            if not HEADLESS:
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    msg = f"Bitte diesen Link im Browser öffnen:\n{url}"
        else:
            msg = "Für diese Version ist noch keine Projektseite eingetragen – deine Meldung liegt als Datei bereit."
        if copy:
            msg += f"\nProtokoll (ZIP, nur anhängen, wenn du magst): {copy}"
        if done:
            done(bool(url), msg)
        return title, body

    def _on_bug_done(self, payload):
        ok, msg = payload
        dlg = getattr(self, "_bug_dlg", None)
        if dlg:
            dlg.result(ok, msg)
        else:
            self._on_status(msg.splitlines()[0])

    def bug_target(self):
        """Wohin „Problem melden“ geht: meldeziel.json im Programmordner (kommt mit Pascals Familien-Paket),
        sonst die Kopie in den Einstellungen (überlebt eine Neuinstallation ohne die Datei)."""
        import bugreport as BR
        if BR.PUBLIC:
            return None
        t = BR.read_target_file(HERE)
        if t:
            tg = t["telegram"]
            saved = (self.cfg.get("bug_target") or {}).get("telegram") or {}
            if saved.get("chat_id") != tg["chat_id"] or unprotect(saved.get("token", "")) != tg["token"]:
                self.cfg["bug_target"] = {"telegram": {"token": protect(tg["token"]), "chat_id": tg["chat_id"]}}
                self.save()
            return t
        saved = (self.cfg.get("bug_target") or {}).get("telegram") or {}
        if saved.get("token") and saved.get("chat_id"):
            return {"telegram": {"token": unprotect(saved["token"]), "chat_id": saved["chat_id"]}}
        return None

    def set_bug_target(self, token, chat_id):
        import bugreport as BR
        BR.write_target_file(HERE, token, chat_id)
        self.cfg["bug_target"] = {"telegram": {"token": protect(token.strip()), "chat_id": str(chat_id)}}
        self.save()

    def family_package(self):
        """Laufende Version + Meldeziel als ZIP für die Familie → Pfad. Mit Signatur, wenn die Installation
        unverändert der offiziellen entspricht – dann klappt das Paket bei der Familie auch als Update."""
        try:
            data = updater.build_signed_copy(HERE, extra=["meldeziel.json"])
        except updater.UpdateError as e:
            log.warning("Familien-Paket ohne Signatur: %s", e)
            data = updater.build_package(HERE)
        folder = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(folder):
            folder = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, f"Hauskern-{VERSION}-Familie.zip")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def bug_receive_dialog(self):
        from pages import BugReceiveDialog
        BugReceiveDialog(self, self)

    def bug_dialog(self, area="allgemein", text=""):
        from pages import BugReportDialog
        BugReportDialog(self, self, area, text)

    def save_support_package(self):
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            z = applog.support_package(desk if os.path.isdir(desk) else _data_dir(), self.support_info())
        except Exception as e:
            log.exception("Support-Paket")
            messagebox.showerror(APP, f"Konnte das Paket nicht erstellen: {e}")
            return
        messagebox.showinfo(APP, f"Fehlerprotokoll gespeichert:\n{z}\n\nPasswörter und Zugangscodes sind darin "
                                 "geschwärzt. Die Datei kannst du mir einfach schicken 🙂")

    def open_log(self):
        p = applog.path()
        if p and os.path.exists(p) and os.name == "nt":
            os.startfile(p)  # noqa: S606
        elif p:
            messagebox.showinfo(APP, f"Protokoll: {p}")

    # ---------------- Push-Nachrichten ----------------
    def push_cfg(self):
        """Push-Einstellungen mit entschlüsselten Zugangsdaten (thread-sicher lesbar)."""
        import copy
        c = copy.deepcopy(DEFAULT_CFG["push"])
        for k, v in copy.deepcopy(self.cfg.get("push", {})).items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
        for svc, field in PUSH_SECRETS:
            val = c.get(svc, {}).get(field, "")
            if val.startswith(("dpapi:", "plain:")):
                c[svc][field] = unprotect(val)
        return c

    def save_push_cfg(self, c):
        for svc, field in PUSH_SECRETS:
            if c.get(svc, {}).get(field):
                c[svc][field] = protect(c[svc][field])
        self.cfg["push"] = c
        self.save()
        self.log("App", APP, "Push-Nachrichten " + ("eingeschaltet" if c.get("enabled") else "ausgeschaltet"))

    def push_dialog(self):
        d = PushDialog(self, self, NOTIFY_KINDS)
        self.wait_window(d)

    def web_click_url(self):
        if not (self.cfg.get("web_enabled") or HEADLESS):
            return ""
        urls = self.web_urls()
        return urls[0] if urls else ""

    def push(self, kind, title, text="", important=False, image_fn=None):
        if self.cfg.get("satellite"):
            return  # der Heimserver schickt die Nachrichten – sonst kämen sie doppelt
        c = self.cfg.get("push", {})
        if not c.get("enabled") or not c.get("kinds", {}).get(kind, kind not in NOTIFY_DEFAULT_OFF):
            return
        self.pusher.send(title, text, important=important, click=self.web_click_url(), image_fn=image_fn)

    def _on_push_result(self, payload):
        ok, msg = payload
        if not ok:
            self.log("App", "Push", f"⚠ nicht gesendet: {msg}")
            log.warning("Push fehlgeschlagen: %s", msg)

    # ---------------- Wetter ----------------
    def weather_cfg(self):
        c = dict(DEFAULT_CFG["weather"])
        c.update(self.cfg.get("weather") or {})
        return c

    def _weather_tick(self):
        try:
            c = self.weather_cfg()
            age = time.time() - (self.weather or {}).get("fetched", 0)
            retry_ok = time.time() - self._weather_try > (120 if self._weather_err else 0)
            if c.get("enabled") and not self._weather_busy and age > 15 * 60 and retry_ok:
                self.refresh_weather()
            self._wxa_tick()
        except Exception:
            log.exception("Wetter-Takt")
        if not self.stop.is_set():
            self.after(30000, self._weather_tick)

    def refresh_weather(self, relocate=False):
        c = self.weather_cfg()
        if self._weather_busy or not c.get("enabled"):
            return
        self._weather_busy, self._weather_try = True, time.time()

        def run():
            try:
                loc = None
                if c.get("lat") is None or (relocate and c.get("mode") == "auto"):
                    if c.get("mode") != "auto":
                        raise wx.WeatherError("Kein Ort gewählt – bitte in den Wetter-Einstellungen festlegen")
                    loc = wx.locate_auto()
                    c.update(loc)
                data = wx.fetch(c["lat"], c["lon"])
                self.results.put(("weather", (data, loc)))
            except Exception as e:  # noqa: BLE001
                self.results.put(("weather_err", str(e)))
        threading.Thread(target=run, daemon=True, name="Wetter").start()

    def _on_weather_err(self, msg):
        self._weather_busy = False
        if msg != self._weather_err:
            log.warning("Wetter: %s", msg)
        self._weather_err = msg
        if not self.weather:
            self.dash.put("Wetter", "weather:now", {"type": "weather", "status": "⚠ " + msg[:60]}, cols=2)
            self.dash.commit()
        if self._weather_dialog:
            try:
                self._weather_dialog.render()
            except Exception:
                pass

    def _on_weather(self, payload):
        data, loc = payload
        self._weather_busy, self._weather_err = False, ""
        if loc:
            c = self.weather_cfg()
            c.update(loc)
            self.cfg["weather"] = c
            self.save()
            self.log("App", "Wetter", f"Standort ermittelt: {loc['name']}")
        first = self.weather is None
        self.weather = data
        cur = data["current"]
        self.store.add_series("weather:temp", cur["temp"])
        self.store.name_series("weather:temp", "Außentemperatur")
        self._weather_tile()
        if first:
            log.info("Wetter für %s geladen", self.weather_cfg().get("name"))
        self._engine_update()
        if self._weather_dialog:
            try:
                self._weather_dialog.render()
            except Exception:
                pass

    # ---------------- Unwetterwarnungen (DWD) ----------------
    def _wxa_tick(self, force=False):
        c = self.weather_cfg()
        if not (c.get("enabled") and c.get("alerts", True)) or c.get("lat") is None or self._wxa_busy:
            if self.wx_alerts and not (c.get("enabled") and c.get("alerts", True)):
                self.wx_alerts = []
                self._weather_tile()
            return
        wait = 600 if not self._wxa_err else 300
        if not force and time.time() - self._wxa_t < wait:
            return
        self._wxa_busy, self._wxa_t = True, time.time()
        lat, lon = c["lat"], c["lon"]

        def run():
            import wxalerts
            try:
                self.results.put(("wxalerts", wxalerts.fetch(lat, lon)))
            except Exception as e:  # noqa: BLE001
                self.results.put(("wxalerts_err", str(e)))
        threading.Thread(target=run, daemon=True, name="DWD-Warnungen").start()

    def _on_wxalerts_err(self, msg):
        self._wxa_busy = False
        if msg != self._wxa_err:
            log.warning("Unwetterwarnungen: %s", msg)
        self._wxa_err = msg

    def _on_wxalerts(self, alerts):
        import wxalerts
        self._wxa_busy, self._wxa_err = False, ""
        now = time.time()
        sent = {k: v for k, v in (self.cfg.get("wx_alerts_sent") or {}).items() if (v.get("until") or now + 1) > now}
        c = self.weather_cfg()
        push_level = int(c.get("alert_push", 2))
        changed = False
        for a in alerts:
            prev = sent.get(a["event"])
            if prev and prev.get("level", 0) >= a["level"]:
                continue  # schon gemeldet (oder nur verlängert)
            worse = bool(prev)
            sent[a["event"]] = {"level": a["level"], "until": a.get("expires") or now + 12 * 3600}
            changed = True
            when = wxalerts.when(a, now)
            self.log("App", "Wetter", f"{a['icon']} {a['headline']}" + (f" ({when})" if when else ""))
            if self.cfg.get("satellite") or a["level"] < push_level:
                continue
            title = f"{a['icon']} {a['headline']}" + (" – verschärft" if worse else "")
            text = " · ".join(x for x in (a["level_name"], when, a.get("instr", "")[:160]) if x)
            self.notify("wxalert", title, text, warn=a["level"] >= 3)
        if changed or len(sent) != len(self.cfg.get("wx_alerts_sent") or {}):
            self.cfg["wx_alerts_sent"] = sent
            self.save()
        self.wx_alerts = alerts
        self._weather_tile()

    def web_wxalerts(self):
        import wxalerts
        now = time.time()
        return [{"event": a["event"], "level": a["level"], "level_name": a["level_name"], "color": a["color"],
                 "icon": a["icon"], "headline": a["headline"], "when": wxalerts.when(a, now), "desc": a["desc"],
                 "instr": a["instr"], "pre": a.get("pre", False)}
                for a in self.wx_alerts if not a.get("expires") or a["expires"] > now]

    def weather_view(self):
        """Aufbereitet für Kachel und Handy (Haupt-Thread)."""
        w = self.weather
        if not w:
            return None
        cur = w["current"]
        txt, emoji = wx.describe(cur["code"], cur["day"])
        sub = []
        if cur.get("feels") is not None:
            sub.append(f"gefühlt {round(cur['feels'])}°")
        if cur.get("wind") is not None:
            sub.append(f"Wind {round(cur['wind'])} km/h")
        if cur.get("hum") is not None:
            sub.append(f"{round(cur['hum'])} % Luftf.")
        warn = ""
        if cur.get("raining"):
            warn = "☂ Es regnet gerade"
        elif cur.get("rain_soon"):
            warn = "☂ Regen in der nächsten Stunde"
        elif (cur.get("gusts") or 0) >= 60:
            warn = f"Sturmböen bis {round(cur['gusts'])} km/h"
        days = []
        for i, d in enumerate(w["daily"][:5]):
            lt = time.localtime(d["ts"])
            dt_, de = wx.describe(d["code"])
            days.append({"label": "Heute" if i == 0 else ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][lt.tm_wday],
                         "code": d["code"], "max": d["max"], "min": d["min"], "prob": d.get("prob"),
                         "text": dt_, "emoji": de, "sunrise": d["sunrise"], "sunset": d["sunset"]})
        al = self.web_wxalerts()
        if al:
            a = al[0]
            warn = f"{a['icon']} {a['headline'].replace('Amtliche ', '')}" + (f" · {a['when']}" if a["when"] else "")
        return {"current": cur, "text": txt, "emoji": emoji, "place": self.weather_cfg().get("name") or "",
                "sub": " · ".join(sub), "warn": warn, "warn_color": al[0]["color"] if al else "",
                "alerts": al, "days": days,
                "updated": time.strftime("%H:%M", time.localtime(w["fetched"]))}

    def _weather_tile(self):
        v = self.weather_view()
        if v:
            self.dash.put("Wetter", "weather:now", dict(v, type="weather"), cols=2)
            self.dash.commit()

    def weather_dialog(self):
        from pages import WeatherDialog
        d = WeatherDialog(self, self)
        self._weather_dialog = d
        self.wait_window(d)
        self._weather_dialog = None

    def save_weather_cfg(self, c):
        old = self.weather_cfg()
        self.cfg["weather"] = c
        self.save()
        if (c.get("lat"), c.get("lon")) != (old.get("lat"), old.get("lon")):
            self.wx_alerts = []  # anderer Ort – alte Warnungen gelten nicht
        self._wxa_t = 0
        if not c.get("enabled"):
            self.weather = None
            self.dash.remove_missing("Wetter", "weather:", [])
            self.dash.commit()
            return
        if (c.get("lat"), c.get("lon"), c.get("mode")) != (old.get("lat"), old.get("lon"), old.get("mode")) \
                or not self.weather:
            self.weather = None
            self._weather_busy = False
            self.refresh_weather(relocate=c.get("mode") == "auto" and c.get("lat") is None)

    # ---------------- Updates ----------------
    def update_token(self):
        if not self.cfg.get("update_token"):
            import secrets
            self.cfg["update_token"] = secrets.token_hex(32)
            self.save()
        return self.cfg["update_token"]

    @property
    def backup_dir(self):
        return os.path.join(_data_dir(), "updates")

    def check_downloads(self, interactive=False):
        """Beim Start: liegt eine neuere Hauskern*.zip im Download-Ordner?"""
        if not (self.cfg.get("update_scan", True) or interactive):
            return None
        try:
            found = updater.find_download(VERSION, edition=edition.EDITION)
        except Exception:
            log.exception("Update-Suche")
            return None
        if not found:
            if interactive:
                messagebox.showinfo(APP, f"Keine neuere Version im Download-Ordner gefunden.\n\nInstalliert: {VERSION}",
                                    parent=self._update_parent())
            return None
        path, ver = found
        if not interactive and self.cfg.get("update_declined") == ver:
            return found
        if messagebox.askyesno(APP, f"Neue Version {ver} gefunden 🎉\n\n{path}\n\nJetzt installieren? Hauskern startet "
                                    f"danach kurz neu. Deine Einstellungen und Daten bleiben erhalten – die jetzige "
                                    f"Version {VERSION} wird vorher gesichert.", parent=self._update_parent()):
            self.install_update_file(path)
        else:
            self.cfg["update_declined"] = ver
            self.save()
        return found

    def _update_parent(self):
        return getattr(self, "_upd_dialog", None) or self

    def install_update_file(self, path, allow_older=False):
        # eigene Sicherungen (Datenordner/updates) sind vertrauenswürdig – alles andere muss signiert sein
        own = os.path.normcase(os.path.realpath(os.path.dirname(path))) == os.path.normcase(os.path.realpath(self.backup_dir))
        try:
            with open(path, "rb") as f:
                data = f.read()
            ver, saved, n = updater.apply(data, HERE, self.backup_dir, VERSION, allow_older=allow_older,
                                          require_sig=not own)
        except (OSError, updater.UpdateError) as e:
            log.warning("Update fehlgeschlagen: %s", e)
            messagebox.showwarning(APP, f"Update nicht installiert:\n{e}", parent=self._update_parent())
            return False
        self.log("App", APP, f"Version {ver} installiert (vorher {VERSION}, Sicherung: {os.path.basename(saved)})")
        log.info("Update auf %s installiert (%s Dateien), Neustart", ver, n)
        self.cfg["update_declined"] = ""
        self.save()
        messagebox.showinfo(APP, f"Version {ver} ist installiert ✅\n\nHauskern startet jetzt neu."
                                 + ("\nDer Raspberry Pi bekommt das Update gleich danach automatisch." if self.cfg.get("pi_host")
                                    else ""), parent=self._update_parent())
        self.restart()
        return True

    def restart(self):
        import subprocess
        args = [sys.executable, os.path.join(HERE, "Hauskern.pyw"), "--after-update"]
        if self.state() == "withdrawn":
            args.append("--tray")
        try:
            subprocess.Popen(args, cwd=HERE, creationflags=0x00000008 if os.name == "nt" else 0)  # DETACHED_PROCESS
        except OSError as e:
            messagebox.showwarning(APP, f"Bitte Hauskern selbst neu starten ({e}).")
        self.after(300, self._quit)

    def update_dialog(self):
        from pages import UpdateDialog
        d = UpdateDialog(self, self)
        self._upd_dialog = d
        self.wait_window(d)
        self._upd_dialog = None

    # --- Raspberry Pi automatisch auf denselben Stand bringen ---
    def _pi_request(self, path, data=b"{}", headers=None, timeout=10):
        import urllib.request
        host, port = self.cfg.get("pi_host"), int(self.cfg.get("pi_port", 8770))
        req = urllib.request.Request(f"http://{host}:{port}{path}", data=data,
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def pi_sync(self, force=False, done=None):
        """Läuft im Hintergrund: Ist der Heimserver älter als dieser PC? Dann das Update hinüberschicken."""
        if not self.cfg.get("pi_host"):
            return

        def run():
            try:
                info = self._pi_request("/api/info", timeout=6)
                pv = info.get("version", "?")
                if not force and updater.vtuple(pv) >= updater.vtuple(VERSION):
                    res = (True, f"Heimserver ist aktuell ({pv})", pv)
                else:
                    data = updater.build_package(HERE)
                    r = self._pi_request("/api/update", data, {"Content-Type": "application/zip",
                                                               "X-Hauskern-Signature": updater.sign(self.update_token(), data)},
                                         timeout=60)
                    res = (bool(r.get("ok")), r.get("msg") or r.get("error") or "?", pv)
            except Exception as e:  # noqa: BLE001
                err = getattr(e, "read", None)
                msg = str(e)
                if err:
                    try:
                        msg = json.loads(e.read()).get("error", msg)
                    except Exception:  # noqa: BLE001
                        pass
                res = (False, f"Heimserver nicht erreichbar oder Update abgelehnt: {msg}", "?")
            self.results.put(("pi_sync", (res, done)))
        threading.Thread(target=run, daemon=True, name="Pi-Update").start()

    def _on_pi_sync(self, payload):
        (ok, msg, pv), done = payload
        if not ok or "installiert" in msg:
            self.log("App", "Heimserver", msg)
        (log.info if ok else log.warning)("Pi-Abgleich: %s", msg)
        if done:
            done(ok, msg, pv)

    def receive_update(self, data, sig):
        """Webserver-Thread: Update vom PC annehmen (nur mit gültiger Signatur)."""
        if not updater.verify(self.cfg.get("update_token", ""), data, sig):
            log.warning("Update mit ungültiger Signatur abgelehnt")
            return False, "Signatur ungültig – bitte am PC einmal „Einstellungen an den Heimserver senden“"
        try:   # durch den gemeinsamen Schlüssel von PC und Heimserver abgesichert (s. o.)
            ver, saved, n = updater.apply(data, HERE, self.backup_dir, VERSION, require_sig=False)
        except updater.UpdateError as e:
            return False, str(e)
        self.log("App", APP, f"Update {ver} vom PC installiert (vorher {VERSION})")
        log.info("Update %s vom PC installiert, Neustart", ver)
        if HEADLESS:
            threading.Timer(1.5, lambda: os._exit(0)).start()  # systemd startet den Dienst neu
            return True, f"Version {ver} installiert – Heimserver startet neu"
        return True, f"Version {ver} installiert – bitte Hauskern dort neu starten"

    # ---------------- Druckkosten ----------------
    def cost_settings(self):
        c = dict(pricing.DEFAULT_SETTINGS)
        c.update(self.cfg.get("print_cost") or {})
        return c

    def filaments(self):
        from pages import clean_color
        return [clean_color(dict(f)) for f in (self.cfg.get("filaments") or pricing.DEFAULT_FILAMENTS)]

    def save_cost_settings(self, settings, filaments):
        self.cfg["print_cost"] = settings
        self.cfg["filaments"] = filaments
        self.save()
        self.log("Drucker", "Druckkosten", "Einstellungen gespeichert")

    def job_from_print(self, row):
        """Ein fertiger Druck (Zeile aus store.prints_between/print_row) als Kalkulations-Auftrag."""
        printer, file, a, b, swh, ewh, res, _ser, grams, fils, _pid = row
        items = []
        try:
            items = json.loads(fils) if fils else []
        except ValueError:
            items = []
        if not items and grams:
            items = [{"type": "", "grams": grams}]
        kwh = (ewh - swh) / 1000 if swh is not None and ewh is not None and ewh >= swh else None
        return {"name": file or "Druck", "items": items, "hours": max(0, (b or a) - a) / 3600, "kwh": kwh, "qty": 1,
                "labor_min": 0, "extras": 0}

    def print_cost(self, row):
        """Selbstkosten (Material, Strom, Maschine, Risiko) eines Drucks – ohne Arbeitszeit/Verpackung/Gewinn."""
        job = self.job_from_print(row)
        s = self.cost_settings()
        s.update(hourly_rate=0, packaging=0, labor_min=0)
        return job, pricing.calculate(job, s, self.filaments(), self.cfg.get("price_ct", 35.0))

    def calculate_job(self, job):
        return pricing.calculate(job, self.cost_settings(), self.filaments(), self.cfg.get("price_ct", 35.0))

    def _ams_update(self, serial, summ):
        units = summ.get("ams_units") or []
        keys = []
        for u in units:
            k = f"ams:{serial}:{u['id']}"
            keys.append(k)
            self.dash.put("3D-Druck", k, {"type": "ams", "unit": u, "printer": summ["name"],
                                          "hum": bambu.humidity_text(u)}, cols=2)
            for t in u["trays"]:
                self._ams_low_check(serial, summ, u, t)
        self.dash.remove_missing("3D-Druck", f"ams:{serial}:", keys)

    def ams_low_pct(self):
        try:
            return max(0, min(50, int(float(self.cfg.get("ams_low_pct", 10)))))
        except (TypeError, ValueError):
            return 10

    def _ams_low_check(self, serial, summ, u, t):
        """„Filament fast leer“ – genau EINMAL pro Rolle. Der Merker steht in den Einstellungen (übersteht Neustarts)
        und wird nur gelöscht, wenn wirklich eine andere/aufgefüllte Rolle drin ist – nicht bei kurzzeitig
        fehlenden Werten vom Drucker (die gab es alle paar Minuten und lösten die Meldung immer wieder aus)."""
        lim = self.ams_low_pct()
        warned = self.cfg.setdefault("ams_warned", {})
        key = f"{serial}|{u['id']}|{t['slot']}"
        ident = t.get("uuid") or f"{t.get('type')}|{t.get('color')}"
        old = warned.get(key)
        if t["empty"] or not t.get("type"):
            return  # leeres Fach / Daten fehlen: Merker bleibt, bis eine andere Rolle erkannt wird
        if old and old != ident and (t.get("uuid") or old.count("|") == 1):
            warned.pop(key, None)  # andere Rolle eingelegt
            old = None
        rem = t["remain"]
        if rem is None:
            return  # Wert gerade unbekannt → nichts ändern
        if old and rem >= lim + 10:
            warned.pop(key, None)  # aufgefüllt / neue Rolle mit gleicher Farbe
            self.save()
            return
        if lim and rem <= lim and not old:
            warned[key] = ident
            self.save()
            self.notify("printer", f"{summ['name']}: Filament fast leer 🧵",
                        f"{u['name']} · Fach {t['slot']}: {t['type']} {t['brand']} – noch {rem} %"
                        + (f" (ca. {t['grams']} g)" if t["grams"] is not None else "")
                        + " – diese Meldung kommt für diese Rolle nur einmal.")

    def ams_trays(self):
        out = []
        for pr in self.printers.values():
            for u in pr.summary().get("ams_units", []):
                for t in u["trays"]:
                    if not t["empty"]:
                        out.append(dict(t, unit=u["name"]))
        return out

    def ams_check(self, serial, filaments):
        """Reicht das Filament im AMS für den geplanten Druck? Gibt Warntexte zurück."""
        pr = self.printers.get(serial)
        if not pr:
            return []
        trays = [(u, t) for u in pr.summary().get("ams_units", []) for t in u["trays"] if not t["empty"]]
        warn = []
        for f in filaments:
            need = f.get("grams") or 0
            col = (f.get("color") or "").lower()[:7]
            match = [(u, t) for u, t in trays if t["type"].upper() == (f.get("type") or "").upper()
                     and (not col or (t["color"] or "") == col)]
            if len(match) == 1:
                u, t = match[0]
                if t["grams"] is None:
                    import filament as FL
                    sp = next((s for s in self.cfg.get("spools", [])
                               if s.get("slot") == FL.slot_key(serial, u["id"], t["slot"])), None)
                    if sp:
                        t = dict(t, grams=round(float(sp.get("left") or 0)))
                if t["grams"] is not None and t["grams"] < need:
                    warn.append(f"{f.get('type')} ({u['name']} · Fach {t['slot']}): geplant {round(need)} g, "
                                f"im Fach noch ca. {t['grams']} g")
            elif not match and trays:
                warn.append(f"{f.get('type')} {col} nicht im AMS gefunden")
        return warn

    # --- Druckkosten für die Handy-Oberfläche (Server-Thread) ---
    def web_calc_info(self):
        s = self.cost_settings()
        prints = []
        for row in self.store.prints_between(time.time() - 60 * 86400, time.time() + 60)[:25]:
            job = self.job_from_print(row)
            g = sum(float(i.get("grams") or 0) for i in job["items"])
            prints.append({"label": f"{time.strftime('%d.%m. %H:%M', time.localtime(row[2]))} · {row[1] or 'Druck'}"
                                    + (f" · {round(g)} g" if g else ""), "job": job})
        return {"filaments": [{"id": f["id"], "name": f["name"], "type": f.get("type", ""), "price_kg": f["price_kg"]}
                              for f in self.filaments()],
                "defaults": {"margin_pct": s["margin_pct"], "labor_min": s["labor_min"]},
                "small_business": bool(s["small_business"]), "vat_pct": s["vat_pct"],
                "saved": [{"id": i, "date": time.strftime("%d.%m.%Y", time.localtime(ts)), "name": n,
                           "price": d.get("result", {}).get("price", 0), "job": d.get("job", {})}
                          for i, ts, n, d in self.store.calcs(50)],
                "prints": prints}

    def web_calc(self, job):
        r = self.calculate_job(job)
        return {"result": {k: v for k, v in r.items()}, "text": pricing.as_text(job, r)}

    def web_calc_save(self, job):
        r = self.calculate_job(job)
        name = (job.get("name") or "").strip() or "Kalkulation vom Handy"
        job["name"] = name
        self.store.save_calc(name, {"job": job, "result": {k: v for k, v in r.items() if k != "material_lines"}})
        return True

    def web_calc_delete(self, cid):
        self.store.delete_calc(int(cid))
        return True

    def web_calc_3mf(self, data, filename=""):
        u = bambu_files.parse_3mf(data, "all")
        fil = self.filaments()
        items = []
        for f in u["filaments"]:
            m = pricing.find_filament(fil, "", f["type"], f["color"])
            items.append({"filament": m["id"], "grams": f["grams"], "type": f["type"], "color": f["color"]})
        name = re.sub(r"(\.gcode)?\.3mf$", "", os.path.basename(filename or ""), flags=re.I)
        return {"name": name, "items": items, "hours": u["seconds"] / 3600, "qty": 1, "plates": u.get("plates", 1)}

    def _fetch_usage(self, serial, pid, raw):
        """Holt (im Hintergrund) den geplanten Filamentverbrauch vom Drucker."""
        pr = self.printers.get(serial)
        if not pr or not pr.code:
            return
        gf, sub = raw.get("gcode_file", ""), raw.get("subtask_name", "")

        def run():
            try:
                u = bambu_files.job_usage(pr.ip, pr.code, gf, sub)
                self.results.put(("print_usage", (serial, pid, u, "")))
            except Exception as e:  # noqa: BLE001
                self.results.put(("print_usage", (serial, pid, None, str(e))))
        threading.Thread(target=run, daemon=True, name="Filamentverbrauch").start()

    def _on_print_usage(self, payload):
        serial, pid, u, err = payload
        name = self.printers[serial].name if serial in self.printers else "Drucker"
        if not u:
            log.info("Filamentverbrauch nicht lesbar: %s", err)
            self.log("Drucker", name, f"Filamentverbrauch nicht lesbar – bitte auf der Seite Druckkosten eintragen ({err})")
            return
        self.store.set_print_usage(pid, u["grams"], json.dumps(u["filaments"]), u["seconds"], u.get("file", ""))
        self.log("Drucker", name, f"Geplanter Verbrauch: {fnum(u['grams'], 1)} g Filament")
        warn = [w for w in self.ams_check(serial, u["filaments"]) if "geplant" in w]
        if warn:
            self.notify("printer", f"{name}: Filament reicht evtl. nicht ⚠️", " · ".join(warn), True)
        if self.page == "costs":
            self.tab_costs.refresh()

    # ---------------- Gerätesuche ----------------
    def add_ping_target(self, name, host):
        if not any(t["host"] == host for t in self.cfg.get("ping_targets", [])):
            self.cfg.setdefault("ping_targets", []).append({"name": name.strip(), "host": host.strip()})
            self.save()
            self._render_ping()
            self.jobs.put(("refresh",))

    def known_ips(self):
        known = {}
        for c in self.cfg.get("cameras", []):
            if c.get("host"):
                known[c["host"]] = f"Kamera „{c.get('name')}“"
        for p in self.printers.values():
            known[p.ip] = f"3D-Drucker „{p.name}“"
        for b in self.cfg.get("boxes", []):
            known[b.get("host", "")] = f"FRITZ!Box „{b.get('name')}“"
        for t in self.cfg.get("ping_targets", []):
            known.setdefault(t["host"], f"im Ping-Monitor („{t.get('name')}“)")
        for c in self.cfg.get("integrations", []):
            if c.get("host"):
                known[c["host"]] = f"{c.get('name') or c['host']} (eingebunden)"
        host = self.cfg.get("host", "")
        if re.match(r"\d+\.\d+\.\d+\.\d+$", host):
            known[host] = "Haupt-FRITZ!Box"
        for h in self.data["hosts"]:
            if (h["name"] or "").lower() in ("fritz.box", "fritz.repeater") and h.get("ip"):
                known[h["ip"]] = "FRITZ!Box"
            elif h.get("ip") and h.get("mac"):
                use = self.device_usage(h["mac"])
                if use:
                    known.setdefault(h["ip"], ", ".join(use))
        return known

    def run_discovery(self):
        if self.disc_running:
            return
        import discover
        hosts, known = [dict(h) for h in self.data["hosts"]], self.known_ips()
        self.disc_running, self.disc_progress = True, "startet …"
        log.info("Gerätesuche gestartet")

        def work():
            try:
                res = discover.scan(hosts, known, progress=lambda t: self.results.put(("disc_progress", t)))
            except Exception as e:  # noqa: BLE001
                log.exception("Gerätesuche")
                res = None
                self.results.put(("disc_progress", f"⚠ {e}"))
            self.results.put(("disc", res))
        threading.Thread(target=work, daemon=True, name="Gerätesuche").start()

    def _on_disc_progress(self, t):
        self.disc_progress = t
        if self.page == "discover":
            self.tab_discover.refresh()

    def _on_disc(self, res):
        self.disc_running = False
        if res is not None:
            new_lokal = {d["ip"] for d in res if d["status"] in ("lokal", "cloud")} - \
                {d["ip"] for d in self.disc_results if d["status"] in ("lokal", "cloud")}
            self.disc_results, self.disc_time = res, time.time()
            n = sum(1 for d in res if d["status"] in ("lokal", "cloud", "erkannt"))
            self.log("Netzwerk", "Gerätesuche", f"{len(res)} Geräte, davon {n} erkannt")
            if new_lokal and len(new_lokal) < len(res):
                log.info("Gerätesuche: neu erkannt %s", sorted(new_lokal))
            try:
                with open(os.path.join(_data_dir(), "geraetesuche.json"), "w", encoding="utf-8") as f:
                    json.dump({"time": self.disc_time, "results": res}, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        if self.page == "discover":
            self.tab_discover.refresh()

    def _load_discovery(self):
        try:
            with open(os.path.join(_data_dir(), "geraetesuche.json"), encoding="utf-8") as f:
                d = json.load(f)
            self.disc_results, self.disc_time = d.get("results", []), d.get("time", 0)
        except (OSError, ValueError):
            pass

    # ---------------- Wake-on-LAN ----------------
    def wake(self, mac, name=""):
        """PC & Co. aufwecken: über die FRITZ!Box und zusätzlich direkt per Magic Packet."""
        mac = mac.upper()
        ok_direct = False
        try:
            magic_packet(mac)
            ok_direct = True
        except (OSError, ValueError) as e:
            log.info("Magic Packet direkt nicht möglich: %s", e)
        if self.client and self.is_fritz():
            self.jobs.put(("action", lambda c: c.wake(mac), "full"))
        elif not ok_direct:
            raise RuntimeError("Weder FRITZ!Box noch Netzwerk erreichbar")
        self.log("Netzwerk", name or mac, "Aufwecken gesendet (Wake-on-LAN)")
        return f"{name or mac} wird geweckt"

    def add_wol(self, mac, name):
        mac = mac.upper()
        lst = [w for w in self.cfg.get("wol", []) if w["mac"].upper() != mac] + [{"mac": mac, "name": name}]
        self.cfg["wol"] = lst
        self.save()
        self._wol_tiles()

    def remove_wol(self, mac):
        self.cfg["wol"] = [w for w in self.cfg.get("wol", []) if w["mac"].upper() != mac.upper()]
        self.save()
        self._wol_tiles()

    def _wol_tiles(self):
        hosts = {(h["mac"] or "").upper(): h for h in self.data["hosts"]}
        keys = []
        for w in self.cfg.get("wol", []):
            k = "wol:" + w["mac"].upper()
            keys.append(k)
            h = hosts.get(w["mac"].upper())
            t = self.dash.tiles.get(k)
            until = t.model.get("busy_until", 0) if t else 0
            online = bool(h and h["active"])
            self.dash.put("Netzwerk", k, {"type": "wol", "name": w["name"], "mac": w["mac"], "online": online,
                                          "busy": until > time.time() and not online, "busy_until": until,
                                          "sub": ((h or {}).get("ip") or w["mac"]) + " · Wake-on-LAN"})
        self.dash.remove_missing("Netzwerk", "wol:", keys)
        self.dash.commit()

    def _host_add(self):
        sel = self._selected(self.t_hosts)
        h = next((x for x in self.data["hosts"] if (x["mac"] or x["ip"] or x["name"]) == sel), None) if sel else None
        if not h or not h.get("mac"):
            messagebox.showinfo(APP, "Bitte zuerst ein Gerät mit MAC-Adresse in der Liste auswählen.")
            return
        self.add_device_dialog(h["mac"], h.get("ip", ""))

    def _host_wake(self):
        sel = self._selected(self.t_hosts)
        h = next((x for x in self.data["hosts"] if (x["mac"] or x["ip"] or x["name"]) == sel), None) if sel else None
        if not h or not h.get("mac"):
            messagebox.showinfo(APP, "Bitte zuerst ein Gerät mit MAC-Adresse in der Liste auswählen.")
            return
        name = h["name"] or h["mac"]
        known = any(w["mac"].upper() == h["mac"].upper() for w in self.cfg.get("wol", []))
        if not known and messagebox.askyesno(
                APP, f"„{name}“ als Aufweck-Kachel auf „Zuhause“ und in der Handy-Oberfläche anlegen?\n\n"
                     "Tipp: Im BIOS/UEFI muss „Wake on LAN“ aktiviert sein, unter Windows zusätzlich beim "
                     "Netzwerkadapter (Energieverwaltung → „Gerät kann den Computer aus dem Ruhezustand aktivieren“)."):
            self.add_wol(h["mac"], name)
        try:
            msg = self.wake(h["mac"], name)
            self.l_hostcount.configure(text="⏻ " + msg)
        except RuntimeError as e:
            messagebox.showwarning(APP, str(e))

    # ---------------- Eigene Geräte (aus WLAN/LAN hinzugefügt) ----------------
    def _disc_lookup(self):
        by_mac = {(d.get("mac") or "").upper(): d for d in self.disc_results if d.get("mac")}
        by_ip = {d["ip"]: d for d in self.disc_results if d.get("ip")}
        return by_mac, by_ip

    def describe_hosts(self):
        """Alle Geräte der FRITZ!Box (+ Treffer der Gerätesuche), eingeordnet."""
        import discover
        by_mac, by_ip = self._disc_lookup()
        out, seen = [], set()
        for h in self.data.get("hosts", []):
            mac = (h.get("mac") or "").upper()
            if not mac or mac in seen:
                continue
            seen.add(mac)
            disc = by_mac.get(mac) or by_ip.get(h.get("ip") or "")
            d = {"mac": mac, "ip": h.get("ip", ""), "name": h.get("name") or (disc or {}).get("name") or mac,
                 "active": bool(h.get("active")), "iface": h.get("iface", ""),
                 "services": (disc or {}).get("services", []), "model": (disc or {}).get("model", "")}
            d.update(discover.classify(d["name"], mac, disc))
            out.append(d)
        for disc in self.disc_results:  # nur per mDNS/UPnP gefunden (ohne Eintrag in der FRITZ!Box)
            mac = (disc.get("mac") or "").upper()
            if mac and mac not in seen:
                seen.add(mac)
                d = {"mac": mac, "ip": disc.get("ip", ""), "name": disc.get("name") or mac, "active": True, "iface": "",
                     "services": disc.get("services", []), "model": disc.get("model", "")}
                d.update(discover.classify(d["name"], mac, disc))
                out.append(d)
        return out

    def describe_host(self, mac="", ip=""):
        mac = (mac or "").upper()
        allh = self.describe_hosts()
        d = next((x for x in allh if mac and x["mac"] == mac), None) or \
            next((x for x in allh if ip and x["ip"] == ip), None)
        if not d and (mac or ip):
            import discover
            by_mac, by_ip = self._disc_lookup()
            disc = by_mac.get(mac) or by_ip.get(ip) or {}
            d = {"mac": mac, "ip": ip, "name": disc.get("name") or ip or mac, "active": True, "iface": "",
                 "services": disc.get("services", []), "model": disc.get("model", "")}
            d.update(discover.classify(d["name"], mac, disc))
        return d, allh

    def device_usage(self, mac):
        """Wo ist dieses Gerät in Hauskern schon eingebunden?"""
        mac = (mac or "").upper()
        use = [f"Person „{p['name']}“" if p.get("kind") != "item" else f"Gegenstand „{p['name']}“"
               for p in self.cfg.get("persons", []) if mac in {m.upper() for m in p.get("macs", [])}]
        use += [f"Gerät „{d['name']}“" for d in self.cfg.get("devices", [])
                if mac == d.get("mac", "").upper() or mac in {m.upper() for m in d.get("children", [])}]
        if any(w["mac"].upper() == mac for w in self.cfg.get("wol", [])):
            use.append("Aufweck-Kachel")
        return use

    def add_device_dialog(self, mac="", ip="", parent=None):
        from pages import AddDeviceDialog
        target, allh = self.describe_host(mac, ip)
        if not target:
            messagebox.showinfo(APP, "Gerät nicht gefunden – bitte die Geräteliste aktualisieren.", parent=parent or self)
            return
        d = AddDeviceDialog(parent or self, self, target, allh)
        self.wait_window(d)
        return d.result

    def edit_device(self, dev_id, parent=None):
        from pages import AddDeviceDialog
        cur = next((d for d in self.cfg.get("devices", []) if d["id"] == dev_id), None)
        if not cur:
            return
        target, allh = self.describe_host(cur.get("mac", ""))
        target = dict(target or {}, name=cur["name"], category=cur.get("category") or (target or {}).get("category", ""))
        d = AddDeviceDialog(parent or self, self, target, allh, cur=cur)
        self.wait_window(d)

    def save_device(self, r):
        self.cfg["devices"] = [d for d in self.cfg.get("devices", []) if d["id"] != r["id"]] + [r]
        self.save()
        n = len(r.get("children", [])) + len(r.get("radio", []))
        self.log("Netzwerk", r["name"], "als Gerät hinzugefügt" + (f" ({n} verbundene Geräte)" if n else ""))
        self._dev_tiles()

    def remove_device(self, dev_id):
        self.cfg["devices"] = [d for d in self.cfg.get("devices", []) if d["id"] != dev_id]
        self.save()
        self._dev_tiles()

    def device_states(self):
        hosts = {(h["mac"] or "").upper(): h for h in self.data.get("hosts", [])}
        out = []
        for d in self.cfg.get("devices", []):
            h = hosts.get(d.get("mac", "").upper())
            kids = []
            for m in d.get("children", []):
                kh = hosts.get(m.upper())
                kids.append({"mac": m, "name": (kh or {}).get("name") or d.get("child_names", {}).get(m, m),
                             "online": bool(kh and kh["active"])})
            radio = d.get("radio", [])
            parts = []
            if kids:
                parts.append(f"{sum(k['online'] for k in kids)}/{len(kids)} verbunden online")
            if radio:
                parts.append(f"{len(radio)} per Funk")
            out.append({"id": d["id"], "key": "dev:" + d["id"], "name": d["name"], "category": d.get("category", ""),
                        "online": bool(h and h["active"]), "known": h is not None, "ip": (h or {}).get("ip", ""),
                        "children": kids, "radio": radio,
                        "sub": " · ".join(parts) or d.get("category") or ((h or {}).get("ip") or "")})
        return out

    def _dev_tiles(self):
        keys = []
        last = self._dev_last
        for st in self.device_states():
            keys.append(st["key"])
            self.dash.put("Geräte", st["key"], dict(st, type="netdev"))
            prev = last.get(st["id"])
            if prev is not None and prev != st["online"]:
                self.log("Netzwerk", st["name"], "wieder erreichbar" if st["online"] else "nicht mehr erreichbar")
            last[st["id"]] = st["online"]
        self._dev_last = last
        self.dash.remove_missing("Geräte", "dev:", keys)
        self.dash.commit()

    # ---------------- Verlauf ----------------
    def _record_smart(self, devs):
        st = self.store
        for d in devs:
            if d["is_group"] or not d["present"]:
                continue
            ain, h = d["ain"], d["hkr"]
            if h:
                st.name_series("temp:" + ain, d["name"])
                if isinstance(h["ist"], (int, float)):
                    st.add_series("temp:" + ain, h["ist"])
                if isinstance(h["soll"], (int, float)):
                    st.add_series("soll:" + ain, h["soll"])
                st.add_series("hoff:" + ain, 1 if h["soll"] == "AUS" else 0)  # für den Heizungs-Check
                st.add_series("win:" + ain, 1 if h.get("window_open") else 0)
            elif d["temp"] is not None and d["power_w"] is None:  # reine Sensoren (Steckdosen messen nur ihre eigene Wärme)
                st.name_series("temp:" + ain, d["name"])
                st.add_series("temp:" + ain, d["temp"])
            if d["humidity"] is not None:
                st.name_series("hum:" + ain, d["name"])
                st.add_series("hum:" + ain, d["humidity"])

    def _minute_tick(self):
        try:
            self._guest_check_timer()
        except Exception:
            log.exception("Gast-WLAN-Zeit")
        try:
            if self.page == "history" and self.tab_history.offset == 0 and self.state() != "withdrawn":
                self.tab_history.refresh()
            if time.time() - getattr(self, "_last_prune", time.time()) > 6 * 3600:
                self._last_prune = time.time()
                threading.Thread(target=self.store.prune, daemon=True, name="Aufräumen").start()
            self._last_prune = getattr(self, "_last_prune", time.time())
        except Exception:
            log.exception("Minuten-Takt")
        for fn in (self._backup_tick, self._health_tick, self._pi_pull_tick, self._clips_tick):
            try:
                fn()
            except Exception:
                log.exception("Minuten-Takt (%s)", fn.__name__)
        if not self.stop.is_set():
            self.after(60000, self._minute_tick)

    def history_sources(self):
        """Was auf der Verlaufs-Seite auswählbar ist (Haupt-Thread)."""
        out = [{"label": "Internet", "kind": "net", "id": "net"}]
        if self.weather_cfg().get("enabled") or "weather:temp" in self.store.series_names():
            out.append({"label": "Wetter: Außentemperatur", "kind": "outside", "id": "outside"})
        pr = {s_: p.name for s_, p in self.printers.items()}
        for s_, nm in pr.items():
            out.append({"label": f"3D-Druck: {nm}", "kind": "prints", "serial": s_, "name": nm, "id": "prints:" + s_})
        names = self.store.series_names()
        devs = [d for d in self.data["smart"] if not d["is_group"]]
        hkr = [d for d in devs if d["hkr"]]
        seen = set()
        if len(hkr) > 1:
            out.append({"label": "Alle Heizkörper", "kind": "heat_all", "id": "heat_all",
                        "ains": [(d["ain"], d["name"]) for d in hkr]})
        for d in hkr:
            out.append({"label": f"Heizung: {d['name']}", "kind": "heat", "ain": d["ain"], "name": d["name"],
                        "id": "heat:" + d["ain"]})
            seen.add("temp:" + d["ain"])
        for d in devs:
            if not d["hkr"] and d["temp"] is not None and d["power_w"] is None:
                out.append({"label": f"Temperatur: {d['name']}", "kind": "temp", "ain": d["ain"], "name": d["name"],
                            "hum": d["humidity"] is not None, "id": "temp:" + d["ain"]})
                seen.add("temp:" + d["ain"])
        plugs = {d["ain"]: d["name"] for d in devs if d["power_w"] is not None}
        for ain, nm in self.store.devices().items():
            plugs.setdefault(ain, nm)
        for ain, nm in sorted(plugs.items(), key=lambda x: x[1].lower()):
            out.append({"label": f"Steckdose: {nm}", "kind": "plug", "ain": ain, "name": nm, "id": "plug:" + ain})
        for k, nm in sorted(names.items(), key=lambda x: x[1].lower()):  # Geräte, die gerade fehlen
            if k.startswith("temp:") and k not in seen:
                out.append({"label": f"Temperatur: {nm} (alt)", "kind": "temp", "ain": k[5:], "name": nm,
                            "hum": "hum:" + k[5:] in names, "id": "temp:" + k[5:]})
        targets = {t["host"]: t.get("name") or t["host"] for t in self.cfg.get("ping_targets", [])}
        for k, nm in names.items():
            if k.startswith("ping:"):
                targets.setdefault(k[5:], nm)
        for host, nm in targets.items():
            out.append({"label": f"Ping: {nm}", "kind": "ping", "host": host, "name": nm, "id": "ping:" + host})
        return out

    def history_data(self, src, t0, t1, bucket):
        """Daten für die Verlaufs-Seite (läuft im Hintergrund-Thread)."""
        from charts import PALETTE, fnum as cf
        st = self.store

        def pts(rows):
            return [(r[0], r[1]) for r in rows]

        def stats(rows, unit):
            vals = [r[1] for r in rows if r[1] is not None]
            if not vals:
                return [("Durchschnitt", "–"), ("Minimum", "–"), ("Maximum", "–")]
            return [("Durchschnitt", cf(sum(vals) / len(vals), unit)), ("Minimum", cf(min(r[2] for r in rows), unit)),
                    ("Maximum", cf(max(r[3] for r in rows), unit))]

        def outage_info(key):
            o = [x for x in st.outages(t0, t1, key)]
            total = sum(b - a for a, b in o)
            from pages import fmt_span
            return o, ("Ausfälle", f"{len(o)} · {fmt_span(total)}" if o else "keine 🎉")

        k = src["kind"]
        if k == "net":
            mb = lambda rows: [(r[0], r[1] / 1e6, r[2] / 1e6, r[3] / 1e6) for r in rows]  # noqa: E731
            dn, up = mb(st.series("net:down", t0, t1, bucket)), mb(st.series("net:up", t0, t1, bucket))
            o, info = outage_info("net:online")
            s = stats(dn, "Mbit/s")
            return {"title": "Genutzte Internet-Geschwindigkeit", "unit": "Mbit/s", "y_zero": True,
                    "series": [{"name": "↓ Download", "color": "#4f8ff7", "pts": pts(dn)},
                               {"name": "↑ Upload", "color": "#f0883e", "pts": pts(up)}],
                    "stats": [("Ø Download", s[0][1]), ("Spitze Download", s[2][1]),
                              ("Ø Upload", stats(up, "Mbit/s")[0][1]), info],
                    "lower": {"kind": "outages", "title": "Internet-Ausfälle", "rows": list(reversed(o))}}
        if k == "heat":
            ist, soll = st.series("temp:" + src["ain"], t0, t1, bucket), st.series("soll:" + src["ain"], t0, t1, bucket)
            s = stats(ist, "°C")
            sv = [r[1] for r in soll]
            return {"title": f"{src['name']} – Raumtemperatur und eingestellte Temperatur", "unit": "°C",
                    "series": [{"name": "Ist", "color": "#ff7849", "pts": pts(ist)},
                               {"name": "Soll", "color": "#8e96a3", "pts": pts(soll), "step": True, "dash": True}],
                    "stats": s + [("Ø Soll", cf(sum(sv) / len(sv), "°C") if sv else "–")]}
        if k == "outside":
            rows = st.series("weather:temp", t0, t1, bucket)
            return {"title": f"Außentemperatur ({self.weather_cfg().get('name') or 'Wetter'})", "unit": "°C",
                    "series": [{"name": "", "color": "#5ab0ff", "pts": pts(rows)}], "stats": stats(rows, "°C")}
        if k == "prints":
            rows = st.prints_between(t0, t1, src["serial"])
            span = t1 - t0
            cb = 86400 if span <= 100 * 86400 else 30 * 86400
            per, total_s, kwh, ok = {}, 0, 0.0, 0
            table = []
            cost_sum = 0.0
            for row in rows:
                printer, file, a, b, swh, ewh, res, _ser, g_, _fils, _pid = row
                dur = max(0, (b or a) - a)
                total_s += dur
                e = (ewh - swh) / 1000 if swh is not None and ewh is not None and ewh >= swh else None
                kwh += e or 0
                try:
                    _, rc = self.print_cost(row)
                    cost_sum += rc["cost"]
                    ctext = pricing.eur(rc["cost"]) + (f" · {fnum(g_, 0)} g" if g_ else " (ohne Material)")
                except Exception:  # noqa: BLE001
                    ctext = "–"
                ok += res == "fertig"
                lt = time.localtime(a)
                key = (time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)) if cb > 86400
                       else time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
                per[key] = per.get(key, 0) + dur / 3600
                from pages import fmt_span
                table.append([time.strftime("%d.%m.%Y %H:%M", lt), file or "–", fmt_span(dur), ctext,
                              {"fertig": "✅ fertig", "fehlgeschlagen": "❌ fehlgeschlagen"}.get(res, "⏹ " + (res or "?"))])
            n = len(rows)
            from pages import fmt_span
            return {"title": f"{src['name']} – Druckzeit pro " + ("Tag" if cb == 86400 else "Monat"), "unit": "Std",
                    "chart": "bars", "bucket": cb, "y_zero": True,
                    "series": [{"name": "", "color": "#f0883e", "pts": sorted(per.items())}],
                    "stats": [("Drucke", f"{n}" + (f" · {ok} fertig" if n else "")),
                              ("Erfolgsquote", f"{round(ok / n * 100)} %" if n else "–"),
                              ("Druckzeit", fmt_span(total_s) if total_s else "–"),
                              ("Selbstkosten gesamt", pricing.eur(cost_sum) if rows else "–")],
                    "lower": {"kind": "table", "title": "Drucke in diesem Zeitraum",
                              "cols": ["Start", "Datei", "Dauer", "Selbstkosten", "Ergebnis"], "rows": table[:200]}}
        if k == "heat_all":
            series, avgs = [], []
            for i, (ain, nm) in enumerate(src["ains"]):
                rows = st.series("temp:" + ain, t0, t1, bucket)
                series.append({"name": nm, "color": PALETTE[i % len(PALETTE)], "pts": pts(rows)})
                if rows:
                    avgs.append((sum(r[1] for r in rows) / len(rows), nm))
            out_rows = st.series("weather:temp", t0, t1, bucket)
            if out_rows:
                series.append({"name": "Draußen", "color": "#8e96a3", "pts": pts(out_rows), "dash": True})
            avgs.sort()
            return {"title": "Raumtemperaturen im Vergleich", "unit": "°C", "series": series,
                    "stats": ([("Wärmster Raum (Ø)", f"{avgs[-1][1]} · {cf(avgs[-1][0], '°C')}"),
                               ("Kältester Raum (Ø)", f"{avgs[0][1]} · {cf(avgs[0][0], '°C')}")] if avgs else [])}
        if k == "temp":
            rows = st.series("temp:" + src["ain"], t0, t1, bucket)
            r = {"title": f"{src['name']} – Temperatur", "unit": "°C",
                 "series": [{"name": "", "color": "#ff7849", "pts": pts(rows)}], "stats": stats(rows, "°C")}
            if src.get("hum"):
                hum = st.series("hum:" + src["ain"], t0, t1, bucket)
                r["lower"] = {"kind": "chart", "title": "Luftfeuchtigkeit", "unit": "%",
                              "series": [{"name": "", "color": "#5ab0ff", "pts": pts(hum)}]}
                r["stats"].append(("Ø Luftfeuchte", cf(sum(x[1] for x in hum) / len(hum), "%") if hum else "–"))
            return r
        if k == "plug":
            pw = st.power_series(src["ain"], t0, t1, bucket)
            span = t1 - t0
            cb = 3600 if span <= 2 * 86400 else 86400
            cons = st.consumption(src["ain"], t0, t1, cb)
            label_fmt = None
            if span > 100 * 86400:  # Jahr: pro Monat
                months = {}
                for ts, v in cons:
                    lt = time.localtime(ts)
                    m = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
                    months[m] = months.get(m, 0) + v
                cons, cb = sorted(months.items()), 30 * 86400
                label_fmt = lambda ts: time.strftime("%m/%Y", time.localtime(ts))  # noqa: E731
            total = sum(v for _, v in cons)
            s = stats(pw, "W")
            return {"title": f"{src['name']} – Leistung", "unit": "W", "y_zero": True,
                    "series": [{"name": "", "color": "#f6c15a", "pts": pts(pw)}],
                    "stats": [s[0], s[2], ("Verbrauch im Zeitraum", cf(total, "kWh")), ("Kosten", self.eur(total))],
                    "lower": {"kind": "chart", "chart": "bars", "unit": "kWh", "bucket": cb, "label_fmt": label_fmt,
                              "title": "Verbrauch pro " + ("Stunde" if cb == 3600 else "Tag" if cb == 86400 else "Monat"),
                              "series": [{"name": "", "color": "#3ecf8e", "pts": cons}]}}
        if k == "ping":
            rows = st.series("ping:" + src["host"], t0, t1, bucket)
            o, info = outage_info("pingok:" + src["host"])
            s = stats(rows, "ms")
            return {"title": f"{src['name']} – Antwortzeit", "unit": "ms", "y_zero": True,
                    "series": [{"name": "", "color": "#b392f0", "pts": pts(rows)}], "stats": [s[0], s[2], info],
                    "lower": {"kind": "outages", "title": f"{src['name']} nicht erreichbar", "rows": list(reversed(o))}}
        return {"error": "unbekannt"}

    def web_history(self, sid, rng):
        """Für die Handy-Oberfläche (Server-Thread, nur lesen)."""
        from pages import HistoryPage
        srcs = self.history_sources()
        src = next((x for x in srcs if x["id"] == sid), srcs[0] if srcs else None)
        rng = max(0, min(int(rng), len(HistoryPage.RANGES) - 1))
        _, span, bucket = HistoryPage.RANGES[rng]
        t1 = time.time()
        res = self.history_data(src, t1 - span, t1, bucket) if src else {"error": "keine Daten"}
        if res.get("lower"):
            res["lower"].pop("label_fmt", None)
            res["lower"]["month"] = res["lower"].get("bucket", 0) >= 28 * 86400
        return {"sources": [{"id": x["id"], "label": x["label"]} for x in srcs], "id": src["id"] if src else "",
                "t0": t1 - span, "t1": t1, "bucket": bucket, "range": rng,
                "ranges": [r[0] for r in HistoryPage.RANGES], **res}

    # ---------------- Diagnose ----------------
    version = VERSION
    headless = HEADLESS
    probe = PROBE

    @property
    def data_dir(self):
        return _data_dir()

    @staticmethod
    def log_path():
        return applog.path() or ""

    def run_diagnose(self, why="manuell"):
        """Startet den Selbsttest im Hintergrund (Haupt-Thread)."""
        if self.diag_running:
            return False
        try:
            snap = diagnose.collect(self)
        except Exception:
            log.exception("Diagnose vorbereiten")
            return False
        self.diag_running, self.diag_progress = True, "startet …"
        log.info("Diagnose gestartet (%s)", why)
        self._diag_refresh()

        def work():
            try:
                res = diagnose.run(snap, lambda t: self.results.put(("diag_progress", t)))
            except Exception as e:  # noqa: BLE001
                log.exception("Diagnose")
                res = [diagnose.R("System", "Diagnose", diagnose.BAD, f"{type(e).__name__}: {e}")]
            self.results.put(("diag", res))
        threading.Thread(target=work, daemon=True, name="Diagnose").start()
        return True

    def _on_diag_progress(self, text):
        self.diag_progress = text
        self._diag_refresh()

    def _on_diag(self, res):
        self.diag_results, self.diag_time, self.diag_running, self.diag_progress = res, time.time(), False, ""
        c = diagnose.summary(res)
        self.log("App", "Diagnose", diagnose.summary_text(res))
        for r in res:
            if r["status"] == diagnose.BAD:
                log.warning("Diagnose: %s / %s: %s", r["group"], r["name"], r["detail"])
        log.info("Diagnose fertig: %s ok, %s Hinweise, %s Probleme", c["ok"], c["warn"], c["bad"])
        try:
            tmp = os.path.join(_data_dir(), "diagnose.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"time": self.diag_time, "version": VERSION, "probe": PROBE, "results": res}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(_data_dir(), "diagnose.json"))
        except OSError:
            log.exception("Diagnose speichern")
        self._diag_refresh()

    def _load_last_diag(self):
        try:
            with open(os.path.join(_data_dir(), "diagnose.json"), encoding="utf-8") as f:
                d = json.load(f)
            self.diag_results, self.diag_time = d.get("results", []), d.get("time", 0.0)
        except (OSError, ValueError):
            pass

    def _diag_refresh(self):
        if self.page == "diagnose":
            self.tab_diagnose.refresh()

    def diag_state(self):
        """Für die Handy-Oberfläche (Server-Thread, nur lesen)."""
        return {"running": self.diag_running, "progress": self.diag_progress, "time": self.diag_time,
                "time_text": time.strftime("%d.%m. %H:%M", time.localtime(self.diag_time)) if self.diag_time else "",
                "summary": diagnose.summary(self.diag_results), "results": self.diag_results, "probe": PROBE}

    # ---------------- Probelauf (wie auf dem Heimserver, aber hier am PC) ----------------
    def probe_running(self):
        return bool(self.probe_proc and self.probe_proc.poll() is None)

    def probe_port(self):
        return int(self.cfg.get("web_port", 8770)) + PROBE_PORT_OFFSET

    def probe_start(self):
        """Startet eine zweite Hauskern-Instanz ohne Fenster mit den exportierten Einstellungen."""
        import shutil
        import sqlite3
        import subprocess
        if self.probe_running():
            return True, "läuft bereits"
        if not self.cfg.get("web_users"):
            return False, "Bitte zuerst unter Einstellungen → Handy-Zugriff einen Handy-Nutzer anlegen – der Heimserver braucht ihn."
        d = os.path.join(_data_dir(), "probelauf")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        c = self.export_config()  # genau das, was auch der Heimserver bekommt (Passwörter ohne Windows-Verschlüsselung)
        c["web_port"] = self.probe_port()
        c["satellite"] = True  # keine Automationen und keine Push-Nachrichten doppelt
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False)
        with self.store.lock:
            dst = sqlite3.connect(os.path.join(d, "verlauf.db"))
            self.store.db.backup(dst)
            dst.close()
        self._probe_heartbeat(d)
        env = dict(os.environ, HAUSKERN_DATA=d)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        cmd = [sys.executable, os.path.abspath(__file__), "--headless", "--probe"]
        if os.name != "nt" and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd
        try:
            self.probe_proc = subprocess.Popen(cmd, env=env, cwd=HERE, creationflags=flags,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.exception("Probelauf starten")
            return False, f"Konnte nicht starten: {e}"
        self.probe_dir, self.probe_started = d, time.time()
        self.log("App", APP, "Probelauf gestartet")
        log.info("Probelauf gestartet (Port %s, Ordner %s)", self.probe_port(), d)
        return True, ""

    @staticmethod
    def _probe_heartbeat(d):
        try:
            with open(os.path.join(d, "herzschlag"), "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass

    def probe_poll(self):
        """Vom Dialog alle 2 s: Herzschlag schreiben, Stand einlesen."""
        d = self.probe_dir
        st = {"running": self.probe_running(), "uptime": time.time() - self.probe_started if self.probe_started else 0,
              "diag": None, "log": [], "errors": 0,
              "urls": [f"http://{ip}:{self.probe_port()}" for ip in self.local_ips()]}
        if not d:
            return st
        if st["running"]:
            self._probe_heartbeat(d)
        try:
            with open(os.path.join(d, "diagnose.json"), encoding="utf-8") as f:
                st["diag"] = json.load(f)
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(d, applog.LOG_NAME), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-400:]
            st["log"] = [ln for ln in lines if re.search(r"^.{19}\s+(WARNING|ERROR|CRITICAL)", ln)][-40:]
            st["errors"] = sum(1 for ln in st["log"] if re.search(r"^.{19}\s+(ERROR|CRITICAL)", ln))
        except OSError:
            pass
        return st

    def probe_request_diag(self):
        if self.probe_dir:
            try:
                open(os.path.join(self.probe_dir, "diagnose.anfrage"), "w").close()
            except OSError:
                pass

    def probe_stop(self):
        p = self.probe_proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:  # noqa: BLE001
                p.kill()
            self.log("App", APP, "Probelauf beendet")
            log.info("Probelauf beendet")
        self.probe_proc = None

    def probe_dialog(self):
        d = ProbeDialog(self, self)
        self.wait_window(d)

    def _probe_tick(self):
        """Läuft im Hintergrundbetrieb: Diagnose auf Anfrage; der Probelauf beendet sich selbst, wenn der PC weg ist."""
        try:
            d = _data_dir()
            req = os.path.join(d, "diagnose.anfrage")
            if os.path.exists(req):
                os.remove(req)
                self.run_diagnose("Anfrage")
            if PROBE:
                hb = os.path.join(d, "herzschlag")
                age = time.time() - os.path.getmtime(hb) if os.path.exists(hb) else 999
                if age > 60 or time.time() - self._t0 > PROBE_MAX_S:
                    log.info("Probelauf endet (%s)", "Zeitlimit" if age <= 60 else "PC-Fenster geschlossen")
                    self.web and self.web.stop()
                    os._exit(0)
        except Exception:
            log.exception("Probelauf-Takt")
        if not self.stop.is_set():
            self.after(5000, self._probe_tick)

    _t0 = time.time()

    # ---------------- Neue Geräte im Netzwerk ----------------
    def _check_new_hosts(self, hosts):
        rows = [((h["mac"] or "").upper(), h) for h in hosts if h.get("mac")]
        if not rows:
            return
        if self.known_macs is None:
            self.known_macs = self.store.known_macs()
            if not self.known_macs:  # allererster Start: alles, was die Box kennt, gilt als bekannt
                self.store.add_known([(m, h["name"]) for m, h in rows])
                self.known_macs = {m for m, _ in rows}
                self.log("Netzwerk", APP, f"{len(rows)} Geräte als bekannt gemerkt")
                return
        new = [(m, h) for m, h in rows if m not in self.known_macs]
        if not new:
            return
        self.store.add_known([(m, h["name"]) for m, h in new])
        self.known_macs |= {m for m, _ in new}
        for m, h in new:
            if not h["active"]:
                continue  # nur in der Liste der Box aufgetaucht (z. B. nach Wiederherstellung) – kein Alarm
            self.new_macs[m] = time.time()
            via = "WLAN" if h["iface"] == "802.11" else ("LAN" if h["iface"] == "Ethernet" else (h["iface"] or "?"))
            ap = self.mesh_map.get(m)
            if ap:
                via += " · " + self._short(ap["model"])
            if h.get("guest"):
                via += " · Gastnetz"
            self.notify("newdevice", f"Neues Gerät im Netzwerk: {h['name'] or m}",
                        f"{h['ip'] or '–'} · {via} · {m}", warn=True)
            self.plugins.emit("new_device", name=h["name"] or m, ip=h["ip"] or "", mac=m)

    def relearn_hosts(self):
        if not messagebox.askyesno(APP, "Alle Geräte, die die FRITZ!Box gerade kennt, als bekannt merken?\n\n"
                                        "Danach meldet Hauskern nur noch Geräte, die neu dazukommen."):
            return
        self.store.forget_known()
        self.known_macs, self.new_macs = None, {}
        self._check_new_hosts(self.data["hosts"])
        self._render_hosts()

    # ---------------- Raspberry Pi ----------------

    def _update_sat_label(self):
        if self.cfg.get("satellite"):
            self.l_sat.configure(text=f"Automationen laufen auf dem\nRaspberry Pi ({self.cfg.get('pi_host') or '?'})")
        else:
            self.l_sat.configure(text="")

    def pi_dialog(self):
        d = PiDialog(self, self)
        self.wait_window(d)
        self._update_sat_label()

    def export_config(self):
        """Einstellungen für einen anderen Rechner: Passwörter entschlüsselt (Windows-DPAPI gilt nur hier)."""
        import copy
        self.update_token()  # gemeinsamer Schlüssel für Updates PC → Pi
        c = copy.deepcopy(self.cfg)

        def plain(v):
            pw = unprotect(v or "")
            return protect_plain(pw) if pw else ""
        c["password"] = protect_plain(self.password) if self.password else plain(c.get("password"))
        for lst, field in (("cameras", "pw"), ("calendars", "pw"), ("boxes", "pw"), ("printers", "code")):
            for item in c.get(lst, []):
                if item.get(field):
                    item[field] = plain(item[field])
        for svc, field in PUSH_SECRETS:
            val = c.get("push", {}).get(svc, {}).get(field, "")
            if val:
                c["push"][svc][field] = plain(val) if val.startswith(("dpapi:", "plain:")) else protect_plain(val)
        for u in c.get("web_users") or []:  # Zwei-Faktor-Schlüssel
            for field in ("totp", "totp_pending"):
                if u.get(field):
                    u[field] = plain(u[field])
        for k in ("satellite", "pi_host", "pi_port", "tray", "tray_hint_shown"):
            c.pop(k, None)
        c["web_enabled"] = True
        return c

    def make_migration_package(self, folder):
        import tempfile
        import zipfile
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{APP}-Umzug.zip")
        tmpdb = os.path.join(tempfile.mkdtemp(), "verlauf.db")
        import sqlite3
        with self.store.lock:
            dst = sqlite3.connect(tmpdb)
            self.store.db.backup(dst)  # konsistente Kopie, auch während die App läuft
            dst.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("config.json", json.dumps(self.export_config(), indent=2, ensure_ascii=False))
            z.write(tmpdb, "verlauf.db")
        self.log("App", APP, "Umzugspaket für den Heimserver erstellt")
        return path

    def send_config_to_pi(self, host, port, pin, code=""):
        """Läuft im Hintergrund-Thread. Gibt (ok, Meldung) zurück – Meldung beginnt mit „2FA:“, wenn der
        Heimserver den Zwei-Faktor-Code des Admins braucht."""
        import urllib.request
        import urllib.error
        body = json.dumps({"pin": pin, "code": code, "config": self.export_config()}).encode()
        req = urllib.request.Request(f"http://{host}:{int(port)}/api/config", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as r:
                res = json.loads(r.read() or b"{}")
                return bool(res.get("ok")), res.get("msg", "")
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read())
                return False, ("2FA:" if j.get("need_code") else "") + j.get("error", f"HTTP {e.code}")
            except Exception:
                return False, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            return False, f"Heimserver nicht erreichbar ({getattr(e, 'reason', e)})"

    def import_config(self, incoming):
        """Vom Webserver aufgerufen (Server-Thread) – Übernahme passiert im Haupt-Thread."""
        holder = {"ev": threading.Event(), "res": (False, "Zeitüberschreitung")}
        self.results.put(("import_config", (incoming, holder)))
        holder["ev"].wait(10)
        return holder["res"]

    def _on_import_config(self, payload):
        incoming, holder = payload
        try:
            new = dict(DEFAULT_CFG)
            new.update(incoming)
            for k in ("web_pin", "web_sessions", "web_port"):  # Zugang des Heimservers bleibt, wie er ist
                if k in self.cfg:
                    new[k] = self.cfg[k]
            if not incoming.get("web_users") and self.cfg.get("web_users"):
                new["web_users"] = self.cfg["web_users"]  # PC ohne Nutzerliste (ältere Version) → Pi behält seine
            else:
                import totp
                new["web_users"] = totp.merge(new.get("web_users"), self.cfg.get("web_users"))  # 2FA vom Handy bleibt
            import users as _u
            _u.migrate(new)
            new["web_enabled"] = True
            new["satellite"] = False
            save_cfg(new)
            self.log("App", APP, "Einstellungen vom PC übernommen – starte neu")
            log.info("Einstellungen vom PC übernommen, Neustart")
            holder["res"] = (True, "Übernommen – Hauskern auf dem Heimserver startet neu (ca. 15 Sekunden)")
            if HEADLESS:
                self.after(800, lambda: os._exit(0))  # systemd startet den Dienst sofort neu
            else:
                holder["res"] = (True, "Übernommen – bitte Hauskern auf dem Heimserver neu starten")
        except Exception as e:
            log.exception("Einstellungen übernehmen")
            holder["res"] = (False, str(e))
        holder["ev"].set()

    # ---------------- Handy-Zugriff ----------------
    app_name = APP
    assets_dir = os.path.join(HERE, "assets")

    @staticmethod
    def local_ips():
        ips = []
        try:
            s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s_.connect(("192.168.178.1", 9))  # es wird nichts gesendet – nur die eigene Adresse ermitteln
            ips.append(s_.getsockname()[0])
            s_.close()
        except OSError:
            pass
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
        except OSError:
            pass
        return ips

    MORE = [("🧵   Filament-Lager", "filament_dialog"), ("🎬   Zeitraffer", "timelapse_dialog"), ("🎥   Kamera-Aufnahmen", "clips_dialog"), None,
            ("🏖   Urlaubsmodus", "vacation_dialog"), ("🧺   Waschmaschine & Co.", "appliances_dialog"),
            ("🔌   Weitere Hersteller", "integrations_dialog"),
            ("🔋   Batterien", "battery_dialog"), ("🔥   Heizungs-Check", "heating_dialog"), None,
            ("🛒   Einkaufsliste & Aufgaben", "lists_dialog"), ("🧠   Kamera-Erkennung", "vision_dialog"), ("📞   Anrufliste", "calls_dialog"), ("🧩   Erweiterungen", "plugins_dialog"), ("📱   Aufs Handy", "phone_dialog"), ("🗣   Siri-Kurzbefehle", "siri_dialog"),
            ("🎙   Alexa & Google", "voice_dialog"), ("✉   Push-Nachrichten", "push_dialog"), None,
            ("💾   Sicherung", "backup_dialog"), ("⬆   Updates", "update_dialog"), ("⇄   Heimserver (Pi/Mini-PC)", "pi_dialog"),
            ("✨   Einrichtung", "wizard"), ("📰   Was ist neu?", "whats_new"), None, ("🐞   Problem melden", "bug_dialog"),
            ("📜   Lizenz & Rechtliches", "legal_dialog")] + ([("❤   Hauskern unterstützen", "donate")]
                                                            if not edition.is_family() and edition.DONATE_URL else []) + ([
            ("📨   Meldungen empfangen (für Pascal)", "bug_receive_dialog")] if edition.is_family() else [])

    def _more_menu(self, anchor):
        """Alle Extras in einem Menü – hält die Seitenleiste auch auf kleinen Bildschirmen übersichtlich."""
        m = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#ffffff",
                    font=("Segoe UI", 10), bd=0, relief="flat")
        for it in self.MORE:
            if it is None:
                m.add_separator()
            else:
                m.add_command(label=it[0], command=lambda f=it[1]: getattr(self, f)())
        try:
            m.tk_popup(anchor.winfo_rootx() + anchor.winfo_width() - 20, anchor.winfo_rooty() - 8)
        finally:
            m.grab_release()

    def phone_dialog(self, parent=None):
        from pages import PhoneAccessDialog
        d = PhoneAccessDialog(parent or self, self)
        self.wait_window(d)

    def web_urls(self):
        port = self.cfg.get("web_port", 8770)
        # Heimnetz zuerst (FRITZ!Box: 192.168.x), virtuelle Adapter (Hyper-V/WSL: 172.x) nach hinten
        ips = sorted(self.local_ips(), key=lambda ip: (not ip.startswith("192.168."), ip.startswith("172."), ))
        return [f"http://{ip}:{port}" for ip in ips]

    def start_web(self, show_info=False):
        if self.web:
            self.web.stop()
            self.web = None
        self._users_ready()
        if not (self.cfg.get("web_enabled") or HEADLESS) or not (self.cfg.get("web_users") or HEADLESS):
            if HEADLESS:
                log.error("Keine PIN für die Handy-Oberfläche – bitte am PC festlegen und an den Heimserver senden")
            return
        self.web = WebServer(self, self.cfg.get("web_port", 8770))
        if self.web.start():
            log.info("Handy-Oberfläche läuft auf Port %s", self.cfg.get("web_port"))
            self.log("App", APP, "Handy-Zugriff eingeschaltet")
            self._web_tick()
            if show_info:
                urls = "\n".join(self.web_urls()) or f"http://<IP dieses PCs>:{self.cfg.get('web_port')}"
                messagebox.showinfo(APP, "Handy-Zugriff ist an 📱\n\nÖffne auf dem iPhone in Safari:\n" + urls +
                                    "\n\nGleich kommt ein QR-Code zum Abscannen.\n"
                                    "Fragt Windows nach der Firewall: „Private Netzwerke“ erlauben.")
                if self.web_urls():
                    self.after(200, self.phone_dialog)
        else:
            log.warning("Handy-Oberfläche startet nicht: %s", self.web.error)
            messagebox.showwarning(APP, f"Handy-Zugriff konnte nicht starten (Port belegt?):\n{self.web.error}")
            self.web = None

    def _web_tick(self):
        if not self.web:
            return
        try:
            self._web_state = self._build_web_state()
        except Exception:
            log.exception("Handy-Status")
        if not self.stop.is_set():
            self.after(2000, self._web_tick)

    # --- Callbacks für den Webserver (laufen im Server-Thread!) ---
    def get_state(self):
        return self._web_state

    def _users_ready(self):
        """Alte Einzel-PIN (z. B. aus einer importierten Konfiguration) → erster Nutzer."""
        if not self.cfg.get("web_users") and self.cfg.get("web_pin"):
            import users
            if users.migrate(self.cfg):
                try:
                    self.save()
                except OSError:
                    pass

    def pin_hash(self):
        """Nur noch als „ist überhaupt jemand eingerichtet?“ benutzt."""
        self._users_ready()
        users = self.cfg.get("web_users") or []
        return users[0]["pin"] if users else ""

    def sessions(self):
        return {s["h"] for s in self.cfg.get("web_sessions", []) if isinstance(s, dict)}

    def session_user(self, h):
        import users
        self._users_ready()
        return users.session_user(self.cfg, h)

    def login_pin(self, pin):
        import users
        self._users_ready()
        return users.find_by_pin(self.cfg.get("web_users"), pin, check_pin)

    def check_admin_pin(self, pin):
        import users
        self._users_ready()
        u = users.find_by_pin(self.cfg.get("web_users"), pin, check_pin)
        if u:
            return bool(u.get("admin"))
        return bool(self.cfg.get("web_pin") and not self.cfg.get("web_users") and check_pin(pin, self.cfg["web_pin"]))

    # ---------------- Zwei-Faktor ----------------
    def secret_protect(self, s):
        return protect(s)

    def secret_unprotect(self, s):
        return unprotect(s or "")

    def twofa_login(self, user, code):
        """Webserver-Thread: True, wenn der Nutzer kein Zwei-Faktor hat oder der Code passt.
        Ein benutzter Code bzw. Notfallcode wird sofort vermerkt (gespeichert wird mit der neuen Sitzung)."""
        import totp
        if not totp.enabled(user):
            return True
        with self._sess_lock:
            u = next((x for x in self.cfg.get("web_users") or [] if x.get("id") == user.get("id")), user)
            how = totp.check_user(u, code, unprotect)
        if how == "backup":
            left = totp.backups_left(u)
            self.results.put(("web_backup_used", (u.get("name", ""), left)))
        return how is not None

    def _on_web_backup_used(self, payload):
        name, left = payload
        self.save()
        try:  # selten und wichtig: Warst du das nicht, PIN ändern und Zwei-Faktor neu einrichten
            self.notify("health", "🔐 Anmeldung mit Notfallcode",
                        f"{name} hat sich mit einem Notfallcode angemeldet – noch {left} übrig."
                        + (" Neue gibt es am Handy unter ⚙ → Zwei-Faktor-Anmeldung." if left <= 2 else ""), warn=left <= 2)
        except Exception:  # noqa: BLE001 – Hinweis ist nur ein Extra
            log.exception("Hinweis Notfallcode")

    def add_session(self, h, user=None):
        """Sofort gültig (die nächste Anfrage vom Handy kommt Millisekunden später), gespeichert wird danach."""
        import users
        user = user or next((u for u in self.cfg.get("web_users", []) if u.get("admin")), None)
        if not user:
            return
        with self._sess_lock:
            users.add_session(self.cfg, h, user)  # ersetzt die Liste in einem Schritt
        self.results.put(("web_session", user))

    def _on_web_session(self, user):
        self.save()
        self.log("App", user.get("name", APP), "Handy angemeldet")

    # ---------------- Einstellungen über das Handy ----------------
    def main_call(self, fn, timeout=25):
        """Aus dem Webserver-Thread etwas im Haupt-Thread ausführen und auf das Ergebnis warten."""
        if threading.current_thread() in (threading.main_thread(), getattr(self, "_mc_thread", None)):
            return fn()  # schon im Haupt-Thread (z. B. „Ausprobieren“ aus den Einstellungen) – sonst wartet er auf sich selbst
        holder = {"ev": threading.Event(), "res": None, "err": None}
        self.results.put(("main_call", (fn, holder)))
        if not holder["ev"].wait(timeout):
            raise TimeoutError("Hauskern antwortet gerade nicht – bitte nochmal versuchen")
        if holder["err"]:
            raise holder["err"]
        return holder["res"]

    def _on_main_call(self, payload):
        fn, holder = payload
        self._mc_thread = threading.current_thread()
        try:
            holder["res"] = fn()
        except Exception as e:  # noqa: BLE001 – wird im Webserver-Thread weitergereicht
            holder["err"] = e
        finally:
            self._mc_thread = None
            holder["ev"].set()

    def notify_kinds(self):
        return dict(NOTIFY_KINDS)

    def web_admin(self, method, section, op, ident, vals, user):
        """GET/POST für /api/admin und /api/auto – Rechte prüft der Webserver vorher."""
        import webadmin as W

        def run():
            try:
                if section == "auto":
                    if method == "GET":
                        return {"ok": True, "rules": W.auto_list(self), "schema": W.auto_schema(self)}
                    ok, msg, extra = W.auto_post(self, op, ident, vals, user)
                elif method == "GET":
                    return dict(W.get(self, section, ident, user), ok=True)
                else:
                    ok, msg, extra = W.post(self, section, op, ident, vals, user)
                return dict(extra or {}, ok=ok, msg=msg)
            except W.Bad as e:
                return {"ok": False, "msg": str(e)}
        try:
            return self.main_call(run)
        except Exception as e:  # noqa: BLE001
            log.exception("Handy-Einstellungen (%s %s)", section, op)
            return {"ok": False, "msg": f"Fehler: {e}"}

    # ---------------- Sicherung ----------------
    def backup_cfg(self):
        return dict(DEFAULT_CFG["backup"], **(self.cfg.get("backup") or {}))

    @property
    def backups_dir(self):
        return os.path.join(_data_dir(), "sicherungen")

    def _backup_tick(self):
        c = self.backup_cfg()
        if not c.get("enabled") or self._backup_busy:
            return
        try:
            h, m = map(int, str(c.get("time", "03:30")).split(":"))
        except ValueError:
            h, m = 3, 30
        now = time.localtime()
        due = now.tm_hour * 60 + now.tm_min >= h * 60 + m
        last = float(c.get("last") or 0)
        if due and time.strftime("%Y-%m-%d", time.localtime(last)) != time.strftime("%Y-%m-%d", now):
            self.run_backup("auto")

    def run_backup(self, kind="manual", done=None):
        """Im Hintergrund: Sicherung anlegen, aufräumen, ggf. zusätzlich kopieren."""
        if self._backup_busy:
            return False
        self._backup_busy = True
        c = self.backup_cfg()
        nas = self.nas_cfg()
        dest, data, db = self.backups_dir, _data_dir(), getattr(self.store, "path", "")

        def work():
            import backup
            res = {"kind": kind, "ok": False}
            try:
                path = backup.create(data, dest, db, VERSION, kind)
                backup.prune(dest)
                res.update(ok=True, path=path, size=os.path.getsize(path))
                if c.get("extra_dir"):
                    try:
                        res["extra"] = backup.copy_to(path, c["extra_dir"])
                    except OSError as e:
                        res["extra_err"] = str(e)
                if nas.get("enabled") and nas.get("host") and nas.get("user"):
                    try:
                        res["nas"] = backup.nas_upload(path, nas["host"], nas["user"], nas["pw"], nas.get("dir", ""),
                                                       int(nas.get("keep") or 14))
                    except backup.BackupError as e:
                        res["nas_err"] = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("Sicherung")
                res["err"] = str(e)
            self.results.put(("backup_done", (res, done)))
        threading.Thread(target=work, daemon=True, name="Sicherung").start()
        return True

    def _on_backup_done(self, payload):
        import backup
        res, done = payload
        self._backup_busy = False
        c = dict(self.cfg.get("backup") or {})
        c.update(last=time.time(), last_ok=res["ok"], last_err=res.get("err") or res.get("extra_err") or "",
                 last_size=res.get("size", 0))
        if "nas" in res or "nas_err" in res:
            c.update(nas_last=time.time(), nas_ok="nas" in res, nas_err=res.get("nas_err", ""))
        self.cfg["backup"] = c
        self.save()
        if res["ok"]:
            self.log("App", "Sicherung", ("Manuelle " if res["kind"] == "manual" else "") + "Sicherung erstellt ("
                     + backup.fmt_size(res["size"]) + ")" + (" + Kopie" if res.get("extra") else ""))
            if res.get("nas_err"):
                self.notify("health", "💾 Kopie auf den Netzwerkspeicher fehlgeschlagen", res["nas_err"][:160], warn=True)
            if res.get("extra_err"):
                self.notify("health", "💾 Zusätzliche Kopie fehlgeschlagen", f"{c.get('extra_dir')}: {res['extra_err']}",
                            warn=True)
        else:
            self.log("App", "Sicherung", f"fehlgeschlagen: {res.get('err')}")
            self.notify("health", "💾 Sicherung fehlgeschlagen", str(res.get("err"))[:160], warn=True)
        if done:
            done(res)

    def nas_cfg(self):
        """Netzwerkspeicher-Einstellungen mit entschlüsseltem Passwort."""
        n = dict({"enabled": False, "host": "fritz.box", "user": "", "pw": "", "dir": "", "keep": 14},
                 **((self.cfg.get("backup") or {}).get("nas") or {}))
        n["pw"] = unprotect(n.get("pw", "")) if n.get("pw") else ""
        return n

    def save_nas(self, n):
        n = dict(n)
        if n.get("pw"):
            n["pw"] = protect(n["pw"]) or "plain:" + __import__("base64").b64encode(n["pw"].encode()).decode()
        c = dict(self.cfg.get("backup") or {})
        c["nas"] = n
        self.cfg["backup"] = c
        self.save()

    def list_backups(self):
        import backup
        return backup.list_backups(self.backups_dir)

    def restore_backup(self, path):
        """Prüft die Sicherung, legt sie bereit und startet Hauskern neu."""
        import backup
        meta = backup.stage_restore(path, _data_dir())
        self.log("App", "Sicherung", f"Wiederherstellung vom {backup.fmt_ts(meta.get('created'))} – Neustart")
        log.warning("Sicherung vom %s wird beim Neustart eingespielt", backup.fmt_ts(meta.get("created")))
        if HEADLESS:
            threading.Timer(1.5, lambda: os._exit(0)).start()  # systemd startet neu
        else:
            self.after(1200, self.restart)
        return meta

    def pull_token(self):
        return self.cfg.get("update_token", "")

    def latest_backup_bytes(self):
        bs = self.list_backups()
        if not bs:
            return None, None
        with open(bs[0]["path"], "rb") as f:
            return bs[0]["name"], f.read()

    def _pi_pull_tick(self):
        """PC: einmal am Tag die neueste Sicherung vom Heimserver holen (zweiter Ort!)."""
        if HEADLESS or not self.cfg.get("pi_host") or not self.backup_cfg().get("pi_pull", True):
            return
        if time.time() - self._pi_pull_t < 6 * 3600:
            return
        self._pi_pull_t = time.time()

        def work():
            import urllib.request
            import backup
            host, port = self.cfg.get("pi_host"), int(self.cfg.get("pi_port", 8770))
            req = urllib.request.Request(f"http://{host}:{port}/api/backup/latest",
                                         headers={"X-Hauskern-Token": self.update_token()})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
                    name = r.headers.get("X-Hauskern-Name") or "hauskern-sicherung-pi.zip"
                    data = r.read()
                dest = os.path.join(_data_dir(), "pi-sicherungen")
                os.makedirs(dest, exist_ok=True)
                target = os.path.join(dest, os.path.basename(name))
                if not os.path.exists(target):
                    with open(target + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(target + ".tmp", target)
                    for b in backup.list_backups(dest)[7:]:
                        os.remove(b["path"])
                    log.info("Sicherung vom Heimserver geholt: %s", name)
            except Exception as e:  # noqa: BLE001
                log.info("Sicherung vom Heimserver nicht geholt: %s", e)
        threading.Thread(target=work, daemon=True, name="Pi-Sicherung").start()

    def backup_dialog(self):
        from pages import BackupDialog
        d = BackupDialog(self, self)
        self.wait_window(d)

    # ---------------- Erweiterungen ----------------
    # ---------------- Geräte anderer Hersteller ----------------
    def _integrations_start(self):
        try:
            self.integrations.start()
            self.integrations.reload()
        except Exception:
            log.exception("Anbieter starten")

    def _integration_cfgs(self):
        """Einstellungen mit entschlüsseltem Passwort (gespeichert wird es verschlüsselt)."""
        return [dict(c, pw=unprotect(c.get("pw", "")) if c.get("pw") else "") for c in self.cfg.get("integrations", [])]

    def integration_title(self, c):
        """Name für Listen: selbst vergeben → Name vom Gerät → Adresse."""
        devs = self.ext_devs.get(c["id"]) or []
        if c.get("name"):
            return c["name"]
        if c.get("type") in ("hue", "z2m"):
            return {"hue": "Hue Bridge", "z2m": "Zigbee2MQTT"}[c["type"]] + f" ({c.get('host')})"
        return (devs[0]["name"] if devs else "") or c.get("host", "")

    def integration_status(self, iid):
        p = self.integrations.provs.get(iid)
        n = len(self.ext_devs.get(iid) or [])
        if not p:
            return "aus" if any(c["id"] == iid and not c.get("enabled", True)
                                for c in self.cfg.get("integrations", [])) else "startet …"
        if p.error:
            host = getattr(p, "host", None) or p.c.get("host")
            h = next((x for x in (self.data.get("hosts") or []) if x.get("ip") == host), None)
            who = (f" · In der FRITZ!Box heißt {host}: „{h.get('name') or '?'}“"
                   + ("" if h.get("active", True) else " (gerade nicht aktiv)")) if h else (
                f" · {host} steht nicht in der Geräteliste der FRITZ!Box – IP richtig?" if self.data.get("hosts") else "")
            return f"⚠ {p.error}{who}"
        return f"✔ {n} Gerät{'e' if n != 1 else ''}" if p.ok_once else "verbinde …"

    def integration_suggestions(self):
        """Aus der Gerätesuche: lokal steuerbare Geräte, die noch nicht eingebunden sind → [(ip, name, typ)]."""
        have = {c.get("host") for c in self.cfg.get("integrations", [])}
        typ = {"Shelly": "shelly", "Tasmota": "tasmota", "ESPHome": "esphome", "Philips Hue": "hue"}
        out = []
        for d in self.disc_results or []:
            t = typ.get(d.get("vendor"))
            if t and d.get("ip") and d["ip"] not in have:
                out.append((d["ip"], d.get("name") or d["ip"], t))
        return out

    def save_integration(self, c):
        if "pw_new" in c:
            c["pw"] = protect(c.pop("pw_new"))
        lst = [x for x in self.cfg.get("integrations", []) if x["id"] != c["id"]]
        idx = next((i for i, x in enumerate(self.cfg.get("integrations", [])) if x["id"] == c["id"]), len(lst))
        lst.insert(idx, c)
        self.cfg["integrations"] = lst
        self.save()
        self.integrations.reload()

    def remove_integration(self, iid):
        self.cfg["integrations"] = [x for x in self.cfg.get("integrations", []) if x["id"] != iid]
        self.save()
        self.integrations.reload()

    def _plugins_start(self):
        """Nur dort, wo die Automationen laufen – sonst würden Erweiterungen doppelt schalten und melden."""
        if self.cfg.get("satellite"):
            log.info("Erweiterungen: laufen auf dem Heimserver, nicht auf diesem PC")
            return
        try:
            self.plugins.start()
        except Exception:
            log.exception("Erweiterungen starten")

    def _on_plugin(self, payload):
        kind, data = payload
        if kind == "log":
            self.log("Erweiterung", data[0], data[1])
        elif kind == "notify":
            self.notify("plugin", data[0], data[1])
        elif kind == "save":
            self.save()
        elif kind == "tiles":
            self._plugin_tiles()

    def _plugin_tiles(self):
        if not self.dash:
            return
        keys = []
        for k, t in sorted(dict(self.plugins.tiles).items()):  # Kopie – Erweiterungen ändern das parallel
            self.dash.put("Erweiterungen", k, dict(t, type="plugin"))
            keys.append(k)
        self.dash.remove_missing("Erweiterungen", "plug:", keys)
        self.dash.commit()

    def plugins_dialog(self):
        from pages import PluginsDialog
        d = PluginsDialog(self, self)
        self.wait_window(d)

    # ---------------- Fehldruck-Wächter ----------------
    def _printwatch(self, serial, summ, st):
        """Meldet, wenn die KI im Drucker einen Fehldruck sieht – mit Kamerafoto. Ändert nichts am Drucker."""
        seen = self._pw_seen.setdefault(serial, set())
        if st not in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            seen.clear()  # neuer Druck → wieder melden
            return
        for ev in printwatch.events(summ.get("print_error"), summ.get("hms")):
            if ev in seen:
                continue
            seen.add(ev)
            title, text = printwatch.message(ev, summ.get("name", "Drucker"))
            pr = self.printers.get(serial)
            img = None
            if pr:
                url = bambu_url(pr.ip, pr.code)
                img = lambda url=url: grab_frame(url)  # noqa: E731 – läuft im Push-Thread
            self.notify("printer", title, text, warn=True, image_fn=img)
            self.log("3D-Druck", summ.get("name", ""), "KI im Drucker: " + (
                "Fehldruck erkannt, pausiert" if ev == "spaghetti_paused" else "möglicher Fehldruck"))
            if getattr(self, "plugins", None):
                self.plugins.emit("print_problem", printer=summ.get("name", ""), paused=ev == "spaghetti_paused")

    # ---------------- Einkaufsliste & Aufgaben ----------------
    def lists(self):
        import lists as LS
        return LS.ensure(self.cfg)

    def web_lists(self):
        out = {}
        for k, lst in self.lists().items():
            items = sorted(lst["items"], key=lambda i: (i["done"], -(i.get("done_ts") or 0) if i["done"] else i["ts"]))
            out[k] = {"name": lst["name"], "icon": lst["icon"],
                      "items": [{"id": i["id"], "text": i["text"], "done": i["done"], "by": i.get("by", "")} for i in items]}
        return {"ok": True, "lists": out}

    def lists_op(self, body, user):
        """Ändern (aus Handy oder PC). body: {op: add|done|undone|remove|clear, list, text|id}"""
        import lists as LS

        def run():
            ls = self.lists()
            key = body.get("list") if body.get("list") in ls else "einkauf"
            lst, op, who = ls[key], body.get("op"), (user or {}).get("name", "")
            if op == "add":
                new, dup = LS.add(lst, LS.split_items(str(body.get("text", ""))[:300]), who)
                if not new and not dup:
                    return False, "Bitte etwas eintippen"
                msg = (f"„{', '.join(new)}“ eingetragen" if new else "") + (
                    (" · " if new else "") + f"„{', '.join(dup)}“ steht schon drauf" if dup else "")
            elif op in ("done", "undone"):
                it = LS.set_done(lst, str(body.get("id", "")), op == "done", who)
                if not it:
                    return False, "Eintrag nicht gefunden"
                msg = f"✓ {it['text']}" if op == "done" else f"↺ {it['text']}"
            elif op == "remove":
                if not LS.remove(lst, str(body.get("id", ""))):
                    return False, "Eintrag nicht gefunden"
                msg = "Gelöscht"
            elif op == "clear":
                msg = f"{LS.clear_done(lst)} erledigte entfernt"
            else:
                return False, "Unbekannte Aktion"
            self.save()
            self._lists_tile()
            return True, msg
        return self.main_call(run)

    def _lists_tile(self):
        if not self.dash:
            return
        import lists as LS
        ls = self.lists()
        e = LS.open_items(ls["einkauf"])
        a = LS.open_items(ls["aufgaben"])
        self.dash.put("Personen", "lists:main", {"type": "lists", "shop": [i["text"] for i in e][:6], "shop_n": len(e),
                                                 "todo": [i["text"] for i in a][:3], "todo_n": len(a)})
        self.dash.commit()

    def _lists_prune(self):
        import lists as LS
        if not self.stop.is_set():
            self.after(6 * 3600 * 1000, self._lists_prune)
        if LS.prune(self.lists()):
            self.save()
            self._lists_tile()

    def lists_dialog(self):
        from pages import ListsDialog
        d = ListsDialog(self, self)
        self.wait_window(d)

    # ---------------- Kamera-Erkennung ----------------
    def vision_cfg(self):
        return self.cfg.setdefault("vision", {"enabled": False, "sens": "mittel", "cams": {}})

    def vision_on(self, cid=None):
        import vision as V
        c = self.vision_cfg()
        if not c.get("enabled") or self.cfg.get("satellite") or not V.available()[0] or not V.model_ok(self.vision.folder):
            return False
        return cid is None or (c.get("cams") or {}).get(cid, True)

    def vision_status(self):
        """Kurzer Text für Einstellungen/Diagnose."""
        import vision as V
        ok, why = V.available()
        if not ok:
            return "⚠ " + why
        if self.vision_dl:
            return f"⏳ Modell wird geladen … {self.vision_dl}"
        if not V.model_ok(self.vision.folder):
            return f"Modell noch nicht geladen (einmalig ca. {V.MODEL_MB} MB)"
        if self.cfg.get("satellite"):
            return "Läuft auf dem Heimserver – dort einschalten"
        return "✅ bereit" if self.vision_cfg().get("enabled") else "Modell da – Erkennung ist aus"

    def vision_download(self):
        """Modell im Hintergrund laden (einmalig)."""
        import vision as V
        if self.vision_dl:
            return "läuft schon"
        ok, why = V.available()
        if not ok:
            raise RuntimeError(why)
        self.vision_dl = "0 %"

        def prog(p):
            self.vision_dl = f"{p} %"

        def run():
            try:
                V.download(self.vision.folder, prog)
                self.results.put(("vision_dl", ""))
            except Exception as e:  # noqa: BLE001
                self.results.put(("vision_dl", str(e)))
        threading.Thread(target=run, daemon=True, name="Modell-Download").start()
        return "Download gestartet"

    def _on_vision_dl(self, err):
        self.vision_dl = None
        if err:
            self.log("Kamera", "Erkennung", f"Modell konnte nicht geladen werden: {err}")
            self._on_error(f"Kamera-Erkennung: {err}")
        else:
            self.log("Kamera", "Erkennung", "Modell geladen und geprüft ✅")
            self._on_status("🧠 Kamera-Erkennung: Modell geladen")

    def _vision_check(self, cid, attempt):
        """Bild holen und prüfen, was zu sehen ist. Bei Bewegung bis zu 4 Versuche im Abstand von 6 s."""
        import vision as V
        e = self.camobjs.get(cid)
        if not e or cid in self._vision_busy:
            return
        url = e.get("main") or e.get("sub")
        if not url:
            return
        self._vision_busy.add(cid)
        conf = V.SENS.get(self.vision_cfg().get("sens", "mittel"), 0.5)

        def run():
            try:
                jpg = grab_frame(url)
                if not jpg:
                    raise RuntimeError("kein Kamerabild")
                dets, area = self.vision.detect_jpeg(jpg, conf)
                found = V.summarize(dets, min_area=0.002, img_area=area)
                self.results.put(("vision", (cid, found, attempt, "")))
            except Exception as ex:  # noqa: BLE001
                self.results.put(("vision", (cid, {}, attempt, str(ex))))
        threading.Thread(target=run, daemon=True, name="Kamera-Erkennung").start()

    def _on_vision(self, payload):
        import vision as V
        cid, found, attempt, err = payload
        self._vision_busy.discard(cid)
        e = self.camobjs.get(cid)
        if not e:
            return
        if err:
            if self._motion_log.ok("vision-err:" + cid):
                self.log("Kamera", e["cfg"]["name"], f"Erkennung fehlgeschlagen: {err}")
            return
        if found:
            prev = self.seen.get(cid)
            new = not prev or time.time() - prev["ts"] > 60 or set(found) - set(prev["found"])
            self.seen[cid] = {"ts": time.time(), "found": found}
            if new:
                best = ", ".join(f"{V.LABELS.get(k, k).split(' (')[0]} {round(v * 100)} %" for k, v in
                                 sorted(found.items(), key=lambda kv: -kv[1]))
                self.log("Kamera", e["cfg"]["name"], f"erkannt: {best}")
                if "person" in found:
                    self._vac_cam_alert(cid, "Person erkannt")
            self._cam_tile(cid)
            self.dash.commit()
            self._engine_update()
        elif self.motion.get(cid) and attempt < 3:
            self.after(6000, lambda: self._vision_check(cid, attempt + 1))

    def vision_dialog(self):
        from pages import VisionDialog
        d = VisionDialog(self, self)
        self.wait_window(d)

    # ---------------- Wochenrückblick ----------------
    WEEKLY_DAY, WEEKLY_HOUR = 6, 18  # Sonntag, 18 Uhr

    def _weekly_tick(self):
        if not self.stop.is_set():
            self.after(10 * 60 * 1000, self._weekly_tick)
        w = self.cfg.setdefault("weekly", {"enabled": False, "last": ""})
        if not w.get("enabled"):
            return
        now = time.localtime()
        key = time.strftime("%G-%V", now)  # Kalenderwoche – einmal pro Woche
        if now.tm_wday != self.WEEKLY_DAY or now.tm_hour < self.WEEKLY_HOUR or w.get("last") == key:
            return
        w["last"] = key
        self.save()
        try:
            title, text = self.weekly_summary()
        except Exception:
            log.exception("Wochenrückblick")
            return
        self.notify("weekly", title, text)
        self.log("App", "Wochenrückblick", "gesendet")

    def weekly_summary(self, now=None):
        """→ (Titel, Text) – nur Zeilen, zu denen es etwas zu sagen gibt."""
        import calls as CL
        import filament as FL
        now = now or time.time()
        t0 = now - 7 * 86400
        lines = []
        rep = (getattr(self, "power_rep", None) or {}).get("total") or {}
        if rep.get("month_eur") is not None:
            ln = f"⚡ Strom: {fnum(rep['month_eur'], 2)} € bisher im Monat"
            if rep.get("prog_eur") is not None:
                ln += f" (Hochrechnung {fnum(rep['prog_eur'], 0)} €)"
            lines.append(ln)
        prints = self.store.prints_between(t0, now)
        if prints:
            ok = sum(1 for r in prints if r[6] == "fertig")
            hours = sum(((r[3] or r[2]) - r[2]) for r in prints) / 3600
            grams = sum(float(r[8] or 0) for r in prints)
            ln = f"🖨 {len(prints)} Drucke ({ok} fertig) · {fnum(hours, 1)} h"
            if grams:
                ln += f" · {fnum(grams, 0)} g Filament"
            lines.append(ln)
        low = FL.summary(self.spools(), self.fil_low_limit())["low"] if self.spools() else []
        if low:
            lines.append("🧵 Knapp: " + ", ".join(low[:3]))
        bats = [b for b in self.battery_items() if b["low"] or (b["days_left"] is not None and b["days_left"] <= 45)]
        if bats:
            lines.append("🔋 Bald tauschen: " + ", ".join(f"{b['name']} ({b['pct']} %)" for b in bats[:3]))
        miss = [c for c in self.call_data if CL.is_missed(c) and c["ts"] >= t0]
        if miss:
            lines.append(f"📞 {len(miss)} verpasste Anrufe" if len(miss) > 1 else "📞 1 verpasster Anruf")
        try:
            hs = self.heating_report(7)["summary"]
            if hs.get("tips"):
                lines.append(f"🔥 Heizungs-Check: {hs['tips']} Hinweis" + ("e" if hs["tips"] != 1 else ""))
        except Exception:  # noqa: BLE001
            pass
        if not lines:
            lines.append("Ruhige Woche – nichts Besonderes 😊")
        return "📅 Dein Wochenrückblick", "\n".join(lines)

    def set_weekly(self, on):
        w = self.cfg.setdefault("weekly", {"enabled": False, "last": ""})
        if bool(on) != bool(w.get("enabled")):
            w["enabled"] = bool(on)
            lt = time.localtime()
            if on and lt.tm_wday == self.WEEKLY_DAY and lt.tm_hour >= self.WEEKLY_HOUR:
                w["last"] = time.strftime("%G-%V", lt)  # Sonntag nach 18 Uhr eingeschaltet: ab nächster Woche
            self.save()

    # ---------------- Was ist neu? ----------------
    def _whats_new_check(self):
        """Nach einem Update einmal zeigen, was neu ist. Beim allerersten Start nicht (da gibt es den Assistenten)."""
        seen = self.cfg.get("seen_version")
        if seen == VERSION:
            return
        show = not HEADLESS and self.cfg.get("wizard_done") and self.state() != "withdrawn"
        self.cfg["seen_version"] = VERSION
        self.save()
        if show:
            self.whats_new(since=seen or self._prev_version())

    def _prev_version(self):
        """Wer von vor 8.0 kommt, hat noch keine „seen_version“ – dann nur die aktuelle Version zeigen."""
        import changelog
        older = [v for v, _t, _l in changelog.sections() if changelog.vtuple(v) < changelog.vtuple(VERSION)]
        return older[0] if older else "0"

    def whats_new(self, since=None):
        import changelog
        secs = changelog.since(since or self._prev_version(), VERSION) if since else changelog.since(
            self._prev_version(), VERSION)
        if not secs:
            secs = changelog.sections()[:1]
        if not secs:
            return
        from pages import WhatsNewDialog
        d = WhatsNewDialog(self, secs)
        self.wait_window(d)

    def web_whatsnew(self, since=""):
        import changelog
        secs = changelog.since(since, VERSION) if since else changelog.since(self._prev_version(), VERSION)
        return {"ok": True, "version": VERSION,
                "sections": [{"v": v, "title": t, "html": changelog.to_html(ls)} for v, t, ls in secs]}

    # ---------------- Anrufliste (FRITZ!Box) ----------------
    def calls_cfg(self):
        return self.cfg.setdefault("calls", {"enabled": True, "last_id": 0})

    def _calls_tick(self):
        """Jede Minute die Anrufliste holen – nur mit FRITZ!Box."""
        if not self.stop.is_set():
            self.after(60 * 1000, self._calls_tick)
        self._calls_tick_once()

    def _calls_tick_once(self):
        """Sofort abrufen (Knopf „Aktualisieren“) – ohne einen zweiten Minutentakt zu starten."""
        if not self.calls_cfg().get("enabled", True) or not self.client or not self.is_fritz() or self._calls_busy:
            return
        self._calls_busy = True

        def job(c):
            try:
                self.results.put(("calls", {"calls": c.call_list()}))
            except Exception as e:  # noqa: BLE001
                self.results.put(("calls", {"error": str(e)}))
        self.jobs.put(("action", job))

    def _on_calls(self, res):
        import calls as CL
        self._calls_busy = False
        if res.get("error"):
            if res["error"] != self.call_err:
                log.info("Anrufliste: %s", res["error"])
            self.call_err = res["error"]
            return
        self.call_err = ""
        lst = res["calls"]
        self.call_data = lst
        cc = self.calls_cfg()
        top = max([c["id"] for c in lst] or [0])
        last = int(cc.get("last_id") or 0)
        if not last or top < last:  # erster Abruf (oder Liste geleert): nicht alles nachmelden
            cc["last_id"] = top
            self.save()
        else:
            new = CL.new_missed(lst, last)
            for c in new:
                ab = " · Nachricht auf dem Anrufbeantworter" if c["type"] == "tam" and c["message"] else (
                    " · AB hat abgenommen" if c["type"] == "tam" else "")
                self.log("Telefon", CL.who(c), f"verpasster Anruf{ab}")
                self.plugins.emit("call_missed", name=CL.who(c), number=c["number"])
            if new:
                if len(new) == 1:
                    c = new[0]
                    extra = " – Nachricht auf dem AB 📼" if c["type"] == "tam" and c["message"] else ""
                    self.notify("calls", f"📞 Verpasster Anruf: {CL.who(c)}",
                                f"{CL.when(c['ts'])}" + (f" · {CL.pretty_number(c['number'])}" if c["name"] and
                                                         c["number"] else "") + extra)
                else:
                    self.notify("calls", f"📞 {len(new)} verpasste Anrufe", ", ".join(CL.who(c) for c in new[-4:]))
            if top != last:
                cc["last_id"] = top
                self.save()
        if self.dash:
            self.dash.put("Personen", "calls:list", {"type": "calls", "items": lst[:3], "missed_today": sum(
                1 for c in lst if CL.is_missed(c) and c["ts"] >= time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))})
            self.dash.commit()

    def web_calls(self):
        import calls as CL
        return {"ok": True, "error": self.call_err,
                "items": [{"who": CL.who(c), "number": CL.pretty_number(c["number"]) if c["name"] else "",
                           "type": c["type"], "icon": CL.ICON.get(c["type"], "📞"), "label": CL.LABEL.get(c["type"], ""),
                           "when": CL.when(c["ts"]), "min": c["minutes"], "msg": c["message"],
                           "own": c["own"], "tel": c["number"]} for c in self.call_data[:40]]}

    def calls_dialog(self):
        if not self.is_fritz():
            messagebox.showinfo(APP, "Die Anrufliste gibt es nur mit einer FRITZ!Box.")
            return
        from pages import CallsDialog
        d = CallsDialog(self, self)
        self.wait_window(d)

    # ---------------- Heizungs-Check ----------------
    def heating_report(self, days=14, force=False):
        import heating as HC
        c = getattr(self, "_heat_cache", None)
        if c and not force and time.time() - c[0] < 600 and c[1] == days:
            return c[2]
        try:
            self.store.flush_series()
        except Exception:  # noqa: BLE001
            pass
        t1 = time.time()
        t0 = t1 - days * 86400
        names = self.entity_names()
        persons = [p["name"] for p in self.cfg.get("persons", []) if p.get("kind") != "item"]
        away = HC.away_mask(self.store.events("Anwesenheit", limit=5000), persons, t0, t1) if persons else None
        rooms = []
        st = self.store
        for d in self.data.get("smart") or []:
            if not d.get("hkr") or d.get("is_group"):
                continue
            a = d["ain"]
            r = HC.analyze_room(names.get(a) or d["name"], st.series("temp:" + a, t0, t1, HC.STEP),
                                st.series("soll:" + a, t0, t1, HC.STEP), st.series("hoff:" + a, t0, t1, HC.STEP),
                                st.series("win:" + a, t0, t1, HC.STEP), away, days)
            r["ain"] = a
            rooms.append(r)
        HC.compare_cooling(rooms)
        order = {"warn": 0, "tip": 1, "ok": 2, "info": 3}
        for r in rooms:
            r["findings"].sort(key=lambda x: order.get(x[0], 9))
        rooms.sort(key=lambda r: (min([order.get(x[0], 9) for x in r["findings"]] or [9]), r["name"].lower()))
        rep_ = {"days": days, "rooms": rooms, "summary": HC.summary(rooms), "persons": bool(persons), "ts": t1}
        self._heat_cache = (time.time(), days, rep_)
        return rep_

    def web_heating(self):
        r = self.heating_report()
        return {"ok": True, "days": r["days"], "summary": r["summary"], "persons": r["persons"],
                "rooms": [{"name": x["name"], "avg_ist": x["avg_ist"], "avg_soll": x["avg_soll"],
                           "heat_h_week": x["heat_h_week"], "heatup_min": x["heatup_min"],
                           "findings": [{"level": a, "text": b} for a, b in x["findings"]]} for x in r["rooms"]]}

    def heating_dialog(self):
        if not any(d.get("hkr") for d in self.data.get("smart") or []):
            messagebox.showinfo(APP, "Keine Heizkörperregler gefunden (FRITZ!DECT 301/302).")
            return
        from pages import HeatingDialog
        d = HeatingDialog(self, self)
        self.wait_window(d)

    # ---------------- Siri-Kurzbefehle ----------------
    def siri_token(self, user_id, renew=False):
        """Schlüssel eines Handy-Nutzers für Kurzbefehle (wird bei Bedarf angelegt)."""
        import siri
        for u in self.cfg.get("web_users") or []:
            if u["id"] == user_id:
                if renew or not u.get("siri"):
                    u["siri"] = siri.new_token()
                    self.save()
                    if renew:
                        self.log("App", u["name"], "Siri-Schlüssel erneuert – alter Kurzbefehl geht nicht mehr")
                return u["siri"]
        return ""

    def siri_url(self, user_id, ip=None):
        urls = self.web_urls()
        base = f"http://{ip}:{self.cfg.get('web_port', 8770)}" if ip else (urls[0] if urls else
                                                                          f"http://<IP>:{self.cfg.get('web_port', 8770)}")
        return f"{base}/api/siri?k={self.siri_token(user_id)}&t="

    def siri_user(self, token):
        import hmac as _hm
        if not token or len(token) < 16:
            return None
        for u in self.cfg.get("web_users") or []:
            if u.get("siri") and _hm.compare_digest(str(u["siri"]), str(token)):
                return u
        return None

    def siri_command(self, user, text):
        """Satz ausführen (Webserver-Thread → Hauptthread) und Antwort für Siri zurückgeben."""
        import siri
        import users as U

        def run():
            try:
                ans = siri.handle(self, user, text, lambda area, lvl=1: U.can(user, area, lvl))
            except Exception as e:  # noqa: BLE001
                log.exception("Siri")
                ans = f"Da ist etwas schiefgegangen: {e}"
            self.store.log("Sprache", user["name"], f"Siri: „{(text or '').strip()[:80]}“ → {ans[:120]}")
            return ans
        try:
            return self.main_call(run)
        except Exception as e:  # noqa: BLE001
            return f"Hauskern antwortet gerade nicht ({e})."

    def siri_dialog(self):
        if not self.cfg.get("web_users"):
            messagebox.showinfo(APP, "Für Siri braucht es zuerst einen Handy-Nutzer.\n\n"
                                     "Einstellungen → Handy-Zugriff → „Handy-Nutzer & Rechte“.")
            return
        from pages import SiriDialog
        d = SiriDialog(self, self)
        self.wait_window(d)

    # ---------------- Batterien ----------------
    def _battery_record(self, devs):
        import battery as B
        day = time.strftime("%Y-%m-%d")
        for d in devs:
            if d["is_group"] or d.get("battery") in (None, "") or not d["present"]:
                continue
            try:
                pct = float(d["battery"])
            except (TypeError, ValueError):
                continue
            last = self._bat_last.get(d["ain"])
            if last and last[0] == day and last[1] == pct:
                continue
            if last is None:  # nach dem Start: letzten gespeicherten Wert holen (für den Wechsel-Vergleich)
                h = self.store.battery_history(time.time() - 40 * 86400).get(d["ain"])
                last = (None, h[-1][1]) if h else None
            self.store.add_battery(d["ain"], pct)
            if last and pct - last[1] >= B.JUMP:
                self.log("Gerät", d["name"], f"Batterie gewechselt ({round(last[1])} % → {round(pct)} %)")
            new_day = not last or last[0] != day
            self._bat_last[d["ain"]] = (day, pct)
            self._bat_cache = (0, [])
            if new_day:
                self._battery_remind(d["ain"])

    def _battery_remind(self, ain):
        """Einmal pro Batterie-Füllung: rechtzeitig erinnern, bevor die FRITZ!Box „schwach“ meldet."""
        import battery as B
        it = next((x for x in self.battery_items() if x["ain"] == ain), None)
        if not it or it["low"] or it["days_left"] is None or it["days_left"] > B.REMIND_DAYS:
            return
        tag = str(it["changed"] or 0)
        warned = self.cfg.setdefault("battery_warned", {})
        if warned.get(ain) == tag:
            return
        warned[ain] = tag
        self.save()
        self.notify("smart", f"🔋 {it['name']}: Batterie bald leer", f"{it['left_text']} ({it['pct']} %) – "
                                                                     "Batterien schon mal besorgen.")

    def battery_items(self):
        import battery as B
        ts, items = self._bat_cache
        if time.time() - ts < 60:
            return items
        items = B.overview(self.data.get("smart") or [], self.store.battery_history(time.time() - 400 * 86400))
        self._bat_cache = (time.time(), items)
        return items

    def battery_dialog(self):
        from pages import BatteryDialog
        d = BatteryDialog(self, self)
        self.wait_window(d)

    def web_battery(self):
        out = []
        for b in self.battery_items():
            out.append({"name": b["name"], "pct": b["pct"], "low": b["low"], "left": b["left_text"],
                        "present": b["present"], "product": b["product"],
                        "changed": time.strftime("%d.%m.%Y", time.localtime(b["changed"])) if b["changed"] else ""})
        return out

    # ---------------- Filament-Lager ----------------
    def spools(self):
        return self.cfg.setdefault("spools", [])

    def fil_low_limit(self):
        try:
            return float(self.cfg.get("filament_low", 150))
        except (TypeError, ValueError):
            return 150.0

    def _fil_sync(self, serial, summ):
        import filament as FL
        units = summ.get("ams_units")
        if not summ.get("connected") or units is None:
            return
        sig = json.dumps([(u["id"], [(t["slot"], t["type"], t["color"], t["uuid"], t["grams"]) for t in u["trays"]])
                          for u in units])
        if self._fil_unknown.get(serial, (None,))[0] == sig:
            return
        changed, unknown = FL.sync_ams(self.spools(), serial, units)
        self._fil_unknown[serial] = (sig, unknown)
        if changed:
            self._fil_after_change()

    def _fil_after_change(self):
        import filament as FL
        for s_ in FL.check_low(self.spools(), self.fil_low_limit()):
            self.log("3D-Druck", s_["name"], f"Filament wird knapp: noch {FL.fmt_left(s_)}")
            self.notify("printer", f"🧵 Filament wird knapp: {s_['name']}", f"Noch {FL.fmt_left(s_)} – Nachschub besorgen.")
        self.save()

    def _fil_consume(self, serial, pid, state, summ):
        import filament as FL
        if not self.spools():
            return
        row = self.store.print_row(pid)
        fils = json.loads(row[9]) if row and row[9] else []
        if not fils:
            self.log("3D-Druck", summ.get("name", ""), "Filament-Lager: Verbrauch unbekannt – nichts abgezogen")
            return
        factor = 1.0 if state == "FINISH" else max(0.0, min(1.0, (self._plast_pct.get(serial) or 0) / 100))
        self._plast_pct.pop(serial, None)
        pr = self.printers.get(serial)
        units = pr.summary().get("ams_units", []) if pr else summ.get("ams_units", [])
        done, missing = FL.consume(self.spools(), serial, units, fils, factor)
        if done:
            self.log("3D-Druck", summ.get("name", ""), "Filament-Lager: " + ", ".join(
                f"{n} −{fnum(g, 0)} g" for n, g in done))
        if missing:
            self.log("3D-Druck", summ.get("name", ""), "Filament-Lager: keine passende Spule für " + ", ".join(missing))
        self._fil_after_change()

    def fil_units(self):
        out = {}
        for serial, pr in self.printers.items():
            try:
                out[serial] = pr.summary().get("ams_units", []) or []
            except Exception:  # noqa: BLE001
                out[serial] = []
        return out

    def fil_slots(self):
        """Alle AMS-Fächer zur Auswahl: [(key, Text)]."""
        import filament as FL
        names = {s_: p.name for s_, p in self.printers.items()}
        units = self.fil_units()
        out = []
        for serial, us in units.items():
            for u in us:
                for t in u["trays"]:
                    k = FL.slot_key(serial, u["id"], t["slot"])
                    inside = f" – drin: {t['type']} {FL.color_name(t['color'])}".rstrip() if not t["empty"] else " – leer"
                    out.append((k, FL.slot_label(k, names, units) + inside))
        return out

    def fil_slot_label(self, key):
        import filament as FL
        return FL.slot_label(key, {s_: p.name for s_, p in self.printers.items()}, self.fil_units()) if key else ""

    def fil_unknown(self):
        """Belegte AMS-Fächer ohne Spule im Lager: [(key, Text, tray)]."""
        import filament as FL
        out = []
        for serial, (_sig, unk) in self._fil_unknown.items():
            for k, t in unk:
                if not any(s_.get("slot") == k for s_ in self.spools()):
                    out.append((k, f"{self.fil_slot_label(k)}: {t['type']} {FL.color_name(t['color'])}".strip(), t))
        return out

    def fil_save(self, spool, gross=None, minus=None):
        """Spule anlegen/ändern. gross = nachgewogen (mit Spule), minus = Verbrauch abziehen."""
        import filament as FL
        sp = self.spools()
        old = next((x for x in sp if x["id"] == spool.get("id")), None)
        if spool.get("slot"):
            for o in sp:
                if o is not old and o.get("slot") == spool["slot"]:
                    o["slot"] = ""
        if old:
            old.update(spool)
            s_ = old
        else:
            s_ = FL.new_spool(**spool)
            sp.append(s_)
        if gross not in (None, ""):
            FL.weigh(s_, float(gross))
        if minus not in (None, ""):
            s_["left"] = round(max(0.0, float(s_["left"]) - float(minus)), 1)
            s_["used"] = round(float(s_.get("used") or 0) + float(minus), 1)
        s_["color"] = FL.norm_color(s_.get("color"))
        self._fil_unknown = {}  # beim nächsten Druckerupdate neu abgleichen
        self._fil_after_change()
        self.log("3D-Druck", s_["name"], f"Filament-Lager: gespeichert ({FL.fmt_left(s_)})")
        return s_

    def fil_delete(self, sid):
        sp = self.spools()
        s_ = next((x for x in sp if x["id"] == sid), None)
        if s_:
            sp.remove(s_)
            self.save()
            self.log("3D-Druck", s_["name"], "Filament-Lager: Spule entfernt")
        return bool(s_)

    def filament_dialog(self):
        from pages import FilamentStockDialog
        d = FilamentStockDialog(self, self)
        self.wait_window(d)

    # ---------------- Zeitraffer ----------------
    def tl_cfg(self):
        import timelapse
        return dict(timelapse.DEFAULTS, **{k: v for k, v in (self.cfg.get("timelapse") or {}).items()})

    def _make_timelapse(self):
        import timelapse
        from pages import media_dir
        work = os.path.join(_data_dir(), "zeitraffer-bilder")
        out = os.path.join(media_dir("video"), "Zeitraffer")
        m = timelapse.Manager(work, out, grab=lambda src: grab_frame(src, width=int(self.tl_cfg().get("width", 1280))),
                              ffmpeg=find_ffmpeg, done=lambda r: self.results.put(("timelapse_done", r)),
                              log=log.info)
        m.cleanup_orphans()
        return m

    def tl_mode(self, serial):
        return ((self.cfg.get("timelapse") or {}).get("modes") or {}).get(serial, "off")

    def tl_source(self, serial):
        """URL, von der die Bilder kommen: Druckerkamera oder eine eingerichtete Kamera."""
        src = ((self.cfg.get("timelapse") or {}).get("sources") or {}).get(serial, "printer")
        if src != "printer":
            e = self.camobjs.get(src)
            if e and (e.get("main") or e.get("sub")):
                return e.get("main") or e.get("sub")
        pr = self.printers.get(serial)
        return bambu_url(pr.ip, pr.code) if pr else ""

    def _tl_update(self, serial, summ):
        mode = self.tl_mode(serial)
        active = mode == "always" or (mode == "ask" and serial in self._tl_armed)
        if not active and serial not in self.tl.sessions:
            return
        self.tl.update(serial, summ.get("name", ""), summ, self.tl_source(serial), active, self.tl_cfg())

    def tl_arm(self, serial, on):
        """„Für diesen Druck aufnehmen“ an/aus. Aus während der Aufnahme = Video aus dem bisher Aufgenommenen."""
        pr = self.printers.get(serial)
        if not pr:
            raise RuntimeError("Drucker unbekannt")
        if on:
            self._tl_armed.add(serial)
            self.log("3D-Druck", pr.name, "Zeitraffer für diesen Druck eingeschaltet")
            return "🎬 Zeitraffer an – nimmt beim Drucken auf"
        self._tl_armed.discard(serial)
        if serial in self.tl.sessions:
            self.tl.finish(serial, "beendet", self.tl_cfg())
            return "🎬 Zeitraffer gestoppt – Video wird erstellt"
        return "🎬 Zeitraffer aus"

    def tl_state(self, serial):
        rec = self.tl.recording(serial)
        return {"mode": self.tl_mode(serial), "armed": serial in self._tl_armed,
                "rec": rec["frames"] if rec else None}

    def _on_timelapse_done(self, res):
        import timelapse
        self._tl_armed.discard(res["serial"])
        if res.get("video"):
            self.log("3D-Druck", res["name"], f"Zeitraffer fertig: {res['seconds']:g} s, "
                                              f"{timelapse.fmt_size(res['size'])}")
            self.notify("printer", f"🎬 Zeitraffer fertig – {res['name']}",
                        f"{timelapse.safe(res['file'])} · {res['seconds']:g} Sekunden")
        elif res.get("error"):
            self.log("3D-Druck", res["name"], f"Zeitraffer fehlgeschlagen: {res['error']}")
            self.notify("printer", "🎬 Zeitraffer fehlgeschlagen", res["error"][:160], warn=True)
        elif res.get("skipped"):
            self.log("3D-Druck", res["name"], f"Zeitraffer verworfen ({res['skipped']})")
        if res.get("removed"):
            self.log("3D-Druck", "Zeitraffer", f"{len(res['removed'])} alte Videos gelöscht (Speicherlimit)")

    def save_tl_cfg(self, c):
        self.cfg["timelapse"] = c
        self.save()
        try:
            self.tl.enforce_limits(self.tl_cfg())
        except OSError:
            pass

    def integrations_dialog(self):
        from pages import IntegrationsDialog
        d = IntegrationsDialog(self, self)
        self.wait_window(d)

    def vacation_dialog(self):
        from pages import VacationDialog
        d = VacationDialog(self, self)
        self.wait_window(d)

    def appliances_dialog(self):
        from pages import AppliancesDialog
        d = AppliancesDialog(self, self)
        self.wait_window(d)

    def clips_dialog(self):
        from pages import ClipsDialog
        d = ClipsDialog(self, self)
        self.wait_window(d)

    def timelapse_dialog(self):
        from pages import TimelapseDialog
        d = TimelapseDialog(self, self)
        self.wait_window(d)

    def web_timelapse(self):
        import timelapse
        vids = self.tl.videos()
        return {"ok": True, "videos": [{"name": v["name"], "size": timelapse.fmt_size(v["size"]), "ts": v["ts"]}
                                       for v in vids],
                "usage": timelapse.fmt_size(sum(v["size"] for v in vids)),
                "limit": f"{self.tl_cfg().get('max_gb', 5):g} GB".replace(".", ",")}

    def timelapse_path(self, name):
        v = next((x for x in self.tl.videos() if x["name"] == name), None)
        return v["path"] if v else None

    def timelapse_delete(self, name):
        def run():
            ok = self.tl.delete(name)
            if ok:
                self.log("3D-Druck", "Zeitraffer", f"„{name}“ gelöscht")
            return ok
        return self.main_call(run)

    # ---------------- Urlaubsmodus ----------------
    def vacation_cfg(self):
        import vacation as VA
        return VA.cfg(self.cfg.get("vacation"))

    def _location(self):
        w = self.cfg.get("weather") or {}
        try:
            return float(w["lat"]), float(w["lon"])
        except (KeyError, TypeError, ValueError):
            return None

    def _anyone_home(self):
        ids = {p["id"] for p in self.cfg.get("persons", []) if p.get("kind") != "item"}
        return any(self.presence.get(i, {}).get("state") == "home" for i in ids)

    def save_vacation_cfg(self, c):
        """Einstellungen speichern (ohne an/aus zu ändern)."""
        cur = self.vacation_cfg()
        for k in ("active", "since", "saved_soll"):
            c[k] = cur[k]
        self.cfg["vacation"] = c
        self.save()
        self._vac_t = 0
        self.results.put(("vacation_tick", None))

    def vacation_set(self, on, until=None, back=None, who="", reason=""):
        """Urlaubsmodus ein-/ausschalten. Thread-sicher (läuft im Haupt-Thread)."""
        def run():
            import vacation as VA
            c = self.vacation_cfg()
            if until is not None:
                c["until"] = until
            if back is not None:
                c["back"] = back
            if on and c["active"] and until is None and back is None:
                return f"🏖 Urlaubsmodus ist schon {VA.describe(c)}"
            if on:
                if VA.until_ts(c) and VA.until_ts(c) <= time.time():
                    raise ValueError("Das Rückkehrdatum liegt in der Vergangenheit")
                if not c["active"]:
                    c["since"] = time.time()
                    c["saved_soll"] = {d["ain"]: d["hkr"]["soll"] for d in self.data.get("smart", [])
                                       if d.get("hkr") and isinstance(d["hkr"].get("soll"), (int, float))}
                c["active"] = True
                self.cfg["vacation"] = c
                self.save()
                self._vac_applied, self._vac_heat_t, self._vac_t = {}, {}, 0
                txt = VA.describe(c)
                self.log("App", "Urlaubsmodus", f"eingeschaltet ({txt})" + (f" von {who}" if who else ""))
                self.notify("vacation", "🏖 Urlaubsmodus an", self._vac_summary(c))
                self._vacation_tick()
                self._vac_tile()
                return f"🏖 Urlaubsmodus {txt}"
            if not c["active"]:
                return "🏖 Urlaubsmodus ist schon aus"
            self._vacation_restore(c)
            c.update(active=False, saved_soll={})
            self.cfg["vacation"] = c
            self.save()
            self._vac_applied, self._vac_heat_t = {}, {}
            why = reason or (f"von {who}" if who else "")
            self.log("App", "Urlaubsmodus", "ausgeschaltet" + (f" ({why})" if why else ""))
            self.notify("vacation", "🏖 Urlaubsmodus aus", reason or "Willkommen zurück! 👋")
            self._vac_tile()
            return "🏖 Urlaubsmodus aus"
        return self.main_call(run)

    def _vac_summary(self, c):
        names = self.entity_names()
        bits = []
        if c["plugs"]:
            bits.append(f"Licht abends: {', '.join(names.get(a, a) for a in c['plugs'][:3])}"
                        + (" …" if len(c["plugs"]) > 3 else ""))
        if c["heat"]:
            bits.append(f"Heizung höchstens {fnum(c['heat_temp'])} °C")
        if c["cam_push"]:
            bits.append("Push bei Personen an der Kamera")
        if c["auto_end"]:
            bits.append("endet, sobald jemand heimkommt")
        return " · ".join(bits) or "nichts ausgewählt – unter Einstellungen festlegen"

    def _vacation_restore(self, c):
        """Beim Ausschalten: Heizung zurück, Lampen aus, wenn niemand da ist."""
        if c.get("heat"):
            for ain, soll in (c.get("saved_soll") or {}).items():
                d = self._dev(ain)
                if d and d.get("hkr") and isinstance(d["hkr"].get("soll"), (int, float)) and d["hkr"]["soll"] < soll:
                    self.jobs.put(("action", lambda cl, ain=ain, v=soll: cl.set_thermostat(ain, v)))
        if not self._anyone_home():
            for ain in c.get("plugs", []):
                d = self._dev(ain)
                if d and (d["switch"] if d["switch"] is not None else d.get("simpleonoff")):
                    self.jobs.put(("action", lambda cl, d=dict(d): cl.set_switch(d, False)))

    def _on_vacation_tick(self, _=None):
        self._vacation_tick(force=True)

    def _vacation_tick(self, force=False):
        import vacation as VA
        if self.cfg.get("satellite"):
            return
        c = self.vacation_cfg()
        if not c["active"]:
            return
        now = time.time()
        if not force and now - self._vac_t < 30:
            return
        self._vac_t = now
        u = VA.until_ts(c)
        if u and now >= u:
            self.vacation_set(False, reason="Rückkehrzeit erreicht – willkommen zurück! 👋")
            return
        if not self.data.get("smart"):
            return  # noch keine Gerätedaten
        loc = self._location()
        for ain in c["plugs"]:
            d = self._dev(ain)
            if not d or not d.get("present"):
                continue
            want = VA.desired(now, ain, c, loc)
            if self._vac_applied.get(ain) == want:
                continue
            self._vac_applied[ain] = want
            is_on = d["switch"] if d["switch"] is not None else d.get("simpleonoff")
            if bool(is_on) != want:
                self.jobs.put(("action", lambda cl, d=dict(d), on=want: cl.set_switch(d, on)))
                self.log("Gerät", d["name"], f"🏖 Urlaubsmodus: {'an' if want else 'aus'}")
        heat = VA.heat_phase(now, c)
        for d in self.data["smart"]:
            h = d.get("hkr")
            if not h or not d.get("present") or not isinstance(h.get("soll"), (int, float)):
                continue
            ain = d["ain"]
            if heat:
                if h["soll"] > float(c["heat_temp"]) + 0.4 and now - self._vac_heat_t.get(ain, 0) > 600:
                    self._vac_heat_t[ain] = now
                    self.jobs.put(("action", lambda cl, ain=ain, v=float(c["heat_temp"]): cl.set_thermostat(ain, v)))
            elif c["heat"] and ain in c["saved_soll"] and self._vac_heat_t.get(ain) != "warm":
                self._vac_heat_t[ain] = "warm"  # rechtzeitig vor der Rückkehr wieder aufheizen – einmal
                soll = c["saved_soll"][ain]
                if h["soll"] < soll:
                    self.jobs.put(("action", lambda cl, ain=ain, v=soll: cl.set_thermostat(ain, v)))
                    self.log("Gerät", d["name"], f"🏖 heizt vor der Rückkehr wieder auf {fnum(soll)} °C")

    def _vac_cam_alert(self, cid, what):
        """Person (oder Bewegung ohne Erkennung) an der Kamera, während Urlaub ist."""
        c = self.vacation_cfg()
        if not (c["active"] and c["cam_push"]) or self.cfg.get("satellite") or self._anyone_home():
            return
        now = time.time()
        if now - self._vac_cam_t.get(cid, 0) < 300:
            return
        self._vac_cam_t[cid] = now
        e = self.camobjs.get(cid) or {}
        url = e.get("main") or e.get("sub")
        img = (lambda url=url: grab_frame(url)) if url else None  # noqa: E731 – läuft im Push-Thread
        self.notify("vacation", f"🏖 {what} an „{e.get('cfg', {}).get('name', 'Kamera')}“",
                    "Ihr seid im Urlaub – bitte kurz nachsehen.", warn=True, image_fn=img)

    def _vac_tile(self):
        try:  # Handy sofort aktualisieren, nicht erst beim nächsten Takt
            self._web_state = self._build_web_state()
        except Exception:  # noqa: BLE001
            log.exception("Handy-Status")

    def web_vacation(self):
        import vacation as VA
        c = self.vacation_cfg()
        return {"active": c["active"], "text": VA.describe(c), "plugs": len(c["plugs"])}

    # ---------------- Waschmaschine & Co. ----------------
    def appliances(self):
        import appliances as AP
        return [AP.norm(a) for a in self.cfg.get("appliances", [])]

    def save_appliance(self, a):
        lst = [x for x in self.cfg.get("appliances", []) if x.get("id") != a["id"]]
        old = next((x for x in self.cfg.get("appliances", []) if x.get("id") == a["id"]), None)
        if old and old.get("ain") != a.get("ain"):
            self.appl_state.pop(a["id"], None)
        idx = next((i for i, x in enumerate(self.cfg.get("appliances", [])) if x.get("id") == a["id"]), len(lst))
        lst.insert(idx, a)
        self.cfg["appliances"] = lst
        self.save()

    def remove_appliance(self, aid):
        self.cfg["appliances"] = [x for x in self.cfg.get("appliances", []) if x.get("id") != aid]
        self.appl_state.pop(aid, None)
        self.save()

    def _appliances_update(self, devs, now=None):
        import appliances as AP
        if self.cfg.get("satellite"):
            return
        now = now or time.time()
        by_ain = {d["ain"]: d for d in devs if not d.get("is_group")}
        for a in self.appliances():
            if not a.get("enabled", True):
                continue
            d = by_ain.get(a.get("ain"))
            watt = d.get("power_w") if d and d.get("present") else None
            st, ev = AP.step(self.appl_state.get(a["id"]), a, watt, now)
            self.appl_state[a["id"]] = st
            if not ev or ev == "abort":
                continue
            title, text = AP.message(a, st, ev, self.cfg.get("price_ct"))
            if ev == "start":
                self.log("Gerät", a["name"], "Programm läuft")
            elif ev == "done":
                self.log("Gerät", a["name"], f"fertig – {text}")
                self.push("appliance", title, text)
                if self.cfg.get("notify", {}).get("appliance", True):
                    self.tray.notify(title, text, False)
            elif ev == "remind":
                self.push("appliance", title, text)

    def web_appliances(self):
        import appliances as AP
        now = time.time()
        out = []
        for a in self.appliances():
            if not a.get("enabled", True):
                continue
            state, sub = AP.status(a, self.appl_state.get(a["id"]), now)
            d = self._dev(a.get("ain"))
            out.append({"id": a["id"], "name": a["name"], "icon": AP.icon(a), "state": state, "sub": sub,
                        "watt": d.get("power_w") if d and d.get("present") else None})
        return out

    # ---------------- Kamera-Aufnahmen ----------------
    def clips_cfg(self):
        import clips
        return dict(clips.DEFAULTS, **(self.cfg.get("clips") or {}))

    def clips_dir(self):
        from pages import media_dir
        return media_dir("video")

    @staticmethod
    def _clips_active():
        """Laufende Aufnahmen – nur solange die Datei noch wächst (falls ffmpeg mal abstürzt)."""
        from camera import Recorder
        act = set()
        for p in list(Recorder.ACTIVE):
            try:
                if time.time() - os.path.getmtime(p) < 120:
                    act.add(p)
            except OSError:
                pass
        return act

    @staticmethod
    def _cam_file_name(name):
        return "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name).strip() or "Kamera"

    def clips_enforce(self):
        import clips
        removed = clips.enforce(self.clips_dir(), self.clips_cfg(), self._clips_active())
        if removed:
            self.log("Kamera", "Aufnahmen", f"{len(removed)} alte Aufnahmen gelöscht (Speicher-/Zeitlimit)")
        return removed

    def _clips_tick(self):
        if time.time() - getattr(self, "_clips_t", 0) < 3600:
            return
        self._clips_t = time.time()
        threading.Thread(target=self.clips_enforce, daemon=True, name="Aufnahmen aufräumen").start()

    def save_clips_cfg(self, c):
        self.cfg["clips"] = c
        self.save()
        threading.Thread(target=self.clips_enforce, daemon=True, name="Aufnahmen aufräumen").start()

    def clips_usage(self):
        import clips
        return sum(v["size"] for v in clips.list_clips(self.clips_dir(), self._clips_active()))

    def web_clips(self, cam_key=""):
        import clips
        import timelapse
        vids = clips.list_clips(self.clips_dir(), self._clips_active())
        total = sum(v["size"] for v in vids)
        title = ""
        if cam_key.startswith("cam:"):
            e = self.camobjs.get(cam_key[4:]) or {}
            nm = (e.get("cfg") or {}).get("name") or next(
                (c["name"] for c in self.cfg.get("cameras", []) if c.get("id") == cam_key[4:]), "")
            if nm:
                want = self._cam_file_name(nm)
                vids = [v for v in vids if v["cam"] == want]
                title = nm
        c = self.clips_cfg()
        lim = f"{c['max_gb']:g} GB".replace(".", ",")
        if c.get("keep_days"):
            lim += f" bzw. {int(c['keep_days'])} Tage"
        return {"ok": True, "cam": title,
                "videos": [{"name": v["name"], "size": timelapse.fmt_size(v["size"]), "ts": v["ts"],
                            "cam": v["cam"]} for v in vids[:200]],
                "count": len(vids), "usage": timelapse.fmt_size(sum(v["size"] for v in vids)),
                "total": timelapse.fmt_size(total), "limit": lim}

    def clip_path(self, name):
        import clips
        v = next((x for x in clips.list_clips(self.clips_dir(), self._clips_active()) if x["name"] == name), None)
        return v["path"] if v else None

    def clip_delete(self, name, user=""):
        import clips
        try:
            ok = clips.delete(self.clips_dir(), name, self._clips_active())
        except OSError:
            ok = False
        if ok:
            self.log("Kamera", "Aufnahmen", f"„{name}“ gelöscht" + (f" ({user})" if user else ""))
        return ok

    # ---------------- Gesundheit (Heimserver) ----------------
    def _health_tick(self, force=False):
        if not force and time.time() - self._health_t < 300:
            return
        self._health_t = time.time()

        def work():
            import health
            try:
                snap = health.snapshot(_data_dir())
            except Exception:  # noqa: BLE001
                log.exception("Gesundheit")
                return
            self.results.put(("health", snap))
        threading.Thread(target=work, daemon=True, name="Gesundheit").start()

    def _on_health(self, snap):
        import health
        self.health = snap
        now = time.time()
        for key, level, title, text in health.problems(snap):
            last = self._health_sent.get(key, 0)
            again = 6 * 3600 if level == "bad" else 24 * 3600
            if key == "power":
                again = 10 ** 9  # Unterspannung „seit Start“: einmal je Start melden
            if now - last >= again:
                self._health_sent[key] = now
                self.notify("health", title, text, warn=level == "bad")
                self.log("App", "Gesundheit", f"{title} – {text}")

    def web_health(self):
        import health
        h = self.health
        if not h:
            return None
        st = h.get("storage") or {}
        b = self.backup_cfg()
        return {"pi": h.get("pi"), "model": h.get("model"), "temp": h.get("temp"),
                "storage": st.get("kind", ""), "free_gb": round((st.get("free") or 0) / 1e9, 1),
                "used_pct": st.get("used_pct"), "uptime": health.fmt_uptime(h.get("uptime")),
                "disk": health.disk_text(h.get("disk")),
                "problems": [{"level": lv, "title": t, "text": x} for _, lv, t, x in health.problems(h)],
                "backup_last": b.get("last"), "backup_ok": b.get("last_ok", True)}

    def web_log(self, user, since=0, kind="", q="", limit=200):
        import users as U
        allowed = {k for k, a in LOG_AREA.items() if U.can(user, a, 1)}
        rows = self.store.events(limit=min(int(limit or 200), 500) * 2, search=str(q or "")[:60])
        out, kinds = [], {}
        for ts, k, name, text in rows:
            area = LOG_AREA.get(k, "system")
            if not U.can(user, area, 1) or (kind and k != kind):
                continue
            kinds[k] = kinds.get(k, 0) + 1
            out.append({"ts": ts, "kind": k, "name": name, "text": text})
            if len(out) >= int(limit or 200):
                break
        return {"ok": True, "entries": out, "kinds": sorted(k for k in LOG_AREA if k in allowed)}

    # ---------------- Alexa & Google Home (Matter-Bridge) ----------------
    def bridge_token(self):
        v = self.cfg.get("voice") or {}
        return v.get("token", "") if v.get("enabled") else ""

    def bridge_devices(self):
        import voice

        def run():
            self.voice_seen = time.time()
            return {"ok": True, "version": VERSION, "name": APP, "devices": voice.devices(self)}
        return self.main_call(run)

    def bridge_command(self, cid, cmd, value=None):
        import voice

        def run():
            try:
                return voice.command(self, cid, cmd, value)
            except (RuntimeError, ValueError, TypeError) as e:
                return False, str(e)
        return self.main_call(run)

    def voice_dialog(self):
        from pages import VoiceDialog
        d = VoiceDialog(self, self)
        self.wait_window(d)

    def setup_needed(self):
        return not self.cfg.get("web_users")

    def setup_create(self, name, pin):
        """Ersteinrichtung über das Handy (nur solange noch niemand eingerichtet ist)."""
        def run():
            if self.cfg.get("web_users"):
                return None
            self.create_first_user(name, pin)
            self.cfg["web_enabled"] = True
            self.save()
            return self.cfg["web_users"][0]
        return self.main_call(run)

    def wizard(self):
        from wizard import WizardDialog
        if self.state() == "withdrawn":
            self.deiconify()
        d = WizardDialog(self, self)
        self.wait_window(d)

    def apply_fritz_login(self, host, user, password):
        """Vom Assistenten: geprüfte Zugangsdaten übernehmen und verbinden."""
        changed = (host, user, password) != (self.cfg.get("host"), self.cfg.get("user"), self.password) or not self.client
        self.cfg.update(host=host, user=user)
        self.password = password
        self.cfg["password"] = protect(password)
        self.save()
        if changed:
            self.client = None
            self.hist.clear()
            self.jobs.put(("connect",))
        self.log("App", APP, f"FRITZ!Box {host} eingerichtet")

    def apply_generic_router(self, name=""):
        """Vom Assistenten/Einstellungen: anderen Router (kein FRITZ!Box) benutzen."""
        changed = self.router_kind() != "generic" or not self.client
        self.cfg.update(router="generic", router_name=name or self.cfg.get("router_name", ""))
        self.save()
        if changed:
            self.client = None
            self.hist.clear()
            self.jobs.put(("connect",))
        self._fritz_only_hints()
        self.log("App", APP, f"Router eingerichtet: {name or 'anderer Router'}")

    def set_no_router(self):
        self.cfg["router"] = "none"
        self.save()
        self.client = None

    def create_first_user(self, name, pin):
        import users
        u = users.new_user(name, hash_pin(pin), admin=True)
        self.save_users((self.cfg.get("web_users") or []) + [u])
        self.log("App", name, "Handy-Nutzer angelegt (Admin)")

    def enable_web(self):
        if not self.cfg.get("web_users"):
            return
        if not self.cfg.get("web_enabled") or not self.web:
            self.cfg["web_enabled"] = True
            self.save()
            self.start_web()

    def users_dialog(self, parent=None):
        from pages import UsersDialog
        d = UsersDialog(parent or self, self)
        self.wait_window(d)

    def save_users(self, lst):
        import users
        self.cfg["web_users"] = lst
        users.migrate(self.cfg)
        if not lst:
            self.cfg["web_pin"] = ""
        self.save()
        if self.cfg.get("web_enabled") and lst and not self.web:
            self.start_web()

    def cam_png(self, key):
        self._web_cam_demand = time.time()
        kind, _, ident = key.partition(":")
        e = self.camobjs.get(ident) if kind == "cam" else None
        if e and e.get("thumb") and e["thumb"].frame:
            return e["thumb"].frame
        v = self.camviews.get(key)
        if v and v.stream and v.stream.frame:
            return v.stream.frame
        return None

    def do_action(self, body, user=None):
        if str(body.get("key", "")).startswith("plug:"):
            ok, msg = self.plugins.tap(str(body["key"]))
            return ok, ("🧩 " + msg) if ok else msg
        holder = {"ev": threading.Event(), "res": (False, "Zeitüberschreitung")}
        self.results.put(("web_action", (body, holder, user)))
        holder["ev"].wait(6)
        return holder["res"]

    def _on_web_action(self, payload):
        body, holder = payload[0], payload[1]
        user = payload[2] if len(payload) > 2 else None
        try:
            holder["res"] = self._web_action(body)
            ok, msg = holder["res"]
            if ok and user and user.get("name") and len(self.cfg.get("web_users") or []) > 1 \
                    and not str(body.get("key", "")).startswith("sys:"):
                self.log("Handy", user["name"], str(msg)[:120])  # wer hat was gemacht
        except Exception as e:
            log.exception("Handy-Aktion %s", body)
            holder["res"] = (False, str(e))
        holder["ev"].set()

    def _web_action(self, b):
        key, action = str(b.get("key", "")), b.get("action")
        kind, _, ident = key.partition(":")
        if kind == "wol" and action == "wake":
            w = next((x for x in self.cfg.get("wol", []) if x["mac"].upper() == ident.upper()), None)
            if not w:
                return False, "Gerät unbekannt"
            msg = self.wake(w["mac"], w["name"])
            t = self.dash.tiles.get(key)
            if t:
                t.model["busy_until"] = time.time() + 120
            return True, msg
        if key == "sys:diag":
            return (True, "Diagnose läuft – dauert bis zu einer Minute") if self.run_diagnose("Handy") else \
                (True, "Diagnose läuft bereits")
        tile = self.dash.tiles.get(key)
        if kind == "dect" and tile:
            d = tile.model["dev"]
            if action == "toggle":
                self.tile_action(key, "toggle", tile.model)
                self.log("Gerät", d["name"], "über das Handy geschaltet")
                return True, f"{d['name']} geschaltet"
            if action == "level" and d.get("level") is not None:
                pct = max(0, min(100, int(float(b.get("value", 50)))))
                d["level"] = pct
                self.jobs.put(("action", lambda c, dev=dict(d): c.set_level(dev, pct) if pct else c.set_switch(dev, False)))
                return True, f"{d['name']}: {pct} %"
            if action == "key" and d.get("media"):
                k = str(b.get("k", ""))
                if k not in _media_keys() and k != "channel":
                    return False, "Unbekannte Taste"
                val = M_channel(b.get("value", "")) if k == "channel" else None
                if k == "channel" and not val:
                    return False, "Bitte eine Sendernummer eingeben"
                self.jobs.put(("action", lambda c, dev=dict(d), k=k, v=val: c.press(dev, k, v)))
                return True, f"{d['name']}: " + (f"Sender {val}" if val else _media_keys()[k])
            if action == "temp" and d["hkr"]:
                val = max(8.0, min(28.0, round(float(b.get("value", 20)) * 2) / 2))
                self.dash.pending_temp[d["ain"]] = val
                tile.redraw()
                ain = d["ain"]
                self.jobs.put(("action", lambda c: c.set_thermostat(ain, val)))
                return True, f"Soll {fnum(val)} °C"
        if kind == "scene":
            rule = next((r for r in self.cfg.get("automations", []) if r["id"] == ident), None)
            if rule:
                self.engine.trigger(rule, reason="Handy")
                return True, f"{rule['name']} gestartet"
        if kind == "printer":
            pr = self.printers.get(ident)
            if not pr or not pr.connected:
                return False, "Drucker nicht verbunden"
            if action == "pause":
                self._user_paused.add(ident)
                pr.pause()
            elif action == "resume":
                pr.resume()
            elif action == "stop":
                pr.stop_print()
                self.log("Drucker", pr.name, "Druck über das Handy abgebrochen")
            elif action == "light":
                pr.set_light(bool(b.get("value")))
            elif action == "timelapse":
                return True, self.tl_arm(ident, bool(b.get("value")))
            else:
                return False, "unbekannt"
            return True, "OK"
        if kind == "guest":
            try:
                if action == "guest":
                    hours = b.get("hours")
                    return True, self.set_guest(bool(b.get("value")), None if hours is None else float(hours), "Handy")
                if action == "guest_pw":
                    pw = self.new_guest_password()
                    return True, f"Neues Passwort: {pw}"
            except (RuntimeError, ValueError) as e:
                return False, str(e)
            return False, "unbekannt"
        if kind == "wlan":
            w = next((x for x in self.data["wlan"] if self._wkey(x) == ident), None)
            if not w:
                return False, "WLAN nicht gefunden"
            idx, en, box = w["index"], bool(b.get("value")), w.get("box", "main")
            cl = None if box == "main" else self.extra_clients.get(box)
            if box != "main" and not cl:
                return False, "zweite FRITZ!Box nicht verbunden"
            self.jobs.put(("action", (lambda c: c.set_wlan(idx, en)) if box == "main" else
                           (lambda c: cl.set_wlan(idx, en)), "full"))
            self.log("Netzwerk", w["ssid"], f"WLAN über das Handy {'ein' if en else 'aus'}geschaltet")
            return True, f"WLAN {'an' if en else 'aus'}"
        if kind == "cam":
            e = self.camobjs.get(ident)
            cam = e and e.get("onvif")
            if not cam or not cam.has_ptz:
                return False, "Kamera kann nicht gesteuert werden"

            def ptz():
                try:
                    if action == "ptz":
                        cam.move(float(b.get("x", 0)) * 0.5, float(b.get("y", 0)) * 0.5)
                        time.sleep(0.45)
                        cam.stop()
                    elif action == "zoom":
                        cam.move(0, 0, float(b.get("z", 0)) * 0.5)
                        time.sleep(0.45)
                        cam.stop()
                    elif action == "preset":
                        cam.goto_preset(str(b.get("value")))
                except Exception:
                    log.exception("PTZ vom Handy")
            threading.Thread(target=ptz, daemon=True).start()
            return True, "OK"
        return False, "Aktion nicht möglich"

    def _build_web_state(self):
        hidden = set(self.cfg.get("hidden_tiles", []))
        name = self.dash.name_for
        import providers as _prov
        st = {"persons": [], "agenda": [], "scenes": [], "cams": [], "printers": [], "smart": [], "wlan": [],
              "energy": None, "net": {}, "weather": self.weather_view(),
              "guest": self.web_guest(),
              "health": self.web_health(),
              "devices": [{k: v for k, v in d.items() if k in ("key", "name", "category", "online", "sub", "children",
                                                                "radio")} for d in self.device_states()],
              "wol": [{"key": "wol:" + w["mac"].upper(), "name": w["name"],
                       "online": bool((self.dash.tiles.get("wol:" + w["mac"].upper()) or None) and
                                      self.dash.tiles["wol:" + w["mac"].upper()].model.get("online")),
                       "busy": bool((self.dash.tiles.get("wol:" + w["mac"].upper()) or None) and
                                    self.dash.tiles["wol:" + w["mac"].upper()].model.get("busy_until", 0) > time.time())}
                      for w in self.cfg.get("wol", [])],
              "diag": {"running": self.diag_running, "progress": self.diag_progress, "probe": PROBE,
                       "time_text": time.strftime("%d.%m. %H:%M", time.localtime(self.diag_time)) if self.diag_time else "",
                       "summary": diagnose.summary(self.diag_results) if self.diag_results else None}}
        for k, t in self.dash.tiles.items():
            if k in hidden:
                continue
            m = t.model
            typ = m.get("type")
            if typ == "person":
                st["persons"].append({"key": k, "name": name(k, m["name"]), "home": m["home"], "since": m.get("since", ""),
                                      "item": m.get("item", False)})
            elif typ == "agenda":
                st["agenda"] = m.get("events", [])[:10]
            elif typ == "scene":
                st["scenes"].append({"key": k, "name": name(k, m["name"])})
            elif typ == "camera":
                e = self.camobjs.get(k[4:], {})
                st["cams"].append({"key": k, "name": name(k, m["name"]), "status": m.get("status", ""),
                                   "ptz": bool(m.get("ptz")), "presets": e.get("presets", [])})
            elif typ == "printer":
                p = m["p"]
                temps = []
                for i, (cur, tgt) in enumerate(p["nozzles"]):
                    lbl = "Düse" if len(p["nozzles"]) == 1 else ("Düse L" if i == 0 else "Düse R")
                    temps.append((lbl, f"{round(cur)}°" + (f" / {round(tgt)}°" if tgt else "")))
                if p["bed"] is not None:
                    temps.append(("Bett", f"{round(p['bed'])}°" + (f" / {round(p['bed_target'])}°" if p["bed_target"] else "")))
                if p["chamber"] is not None:
                    temps.append(("Kammer", f"{round(p['chamber'])}°"))
                eta = ""
                if p["state"] in ("RUNNING", "PREPARE", "PAUSE") and p["remaining_min"]:
                    eta = "fertig ca. " + time.strftime("%H:%M", time.localtime(time.time() + int(p["remaining_min"]) * 60))
                st["printers"].append({"key": k, "name": name(k, p["name"]), "state": p["state"],
                                       "state_text": p["state_text"], "connected": p["connected"], "error": p["error"],
                                       "percent": p["percent"] if isinstance(p["percent"], (int, float)) else 0,
                                       "eta": eta, "layer": p["layer"], "layers": p["layers"], "file": p["file"],
                                       "temps": temps, "light": p["light"], "ams": p["ams"][:16],
                                       "ams_units": [dict(u, hum=bambu.humidity_text(u)) for u in p.get("ams_units", [])],
                                       "cost": p.get("cost_text", ""), "tl": p.get("tl"),
                                       "ai": printwatch.xcam_text(p.get("xcam")),
                                       "ai_off": bool(p.get("xcam") and p["xcam"].get("on") is False),
                                       "spaghetti": bool(printwatch.events(p.get("print_error"), p.get("hms")))})
            elif typ == "dect":
                d = m["dev"]
                on = d["switch"] if d["switch"] is not None else d["simpleonoff"]
                soll = d["hkr"]["soll"] if d["hkr"] else None
                pend = self.dash.pending_temp.get(d["ain"])
                if pend is not None:
                    soll = pend
                st["smart"].append({
                    "key": k, "name": name(k, d["name"]), "kind": d["kind"], "present": d["present"],
                    "switchable": on is not None or d["is_group"], "on": bool(on), "power": d["power_w"],
                    "energy": d["energy_kwh"], "temp": d["temp"], "thermostat": bool(d["hkr"]),
                    "ist": d["hkr"]["ist"] if d["hkr"] and isinstance(d["hkr"]["ist"], float) else None,
                    "soll": soll if isinstance(soll, float) else None,
                    "soll_txt": (f"{fnum(soll)} °C" if isinstance(soll, float) else (soll or "–")),
                    "window": bool(d["hkr"] and d["hkr"]["window_open"]), "battery_low": d["batterylow"],
                    "level": d.get("level"), "vendor": d.get("vendor") if d.get("vendor") != "AVM" else None,
                    "sensor_txt": _prov.sensor_text(d.get("sensor"))[0] if d.get("sensor") else "",
                    "sensor_mood": _prov.sensor_text(d.get("sensor"))[1] if d.get("sensor") else "",
                    "media": d.get("media")})
            elif typ == "energy":
                st["energy"] = {"power": m["power"], "count": m["count"], "month_eur": m.get("month_eur"),
                                "prog_eur": m.get("prog_eur"), "over_budget": m.get("over_budget")}
            elif typ == "internet":
                st["net"].update(online=bool(m.get("online")), down=m.get("down"), up=m.get("up"))
            elif typ == "hosts":
                st["net"]["hosts"] = m["online"]
            elif typ == "wlan":
                w = m["wlan"]
                st["wlan"].append({"key": "wlan:" + self._wkey(w), "ssid": w["ssid"], "band": w["band"],
                                   "box": w.get("box_name", ""), "enabled": w["enabled"], "clients": w["clients"]})
        st["smart"].sort(key=lambda x: (not x["present"], x["name"].lower()))
        try:
            if any(d.get("hkr") for d in self.data.get("smart") or []):
                st["heating"] = True
            if self.call_data or self.call_err:
                import calls as CL
                t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
                st["calls"] = {"missed_today": sum(1 for c in self.call_data if CL.is_missed(c) and c["ts"] >= t0),
                               "last": ({"who": CL.who(self.call_data[0]), "when": CL.when(self.call_data[0]["ts"]),
                                         "icon": CL.ICON.get(self.call_data[0]["type"], "📞")} if self.call_data else None),
                               "error": bool(self.call_err)}
            bats = self.web_battery()
            if bats:
                st["battery"] = {"count": len(bats), "low": sum(1 for b in bats if b["low"]), "worst": bats[0]}
        except Exception:
            log.exception("Batterien (Handy)")
        if self.printers or self.spools():
            import filament as FL
            st["filament"] = FL.summary(self.spools(), self.fil_low_limit())
        if self.plugins.tiles:
            st["plugins"] = sorted(dict(self.plugins.tiles).values(), key=lambda t: (t["plugin"], t["key"]))
        import lists as LS
        ls = self.lists()
        st["lists"] = {k: {"name": v["name"], "icon": v["icon"], "open": len(LS.open_items(v)),
                           "top": [i["text"] for i in LS.open_items(v)][:4]} for k, v in ls.items()}
        st["vacation"] = self.web_vacation()
        st["appliances"] = self.web_appliances()
        st["updated"] = time.time()
        st["version"] = VERSION
        return st

    def log_automation(self, text):
        self.log("Automation", "", text)

    def _on_logbook(self, _):
        if self.page == "logbook" and not self._log_refresh_pending:
            self._log_refresh_pending = True

            def go():
                self._log_refresh_pending = False
                self.tab_logbook.refresh()
            self.after(400, go)

    # ---------------- Kameras ----------------
    def start_cameras(self):
        for w in self.watchers.values():
            w.stop()
        self.watchers, self.motion = {}, {}
        for e in self.camobjs.values():
            if e.get("thumb"):
                e["thumb"].stop()
        self.camobjs = {}
        for c in self.cfg.get("cameras", []):
            self.camobjs[c["id"]] = {"cfg": c, "onvif": None, "main": None, "sub": None, "status": "verbinde …",
                                     "thumb": None, "img": None, "card": None, "last_no": -1, "last_frame": 0,
                                     "thumb_started": 0}
            threading.Thread(target=self._cam_connect, args=(c["id"],), daemon=True).start()
        keys = ["cam:" + k for k in self.camobjs]
        for k in keys:
            self._cam_tile(k[4:])
        self.dash.remove_missing("Kameras", "cam:", keys)
        self.dash.commit()
        self.tab_cameras.refresh()

    def _cam_pw(self, c):
        return unprotect(c.get("pw", "")) or c.get("pw_plain", "")

    def _cam_connect(self, cid):
        e = self.camobjs.get(cid)
        if not e:
            return {"main": None, "sub": None, "onvif": None, "error": "unbekannte Kamera"}
        c, pw = e["cfg"], self._cam_pw(e["cfg"])
        res = {"onvif": None, "main": None, "sub": None, "error": ""}
        manual = c.get("rtsp", "").strip()
        if manual and "@" not in manual and c.get("user"):
            manual = with_credentials(manual, c.get("user"), pw)
        if c.get("host") and c.get("brand") != "rtsp":
            try:
                cam = OnvifCamera(c["host"], c.get("port", 80), c.get("user", ""), pw).connect()
                res["onvif"] = cam
                try:
                    res["presets"] = cam.presets() if cam.has_ptz else []
                except OnvifError:
                    res["presets"] = []
                res["main"] = manual or cam.stream_url(False)
                res["sub"] = manual or cam.stream_url(True)
            except OnvifError as ex:
                res["error"] = str(ex)
        if not res["main"] and manual:
            res["main"] = res["sub"] = manual
            res["error"] = ""
        if c.get("brand") == "tapo" and c.get("host"):
            # Tapo: feste Stream-Adressen – funktionieren auch, wenn ONVIF zickt; stream2 schont das WLAN
            base = f"rtsp://{c['host']}:554/"
            if not res["main"]:
                res["main"] = with_credentials(base + "stream1", c.get("user", ""), pw)
                res["error"] = ""
            if not manual:
                res["sub"] = with_credentials(base + "stream2", c.get("user", ""), pw)
        self.results.put(("cam_ready", (cid, res)))
        return res

    def _on_cam_ready(self, payload):
        cid, res = payload
        e = self.camobjs.get(cid)
        if not e:
            return
        e.update(onvif=res["onvif"], main=res["main"], sub=res["sub"], presets=res.get("presets", []))
        name = e["cfg"]["name"]
        mo = e["cfg"].get("motion") or {}
        if mo.get("enabled") and cid not in self.watchers and (res["main"] or res["onvif"]):
            self.watchers[cid] = MotionWatcher(
                cid, name, lambda e=e: e.get("onvif"), lambda e=e: e.get("sub") or e.get("main"),
                mo.get("mode", "auto"), mo.get("sens", "mittel"),
                on_change=lambda c, on, src: self.results.put(("motion", (c, on, src))),
                log=log.info).start()
            self.motion[cid] = False
        if res["main"]:
            e["status"] = "verbunden"
            info = res["onvif"].info if res["onvif"] else {}
            model = " ".join(x for x in (info.get("Manufacturer"), info.get("Model")) if x)
            self.log("Kamera", name, "verbunden" + (f" ({model})" if model else ""))
        else:
            e["status"] = "⚠ " + (res["error"] or "keine Verbindung")
            self.log("Kamera", name, e["status"])
            log.warning("Kamera %s (%s:%s, %s): %s", name, e["cfg"].get("host"), e["cfg"].get("port"),
                        e["cfg"].get("brand", "?"), res["error"])
        self._cam_tile(cid)
        self.tab_cameras.refresh()

    def _on_motion(self, payload):
        cid, on, src = payload
        e = self.camobjs.get(cid)
        if not e or cid not in self.watchers:
            return
        self.motion[cid] = on
        if on and self._motion_log.ok(cid):
            self.log("Kamera", e["cfg"]["name"], f"Bewegung erkannt ({src})")
        if on and self.vision_on(cid):
            self._vision_check(cid, 0)
        elif on:
            self._vac_cam_alert(cid, "Bewegung")
        self._cam_tile(cid)
        self._engine_update()

    def motion_status(self, cid):
        w = self.watchers.get(cid)
        if not w:
            return ""
        if w.error and not w.source:
            return f"Bewegung: ⚠ {w.error}"
        return ("● Bewegung" if self.motion.get(cid) else "Bewegungserkennung an") + (f" ({w.source})" if w.source else "")

    def _cam_tile(self, cid):
        e = self.camobjs.get(cid)
        if not e:
            return
        hint = "" if find_ffmpeg() else "Klick zum Einrichten der Livebilder"
        ptz = bool(e["onvif"] and e["onvif"].has_ptz)
        status = "● Bewegung" if self.motion.get(cid) else e["status"]
        sn = self.seen.get(cid)
        if sn and time.time() - sn["ts"] < 120:
            import vision as _v
            status = "● " + _v.describe(sn["found"]) + " erkannt"
        self.dash.put("Kameras", "cam:" + cid, {"type": "camera", "name": e["cfg"]["name"], "img": e["img"],
                                                "status": status, "ptz": ptz, "hint": hint}, cols=2, rows=2)

    def _cam_tick(self):
        try:
            self._cam_tick_inner()
        except Exception:
            log.exception("Kamera-Vorschau")
        if not self.stop.is_set():
            self.after(1000, self._cam_tick)

    def _cam_tick_inner(self):
        visible = (self.state() != "withdrawn" and self.page in ("home", "cameras")) or \
            time.time() - self._web_cam_demand < 20
        have_ff = bool(find_ffmpeg()) if self.camobjs else False
        changed = False
        now = time.time()
        for cid, e in self.camobjs.items():
            if "thumb" not in e:  # unvollständiger Eintrag – überspringen statt alles anzuhalten
                continue
            key = "cam:" + cid
            want = visible and have_ff and e.get("sub") and key not in self.camviews
            th = e["thumb"]
            if want and (th is None or (not th.running and now - e["thumb_started"] > 15)):
                if th:
                    th.stop()
                e["thumb"] = CameraStream(e["sub"], fps=1, width=464)
                e["thumb_started"] = now
                e["thumb"].start()
            elif not want and th:
                th.stop()
                e["thumb"] = None
            th = e["thumb"]
            if th and th.frame and th.frame_no != e["last_no"]:
                e["last_no"] = th.frame_no
                try:
                    img = tk.PhotoImage(data=base64.b64encode(th.frame).decode())
                    if img.height() > 290:
                        img = img.subsample(2)
                    e["img"] = img
                    e["card"] = img.zoom(2).subsample(3) if img.width() > 320 else img
                    e["last_frame"] = now
                    e["status"] = "● Live"
                    changed = True
                except tk.TclError:
                    pass
            elif th and not th.running and th.error and e["status"] != "⚠ " + th.error:
                e["status"] = "⚠ " + th.error
                log.warning("Kamera-Vorschau %s: %s", e["cfg"]["name"], th.error)
                changed = True
            elif e["status"] == "● Live" and now - e["last_frame"] > 20:
                e["status"] = "verbunden"
                changed = True
            if changed:
                self._cam_tile(cid)
        if changed and self.page == "cameras":
            self.tab_cameras.refresh()

    def camera_list(self):
        out = []
        for cid, e in self.camobjs.items():
            out.append(("cam:" + cid, self.dash.name_for("cam:" + cid, e["cfg"]["name"]), e["status"], e["card"],
                        bool(e["onvif"] and e["onvif"].has_ptz)))
        for serial, pr in self.printers.items():
            out.append(("printer:" + serial, pr.name, "Drucker-Kamera", None, False))
        return out

    def open_camera(self, key):
        if key in self.camviews:
            self.camviews[key].lift()
            return
        kind, _, ident = key.partition(":")
        if kind == "printer":
            pr = self.printers.get(ident)
            if not pr:
                return
            view = CamView(self, key, pr.name, lambda sub: bambu_url(pr.ip, pr.code),
                           on_close=lambda: self.camviews.pop(key, None))
        else:
            e = self.camobjs.get(ident)
            if not e:
                return

            def url(sub, e=e, ident=ident):
                if e["main"]:
                    return e["sub"] if sub else e["main"]
                res = self._cam_connect(ident)  # erneut versuchen (läuft im Hintergrund-Thread der Ansicht)
                if not res["main"]:
                    raise OnvifError(res["error"] or "keine Verbindung")
                return res["sub"] if sub else res["main"]
            if e["thumb"]:
                e["thumb"].stop()
                e["thumb"] = None
            view = CamView(self, key, self.dash.name_for(key, e["cfg"]["name"]), url, onvif=e["onvif"],
                           on_close=lambda: self.camviews.pop(key, None))
        self.camviews[key] = view

    def camera_dialog(self, cid=None, prefill=None):
        cur = next((c for c in self.cfg.get("cameras", []) if c["id"] == cid), None) if cid else (prefill or None)
        d = CameraDialog(self, cur, self._cam_pw(cur) if cur and cid else "")
        self.wait_window(d)
        r = d.result
        if not r:
            return
        self.save_camera(r)

    def camera_password(self, entry):
        return unprotect(entry.get("pw", "")) or entry.get("pw_plain", "")

    def save_camera(self, r):
        entry = {"id": r["id"], "name": r["name"], "host": r["host"], "port": r["port"], "user": r["user"],
                 "rtsp": r["rtsp"], "brand": r["brand"], "motion": r.get("motion", {})}
        enc = protect(r["pw"])
        if enc:
            entry["pw"] = enc
        elif r["pw"]:
            entry["pw_plain"] = r["pw"]
        lst = [c for c in self.cfg.get("cameras", []) if c["id"] != r["id"]]
        lst.append(entry)
        self.cfg["cameras"] = lst
        self.save()
        self.log("Kamera", r["name"], "gespeichert")
        self.start_cameras()

    def remove_camera(self, cid):
        c = next((c for c in self.cfg.get("cameras", []) if c["id"] == cid), None)
        if c and messagebox.askyesno(APP, f"Kamera „{c['name']}“ entfernen?"):
            self.cfg["cameras"] = [x for x in self.cfg["cameras"] if x["id"] != cid]
            self.save()
            self.start_cameras()

    def discover_cameras(self):
        d = DiscoverDialog(self)
        self.wait_window(d)
        if d.result:
            self.camera_dialog(prefill=d.result)

    # ---------------- Automationen ----------------
    def snapshot(self):
        smart = self.data["smart"]
        sw = {d["ain"]: (d["switch"] if d["switch"] is not None else d["simpleonoff"]) for d in smart
              if d["present"] and (d["switch"] is not None or d["simpleonoff"] is not None)}
        temps = {}
        for d in smart:
            if d["hkr"] and isinstance(d["hkr"]["ist"], float):
                temps[d["ain"]] = d["hkr"]["ist"]
            elif d["temp"] is not None:
                temps[d["ain"]] = d["temp"]
        if self.weather:
            temps["weather"] = self.weather["current"]["temp"]
        return {"switch": sw, "power": {d["ain"]: d["power_w"] for d in smart if d["power_w"] is not None},
                "temp": temps, "printer": dict(self._pstate),
                "ping": {h: ("up" if v == "ok" else "down") for h, v in self._ping_state.items()},
                "internet": None if self._prev_online is None else ("up" if self._prev_online else "down"),
                "presence": self.presence_snapshot(), "motion": dict(self.motion),
                "seen": {c: (v["ts"], tuple(sorted(v["found"]))) for c, v in self.seen.items()},
                "weather": ({"raining": self.weather["current"]["raining"],
                             "rain_soon": self.weather["current"]["rain_soon"]} if self.weather else {}),
                "sensor": {d["ain"]: {k: (v[0] if k == "button" and v else v) for k, v in d["sensor"].items()}
                           for d in smart if d.get("sensor") and d.get("present", True)}}

    def _engine_update(self):
        if self.cfg.get("satellite"):
            return  # der Heimserver führt die Automationen aus
        try:
            snap = self.snapshot()
            self.engine.update(snap)
            if self.plugins.active:
                self.plugins.on_snapshot(snap, {p["id"]: p["name"] for p in self.cfg.get("persons", [])})
        except Exception as e:
            log.exception("Automation-Prüfung")
            self._on_error(f"Automation: {e}")

    def _engine_tick(self):
        try:
            if not self.cfg.get("satellite"):
                self.engine.tick()
        except Exception:
            pass
        if not self.stop.is_set():
            self.after(20000, self._engine_tick)

    def _dev(self, ain):
        return next((d for d in self.data["smart"] if d["ain"] == ain), None)

    def run_action(self, a):
        """Wird aus dem Automations-Thread aufgerufen."""
        typ = a.get("type")
        names = self.entity_names()
        if typ == "switch":
            d = self._dev(a.get("ain"))
            if not d:
                raise RuntimeError("Gerät nicht gefunden")
            mode = a.get("mode", "toggle")
            if mode == "toggle":
                self.jobs.put(("action", lambda c, d=dict(d): c.toggle(d)))
            else:
                self.jobs.put(("action", lambda c, d=dict(d), on=(mode == "on"): c.set_switch(d, on)))
            return f"{d['name']} {dict(on='an', off='aus', toggle='umgeschaltet')[mode]}"
        if typ == "level":
            d = self._dev(a.get("ain"))
            if not d:
                raise RuntimeError("Lampe nicht gefunden")
            pct = max(0, min(100, int(float(a.get("value", 50)))))
            self.jobs.put(("action", lambda c, d=dict(d), pct=pct: c.set_level(d, pct) if pct else c.set_switch(d, False)))
            return f"{d['name']} auf {pct} %"
        if typ == "media":
            d = self._dev(a.get("ain"))
            if not d or not d.get("media"):
                raise RuntimeError("Fernseher / TV-Box nicht gefunden")
            k = a.get("key") or "pause"
            val = M_channel(a.get("value")) if k == "channel" else None
            if k == "channel" and not val:
                raise RuntimeError("Keine Sendernummer angegeben")
            self.jobs.put(("action", lambda c, d=dict(d), k=k, v=val: c.press(d, k, v)))
            return f"{d['name']}: " + (f"Sender {val}" if val else _media_keys().get(k, k))
        if typ == "thermostat":
            ain, val = a.get("ain"), float(a.get("value", 20))
            self.jobs.put(("action", lambda c: c.set_thermostat(ain, val)))
            return f"{names.get(ain, ain)} auf {fnum(val)} °C"
        if typ == "wlan":
            raw, on = a.get("index", 1), a.get("mode") == "on"
            if isinstance(raw, str) and ":" in raw:
                box, idx = raw.rsplit(":", 1)
                cl = self.extra_clients.get(box)
                if not cl:
                    raise RuntimeError("zweite FRITZ!Box nicht verbunden")
                idx = int(idx)
                self.jobs.put(("action", lambda c: cl.set_wlan(idx, on), "full"))
            else:
                idx = int(raw)
                self.jobs.put(("action", lambda c: c.set_wlan(idx, on), "full"))
            return f"WLAN {names.get(raw, raw)} {'an' if on else 'aus'}"
        if typ == "notify":
            self.tray.notify(APP, a.get("text", ""))
            img = None
            e = self.camobjs.get(a.get("cam") or "")
            if e:
                def img(e=e):
                    th = e.get("thumb")
                    if th and th.frame:
                        return th.frame
                    return grab_frame(e["main"]) if e.get("main") else None
            self.push("automation", a.get("text", "") or APP, "", False, img)
            return "Hinweis gezeigt"
        if typ == "printer_light":
            pr = self.printers.get(a.get("serial"))
            if not pr or not pr.connected:
                raise RuntimeError("Drucker nicht verbunden")
            pr.set_light(a.get("mode") == "on")
            return f"Licht {pr.name} {'an' if a.get('mode') == 'on' else 'aus'}"
        if typ == "camera_preset":
            e = self.camobjs.get(a.get("cam"))
            if not e or not e["onvif"]:
                raise RuntimeError("Kamera nicht verbunden")
            want = str(a.get("preset", "")).strip().lower()
            p = next((x for x in e["onvif"].presets() if x["name"].lower() == want or x["token"] == want), None)
            if not p:
                raise RuntimeError(f"Position „{a.get('preset')}“ nicht gefunden")
            e["onvif"].goto_preset(p["token"])
            return f"{e['cfg']['name']} → {p['name']}"
        if typ == "wol":
            mac = a.get("mac", "")
            return self.wake(mac, names.get(mac, mac))
        if typ == "record":
            e = self.camobjs.get(a.get("cam"))
            if not e or not e.get("main"):
                raise RuntimeError("Kamera nicht verbunden")
            secs = max(5.0, min(600.0, float(a.get("seconds", 30) or 30)))
            from camera import Recorder
            from pages import media_dir
            rec = Recorder(e["main"], media_dir("video"), e["cfg"]["name"])
            path = rec.start()
            def _done(rec=rec):
                rec.stop()
                try:
                    self.clips_enforce()
                except Exception:  # noqa: BLE001
                    log.exception("Aufnahmen aufräumen")
            threading.Timer(secs, _done).start()
            self.log("Kamera", e["cfg"]["name"], f"Aufnahme {secs:g} s → {os.path.basename(path)}")
            return f"{e['cfg']['name']} nimmt {secs:g} s auf"
        return ""

    def automations_changed(self, save=True):
        if save:
            self.save()
        keys = []
        for r in self.cfg.get("automations", []):
            if r.get("trigger", {}).get("type") == "manual" and r.get("enabled", True):
                k = "scene:" + r["id"]
                keys.append(k)
                self.dash.put("Szenen", k, {"type": "scene", "name": r["name"], "count": len(r.get("actions", [])),
                                            "id": r["id"]})
        self.dash.remove_missing("Szenen", "scene:", keys)
        self.dash.commit()
        self.tab_automations.refresh()

    def printer_plug(self, serial):
        pc = next((p for p in self.cfg.get("printers", []) if p["serial"].strip() == serial), {})
        return pc.get("plug_ain")

    def entities(self):
        smart = [d for d in self.data["smart"]]
        return {
            "switches": [(d["ain"], d["name"]) for d in smart if d["switch"] is not None or d["simpleonoff"] is not None],
            "power": [(d["ain"], d["name"]) for d in smart if d["power_w"] is not None and not d["is_group"]],
            "temps": [(d["ain"], d["name"]) for d in smart if d["temp"] is not None or d["hkr"]]
                     + ([("weather", "Außentemperatur (Wetter)")] if self.weather_cfg().get("enabled") else []),
            "wol": [(w["mac"], w["name"]) for w in self.cfg.get("wol", [])],
            "thermostats": [(d["ain"], d["name"]) for d in smart if d["hkr"]],
            "sensors": [(d["ain"], d["name"]) for d in smart if d.get("sensor")],
            "lamps": [(d["ain"], d["name"]) for d in smart if d.get("level") is not None],
            "media": [(d["ain"], d["name"]) for d in smart if d.get("media")],
            "printers": [(s_, p.name) for s_, p in self.printers.items()],
            "pings": [(t["host"], t["name"]) for t in self.cfg.get("ping_targets", [])],
            "wlans": [(w["index"] if w.get("box", "main") == "main" else f"{w['box']}:{w['index']}",
                       f"{w['ssid']} ({w['band']}" + (f", {w['box_name']})" if w.get("box_name") else ")"))
                      for w in self.data["wlan"]],
            "cameras": [(c["id"], c["name"]) for c in self.cfg.get("cameras", [])],
            "persons": [(p["id"], ("🔑 " if p.get("kind") == "item" else "") + p["name"])
                        for p in self.cfg.get("persons", [])],
            "items": [p["id"] for p in self.cfg.get("persons", []) if p.get("kind") == "item"],
            "motion_cams": [c["id"] for c in self.cfg.get("cameras", []) if (c.get("motion") or {}).get("enabled")],
            "scenes": [(r["id"], r["name"]) for r in self.cfg.get("automations", [])
                       if r.get("trigger", {}).get("type") == "manual"],
        }

    def entity_names(self):
        names = {}
        for lst in self.entities().values():  # nur (id, Name)-Paare – „items“/„motion_cams“ sind reine ID-Listen
            names.update(x for x in lst if isinstance(x, (tuple, list)) and len(x) == 2)
        return names

    def _close_window(self):
        if HEADLESS:
            return
        if self.cfg.get("tray", True) and self.tray.ok:
            self.withdraw()
            if not self.cfg.get("tray_hint_shown"):
                self.cfg["tray_hint_shown"] = True
                self.save()
                self.tray.notify(APP, "Läuft im Infobereich weiter 👋 Rechtsklick auf das Symbol zum Beenden.")
        else:
            self._quit()

    def _on_tray(self, ev):
        if ev == "open":
            self.deiconify()
            self.lift()
            self.focus_force()
        elif ev == "quit":
            self._quit()

    def _tab_ping(self):
        t = ttk.Frame(self.nb, padding=8)
        self.tab_ping = t
        self.nb.add(t, text="Ping-Monitor")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Behalte wichtige Geräte im Blick (PC, Switch, Server, Drucker …)",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Entfernen", command=self._ping_remove).pack(side="right")
        ttk.Button(bar, text="Hinzufügen …", command=self._ping_add).pack(side="right", padx=6)
        cols = [("st", "", 34, "center"), ("name", "Name", 220, "w"), ("host", "Adresse", 200, "w"),
                ("status", "Status", 110, "w"), ("rtt", "Antwortzeit", 110, "e"), ("seen", "Zuletzt erreichbar", 160, "center")]
        self.t_ping = self._make_tree(t, cols, "ping")
        self._render_ping()

    # ---------------- Hintergrund-Worker ----------------
    def _worker(self):
        next_full = next_smart = 0.0
        while not self.stop.is_set():
            due = min(next_full, next_smart)
            try:
                job = self.jobs.get(timeout=max(0.05, due - time.time()))
            except queue.Empty:
                job = ("tick",)
            kind = job[0]
            try:
                if kind == "quit":
                    return
                if kind == "connect":
                    self.results.put(("status", "Verbinde …"))
                    if self.router_kind() == "generic":
                        from router import GenericRouter
                        cl = GenericRouter(self.cfg.get("router_host", ""), self.cfg.get("router_name", ""))
                    else:
                        cl = FritzClient(self.cfg["host"], self.cfg["user"], self.password)
                    cl.connect()
                    self.client = cl
                    self.smart_pause_until = 0.0
                    self.results.put(("connected", None))
                    next_full = 0.0
                elif kind == "action":
                    import providers as _prov
                    try:
                        job[1](_prov.Hub(self.client, self.integrations))
                    except _prov.ProviderError as e:
                        self.results.put(("status", f"⚠ {e}"))
                        continue
                    self.smart_pause_until = 0.0
                    time.sleep(0.5)
                    next_smart = 0.0
                    if len(job) > 2 and job[2] == "full":
                        next_full = 0.0
                elif kind == "refresh":
                    next_full = 0.0
                now = time.time()
                if now >= next_full:
                    next_full = now + self.cfg.get("interval", 5)
                    next_smart = now + self.cfg.get("smart_interval", 3)
                    self._refresh_all()
                elif now >= next_smart:
                    next_smart = now + self.cfg.get("smart_interval", 3)
                    self._refresh_smart()
            except FritzError as e:
                self.results.put(("error", str(e)))
                next_full = next_smart = time.time() + max(10, self.cfg.get("interval", 5))
            except Exception as e:  # unerwartet, App soll weiterlaufen
                log.exception("Unerwarteter Fehler im Hintergrund (%s)", kind)
                self.results.put(("error", f"{type(e).__name__}: {e}"))
                next_full = next_smart = time.time() + 10

    def _refresh_extra(self, b):
        """Zusätzliche FRITZ!Box (z. B. Repeater) – nur Infos und WLAN."""
        bid = b["id"]
        try:
            cl = self.extra_clients.get(bid)
            if cl is None:
                pw = unprotect(b.get("pw", "")) or b.get("pw_plain", "")
                cl = FritzClient(b["host"], b.get("user", ""), pw)
                cl.connect()
                self.extra_clients[bid] = cl
            ov = cl.overview()
            wl = cl.wlans()
            self.results.put(("extra_box", (bid, ov, wl, "")))
        except Exception as e:
            self.extra_clients.pop(bid, None)
            if self._box_throttle.ok(bid + str(e)):
                log.warning("Zweite FRITZ!Box %s: %s", b.get("host"), e)
            self.results.put(("extra_box", (bid, {}, [], str(e))))

    def _refresh_smart(self):
        if self.client and time.time() >= self.smart_pause_until:
            try:
                self.results.put(("smart", self.client.smarthome()))
            except FritzError as e:
                self.smart_pause_until = time.time() + 60  # nicht dauernd neu einloggen
                self.results.put(("smart_err", str(e)))

    def _refresh_all(self):
        targets = list(self.cfg.get("ping_targets", []))
        ping_futs = {t["host"]: self.pool.submit(ping, t["host"]) for t in targets}
        if self.client:
            c = self.client
            for key, fn in (("overview", c.overview), ("hosts", c.hosts), ("wlan", c.wlans)):
                try:
                    self.results.put((key, fn()))
                except FritzError as e:
                    if key == "overview":
                        raise
                    self.results.put(("error", str(e)))
            self._refresh_smart()
            try:
                self.results.put(("mesh", c.mesh()))
            except Exception as e:
                if self._mesh_throttle.ok(str(e)):
                    log.info("Mesh-Übersicht nicht verfügbar: %s", e)
        for b in list(self.cfg.get("boxes", [])):
            self._refresh_extra(b)
        res = {}
        for h, f in ping_futs.items():
            try:
                res[h] = f.result(timeout=8)
            except Exception:
                res[h] = (False, "")
        self.results.put(("ping", res))
        if self.client:
            self.results.put(("status", "Aktualisiert " + time.strftime("%H:%M:%S")))

    def _poll(self):
        if self.stop.is_set():
            return  # App wird beendet – Fenster ist schon abgebaut
        try:
            while True:
                kind, payload = self.results.get_nowait()
                try:
                    getattr(self, "_on_" + kind)(payload)
                except tk.TclError as e:
                    if self.stop.is_set() or "invalid command name" in str(e):
                        log.debug("Anzeige schon geschlossen (%s): %s", kind, e)  # beim Beenden normal
                    else:
                        log.exception("Fehler beim Anzeigen von '%s'", kind)
                except Exception:
                    log.exception("Fehler beim Anzeigen von '%s'", kind)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.after(150, self._poll)

    # ---------------- Ergebnisse anzeigen ----------------
    def _on_status(self, text):
        self.l_status.configure(text=text, foreground=MUTED)

    def _on_error(self, text):
        self.l_status.configure(text="⚠ " + text, foreground=BAD)
        if self._log_throttle.ok(text):
            log.warning("Meldung: %s", text)

    def router_kind(self):
        """„fritz“, „generic“ (anderer Router) oder „“ (noch nicht eingerichtet)."""
        k = self.cfg.get("router") or ""
        if not k and self.cfg.get("user") and (self.password or self.cfg.get("password")):
            k = "fritz"  # ältere Einstellungen: FRITZ!Box
        return k

    def is_fritz(self):
        return self.router_kind() == "fritz"

    def _on_connected(self, _):
        if self.router_kind() == "generic":
            log.info("Router verbunden (allgemein, %s)", getattr(self.client, "host", "?"))
            self._fritz_only_hints()
        else:
            log.info("FRITZ!Box verbunden (%s)", self.cfg["host"])
        self.l_status.configure(text="Verbunden", foreground=OK)

    def _fritz_only_hints(self):
        """Mit anderem Router: WLAN- und DECT-Seite erklären, statt leer zu bleiben."""
        generic = self.router_kind() == "generic"
        for attr, txt in (("l_wlan_fo", "📶 WLAN ein-/ausschalten und das Gast-WLAN gehen nur mit einer FRITZ!Box.\n"
                                         "Dein Router wird über UPnP und die Geräteliste angebunden – Internet-Status, "
                                         "Datenrate, Geräte und Anwesenheit funktionieren."),
                          ("l_smart_fo", "🔌 Diese Seite zeigt FRITZ!DECT-Geräte (Steckdosen, Heizkörperregler) – die "
                                         "gibt es nur mit einer FRITZ!Box.\nAndere smarte Geräte findest du unter "
                                         "„⌕ Gerätesuche“.")):
            lb = getattr(self, attr, None)
            if lb is None:
                continue
            if generic:
                lb.configure(text=txt)
                lb.pack(anchor="w", pady=(10, 0))
            else:
                lb.pack_forget()

    def _on_mesh(self, payload):
        self.mesh_boxes, self.mesh_map = payload
        self._box_tiles()
        self._render_hosts()

    def _on_extra_box(self, payload):
        bid, ov, wl, err = payload
        b = next((x for x in self.cfg.get("boxes", []) if x["id"] == bid), None)
        if not b:
            return
        self.extra_info[bid] = {"ov": ov, "error": err}
        name = ov.get("model") or b.get("name") or b["host"]
        for w in wl:
            w["box"], w["box_name"] = bid, self._short(name)
        self.data["wlan"] = [w for w in self.data["wlan"] if w.get("box", "main") != bid] + wl
        self._on_wlan(self.data["wlan"], rebuild=False)
        self._box_tiles()

    @staticmethod
    def _short(model):
        return (model or "").replace("FRITZ!Box ", "").replace(" Cable", "") or "FRITZ!Box"

    def _box_tiles(self):
        keys = []
        main_model = self.main_info.get("model", "")
        seen_models = set()
        # Haupt-Box
        if main_model:
            role = next((b["role"] for b in self.mesh_boxes if b["model"] == main_model), "Hauptbox")
            ov = self.main_info
            sub = f"FRITZ!OS {ov.get('firmware', '?')} · läuft seit {fmt_dur(ov.get('uptime', 0))}"
            if self.mesh_map:
                sub = f"{sum(1 for v in self.mesh_map.values() if v['model'] == main_model)} Geräte · " + sub
            self.dash.put("Netzwerk", "box:main", {"type": "box", "box": {
                "name": self.dash.name_for("box:main", main_model), "model": main_model,
                "role": f"{role} · {self.cfg['host']}", "online": ov.get("online", True), "sub": sub}})
            keys.append("box:main")
            seen_models.add(main_model)
        # Zusätzlich eingerichtete Boxen
        for b in self.cfg.get("boxes", []):
            info = self.extra_info.get(b["id"], {})
            ov = info.get("ov", {})
            model = ov.get("model") or b.get("name") or "FRITZ!Box"
            role = next((m["role"] for m in self.mesh_boxes if m["model"] == model), "zusätzliche Box")
            k = "box:" + b["id"]
            keys.append(k)
            seen_models.add(model)
            sub = (f"FRITZ!OS {ov.get('firmware', '?')} · läuft seit {fmt_dur(ov.get('uptime', 0))}"
                   if ov else "verbinde …")
            self.dash.put("Netzwerk", k, {"type": "box", "box": {
                "name": b.get("name") or model, "model": model, "role": f"{role} · {b['host']}",
                "online": None if not info else not info.get("error"), "error": info.get("error", ""), "sub": sub}})
        # Weitere Boxen, die nur das Mesh kennt (ohne Zugangsdaten)
        for m in self.mesh_boxes:
            if m["model"] in seen_models or not m["model"]:
                continue
            k = "box:mesh-" + (m["mac"] or m["model"]).replace(":", "")
            keys.append(k)
            n_clients = sum(1 for v in self.mesh_map.values() if v["model"] == m["model"])
            self.dash.put("Netzwerk", k, {"type": "box", "box": {
                "name": m["model"], "model": m["model"], "role": m["role"], "online": True,
                "sub": f"{n_clients} Geräte verbunden · Details: Zugangsdaten hinterlegen"}})
        self.dash.remove_missing("Netzwerk", "box:", keys)
        self.dash.commit()

    def box_password(self, b):
        return unprotect(b.get("pw", "")) or b.get("pw_plain", "")

    # ---------------- Kalender ----------------
    def cal_password(self, src):
        return unprotect(src.get("pw", "")) or src.get("pw_plain", "") if src else ""

    def save_calendar(self, r):
        pw = r.pop("password", "")
        entry = dict(r)
        if pw:
            enc = protect(pw)
            if enc:
                entry["pw"] = enc
            else:
                entry["pw_plain"] = pw
        else:
            old = next((c for c in self.cfg.get("calendars", []) if c["id"] == r["id"]), {})
            for k in ("pw", "pw_plain"):
                if k in old:
                    entry[k] = old[k]
        self.cfg["calendars"] = [c for c in self.cfg.get("calendars", []) if c["id"] != r["id"]] + [entry]
        self.save()
        self.log("App", entry["name"], "Kalender verbunden")
        self.refresh_calendars(force=True)

    def remove_calendar(self, cid):
        self.cfg["calendars"] = [c for c in self.cfg.get("calendars", []) if c["id"] != cid]
        self.cal_status.pop(cid, None)
        self.save()
        self.refresh_calendars(force=True)

    def cal_status_text(self):
        if not self.cfg.get("calendars"):
            return "Noch kein Kalender verbunden → „Kalender verwalten …“"
        errs = [v for v in self.cal_status.values() if v.startswith("⚠")]
        if self._cal_busy:
            return "Lade Termine …"
        if errs:
            return errs[0]
        return f"Aktualisiert {time.strftime('%H:%M', time.localtime(self._cal_last))}" if self._cal_last else ""

    def refresh_calendars(self, force=False):
        if self._cal_busy or (not force and time.time() - self._cal_last < 900):
            return
        srcs = [dict(c) for c in self.cfg.get("calendars", [])]
        self._cal_busy = True
        if self.page == "calendar":
            self.tab_calendar.refresh()

        def run():
            now = dt.datetime.now()
            start = (now - dt.timedelta(days=45)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now + dt.timedelta(days=400)
            events, status = [], {}
            for src in srcs:
                try:
                    evs = calendar_sync.load_source(src, self.cal_password(src), start, end)
                    for e in evs:
                        e["color"] = src.get("color") or e["color"]
                        e["calendar"] = src.get("name", e["calendar"])
                    events += evs
                    status[src["id"]] = f"✓ {len(evs)} Termine"
                except calendar_sync.CalendarError as e:
                    status[src["id"]] = f"⚠ {src.get('name', '')}: {e}"
                    log.warning("Kalender %s (%s): %s", src.get("name"), src.get("kind", src.get("type")), e)
                except Exception as e:
                    status[src["id"]] = f"⚠ {src.get('name', '')}: {type(e).__name__}"
                    log.exception("Kalender %s", src.get("name"))
            events.sort(key=lambda x: (x["start"], not x["allday"], x["title"]))
            self.results.put(("calendar", (events, status)))
        threading.Thread(target=run, daemon=True).start()

    def _on_calendar(self, payload):
        self.cal_events, self.cal_status = payload
        self._cal_busy = False
        self._cal_last = time.time()
        self._agenda_tile()
        if self.page == "calendar":
            self.tab_calendar.refresh()

    def _agenda_tile(self):
        if not self.cfg.get("calendars"):
            self.dash.remove_missing("Termine", "cal:", [])
            self.dash.commit()
            return
        now = dt.datetime.now()
        today = now.date()
        upcoming = [e for e in self.cal_events if e["end"] > now or (e["allday"] and e["start"].date() >= today)]
        items = []
        for e in upcoming[:12]:
            d = max(e["start"].date(), today)
            items.append({"day": day_label(d), "time": "ganztägig" if e["allday"] else f"{e['start']:%H:%M}",
                          "title": e["title"], "color": e["color"]})
        n_today = sum(1 for e in upcoming if e["start"].date() <= today)
        errs = [v for v in self.cal_status.values() if v.startswith("⚠")]
        self.dash.put("Termine", "cal:agenda", {"type": "agenda", "events": items, "today": n_today,
                                                "status": errs[0][:38] if errs else ""}, cols=2, rows=2)
        self.dash.commit()

    def _cal_tick(self):
        try:
            self._cal_tick_inner()
        except Exception:
            log.exception("Kalender-Takt")
        if not self.stop.is_set():
            self.after(30000, self._cal_tick)

    def _cal_tick_inner(self):
        self.refresh_calendars()
        if self.cal_events and not self.cfg.get("satellite"):
            try:
                self.engine.calendar_check(self.cal_events)
            except Exception:
                pass
            if int(time.time()) % 300 < 30:
                self._agenda_tile()  # „Heute/Morgen“ aktuell halten

    # ---------------- Anwesenheit ----------------
    def _update_presence(self, now=None):
        now = now or time.time()
        active = {(h["mac"] or "").upper() for h in self.data["hosts"] if h["active"]}
        names = {(h["mac"] or "").upper(): h["name"] for h in self.data["hosts"]}
        changed, keys = False, []
        for p in self.cfg.get("persons", []):
            st = self.presence.setdefault(p["id"], {"state": None, "last_seen": 0.0, "since": now})
            macs = [m.upper() for m in p.get("macs", [])]
            seen = [m for m in macs if m in active]
            if seen:
                st["last_seen"] = now
                new = "home"
            elif st["state"] is None or now - st["last_seen"] >= float(p.get("delay", 10)) * 60:
                new = "away"
            else:
                new = st["state"]  # Wartezeit läuft noch
            if new != st["state"]:
                if st["state"] is not None:
                    if p.get("kind") == "item":
                        txt = "ist wieder da" if new == "home" else "ist weg"
                    else:
                        txt = "ist nach Hause gekommen" if new == "home" else "ist unterwegs"
                    self.log("Anwesenheit", p["name"], txt)
                    self.notify("presence", f"{p['name']} {txt}", "")
                    if new == "home" and p.get("kind") != "item":
                        vc = self.vacation_cfg()
                        if vc["active"] and vc["auto_end"] and not self.cfg.get("satellite"):
                            name_ = p["name"]
                            self.after(10, lambda n=name_: self.vacation_set(
                                False, reason=f"{n} ist nach Hause gekommen – willkommen zurück! 👋"))
                st["state"], st["since"] = new, now
                changed = True
            k = "person:" + p["id"]
            keys.append(k)
            devs = ", ".join(names.get(m) or m[-5:] for m in macs[:2]) + (" …" if len(macs) > 2 else "")
            self.dash.put("Personen", k, {"type": "person", "name": p["name"], "home": st["state"] == "home",
                                          "since": time.strftime("%H:%M" if now - st["since"] < 86400 else "%d.%m. %H:%M",
                                                                 time.localtime(st["since"])), "devices": devs,
                                          "item": p.get("kind") == "item"})
        self.dash.remove_missing("Personen", "person:", keys)
        self.dash.commit()
        if changed:
            self._engine_update()

    def presence_snapshot(self):
        pres = {pid: st["state"] for pid, st in self.presence.items()
                if st["state"] and any(p["id"] == pid for p in self.cfg.get("persons", []))}
        people = {p["id"] for p in self.cfg.get("persons", []) if p.get("kind") != "item"}
        ppl = [v for k, v in pres.items() if k in people]
        if ppl:  # „Irgendwer“ zählt nur Personen – das Auto in der Einfahrt ist niemand, der zu Hause ist
            pres["_any"] = "home" if "home" in ppl else "away"
        return pres

    def persons_dialog(self):
        d = PersonsDialog(self, self)
        self.wait_window(d)

    def save_person(self, r):
        self.cfg["persons"] = [p for p in self.cfg.get("persons", []) if p["id"] != r["id"]] + [r]
        self.save()
        self.log("Anwesenheit", r["name"], f"eingerichtet ({len(r['macs'])} Gerät(e))")
        self._update_presence()

    def remove_person(self, pid):
        self.cfg["persons"] = [p for p in self.cfg.get("persons", []) if p["id"] != pid]
        self.presence.pop(pid, None)
        self.save()
        self._update_presence()

    def boxes_dialog(self):
        known = [m for m in self.mesh_boxes if m["model"] != self.main_info.get("model")]
        d = BoxesDialog(self, self, known)
        self.wait_window(d)

    def save_box(self, r):
        entry = {"id": r["id"], "name": r["name"], "host": r["host"], "user": r["user"]}
        enc = protect(r["password"])
        if enc:
            entry["pw"] = enc
        elif r["password"]:
            entry["pw_plain"] = r["password"]
        self.cfg["boxes"] = [b for b in self.cfg.get("boxes", []) if b["id"] != r["id"]] + [entry]
        self.extra_clients.pop(r["id"], None)
        self.save()
        self.jobs.put(("refresh",))

    def remove_box(self, bid):
        self.cfg["boxes"] = [b for b in self.cfg.get("boxes", []) if b["id"] != bid]
        self.extra_clients.pop(bid, None)
        self.extra_info.pop(bid, None)
        self.data["wlan"] = [w for w in self.data["wlan"] if w.get("box", "main") != bid]
        self.save()
        self._on_wlan(self.data["wlan"], rebuild=False)
        self._box_tiles()

    def _on_overview(self, d):
        self.l_title.configure(text=d.get("model", "FRITZ!Box"))
        self.main_info = d
        self._box_tiles()
        v, s = self.c["box"]
        v.configure(text=d.get("model", "–"))
        if d.get("kind") == "generic":
            s.configure(text="Router · Internet-Daten über UPnP" if d.get("upnp") else
                        "Router · UPnP aus – nur Online-Status (im Router „UPnP“ einschalten für mehr)")
        else:
            s.configure(text=f"FRITZ!OS {d.get('firmware', '?')} · läuft seit {fmt_dur(d.get('uptime', 0))}")
        v, s = self.c["inet"]
        if "online" in d:
            v.configure(text="● Online" if d["online"] else "● Offline", fg=OK if d["online"] else BAD)
            s.configure(text=(f"verbunden seit {fmt_dur(d.get('wan_uptime', 0))}" if d["online"] else "keine Verbindung")
                        + (f" · {d['access']}" if d.get("access") else ""))
        v, s = self.c["ip"]
        v.configure(text=d.get("ext_ip") or "–")
        s.configure(text="IPv4")
        v, s = self.c["line"]
        if d.get("max_down"):
            v.configure(text=f"↓ {fmt_bits(d['max_down'])}")
            s.configure(text=f"↑ {fmt_bits(d.get('max_up', 0))}")
        if "rate_down" in d:
            dn, up = d["rate_down"] * 8, d["rate_up"] * 8
            self.c["down"][0].configure(text=fmt_bits(dn), fg=DOWN_C)
            self.c["up"][0].configure(text=fmt_bits(up), fg=UP_C)
            if d.get("max_down"):
                self.c["down"][1].configure(text=f"{dn / d['max_down'] * 100:.0f} % der Leitung")
                self.c["up"][1].configure(text=f"{up / max(1, d.get('max_up', 1)) * 100:.0f} % der Leitung")
            self.hist.append((dn, up))
            self.store.add_series("net:down", dn)
            self.store.add_series("net:up", up)
            self._draw_graph()
            self.dash.down_hist = [a for a, _ in self.hist][-40:]
        if "online" in d:
            self.store.add_series("net:online", 1 if d["online"] else 0)
            if self._prev_online is not None and d["online"] != self._prev_online:
                if d["online"]:
                    self.notify("internet", "Internet ist wieder da ✅", f"Externe IP: {d.get('ext_ip', '?')}")
                else:
                    self.notify("internet", "Internet weg ⚠️", ("Die FRITZ!Box" if self.is_fritz() else "Der Router")
                                + " meldet keine Verbindung.", True)
            changed = self._prev_online != d["online"]
            self._prev_online = d["online"]
            if changed:
                self._engine_update()
        im = {"type": "internet", "online": d.get("online"), "ext_ip": d.get("ext_ip", "")}
        if "rate_down" in d:
            im["down"], im["up"] = fmt_bits(d["rate_down"] * 8), fmt_bits(d["rate_up"] * 8)
        self.dash.put("Netzwerk", "net:internet", im)
        self.dash.commit()

    def _draw_graph(self):
        g = self.graph
        g.delete("all")
        w, h = g.winfo_width(), g.winfo_height()
        if w < 50 or h < 50:
            return
        pad_l, pad_b, pad_t = 70, 18, 8
        mx = max([max(a, b) for a, b in self.hist] + [1e6])
        for i in range(5):
            y = pad_t + (h - pad_t - pad_b) * i / 4
            g.create_line(pad_l, y, w - 6, y, fill=CARD2)
            g.create_text(pad_l - 8, y, text=fmt_bits(mx * (4 - i) / 4), fill=MUTED, anchor="e",
                          font=("Segoe UI", 8))
        if len(self.hist) < 2:
            g.create_text(w / 2, h / 2, text="Sammle Daten …", fill=MUTED, font=("Segoe UI", 10))
            return
        n = self.hist.maxlen
        step = (w - 6 - pad_l) / (n - 1)
        off = n - len(self.hist)
        for idx, color in ((0, DOWN_C), (1, UP_C)):
            pts = []
            for i, val in enumerate(self.hist):
                pts += [pad_l + (off + i) * step, h - pad_b - (h - pad_t - pad_b) * val[idx] / mx]
            g.create_line(*pts, fill=color, width=2, smooth=True)
        secs = n * self.cfg.get("interval", 5)
        g.create_text(pad_l, h - 2, text=f"vor {secs // 60} min", fill=MUTED, anchor="sw", font=("Segoe UI", 8))
        g.create_text(w - 6, h - 2, text="jetzt", fill=MUTED, anchor="se", font=("Segoe UI", 8))

    def _on_hosts(self, hosts):
        self.data["hosts"] = hosts
        try:
            self._check_new_hosts(hosts)
        except Exception:
            log.exception("Neue Geräte prüfen")
        if self.cfg.get("wol"):
            self._wol_tiles()
        if self.cfg.get("devices"):
            self._dev_tiles()
        on = sum(1 for x in hosts if x["active"])
        lan = sum(1 for x in hosts if x["active"] and x["iface"] == "Ethernet")
        wl = sum(1 for x in hosts if x["active"] and x["iface"] == "802.11")
        v, s = self.c["hosts"]
        v.configure(text=f"{on} von {len(hosts)}")
        s.configure(text=f"{lan} per LAN · {wl} per WLAN")
        self._render_hosts()
        try:
            self._update_presence()
        except Exception:
            log.exception("Anwesenheit")
        self.dash.put("Netzwerk", "net:hosts", {"type": "hosts", "online": on, "lan": lan, "wlan": wl})
        self.dash.commit()

    def _render_hosts(self):
        flt = self.v_filter.get().lower().strip()
        rows = []
        for x in self.data["hosts"]:
            if self.v_online.get() and not x["active"]:
                continue
            conn = {"Ethernet": "LAN", "802.11": "WLAN"}.get(x["iface"], x["iface"] or "–")
            if x["port"] and conn == "LAN":
                conn += f" {x['port']}"
            ap = self.mesh_map.get((x["mac"] or "").upper())
            if ap and x["active"]:
                via = ap["via"] if ap["via"] and ap["via"] != "LAN" else ""
                conn = ("WLAN " + via if x["iface"] == "802.11" and via else conn) + f" · {self._short(ap['model'])}"
            if x["guest"]:
                conn += " (Gast)"
            speed = f"{x['speed']} Mbit/s" if x["speed"] and x["speed"] != "0" else ""
            name = ("🆕 " + x["name"]) if (x["mac"] or "").upper() in self.new_macs else x["name"]
            vals = ("●" if x["active"] else "○", name, x["ip"], x["mac"], conn, speed)
            if flt and flt not in " ".join(vals).lower():
                continue
            rows.append((x["mac"] or x["ip"] or x["name"], vals, ("on" if x["active"] else "off",)))
        self._apply_sort("hosts", rows)
        rows = self._dedupe(rows)
        self.l_hostcount.configure(text=f"{len(rows)} Geräte angezeigt")
        mode = self.v_hview.get()
        if mode != self._hview_mode:
            self.t_hosts.delete(*self.t_hosts.get_children())
            if mode == "Netzwerkkarte":
                self.hosts_wrap.pack_forget()
                self.hmap_wrap.pack(fill="both", expand=True)
            else:
                self.hmap_wrap.pack_forget()
                self.hosts_wrap.pack(fill="both", expand=True)
            self.t_hosts.configure(show="tree headings" if mode == "Nach FRITZ!Box" else "headings")
            self.t_hosts.column("#0", width=260 if mode == "Nach FRITZ!Box" else 0, stretch=False)
            self._hview_mode = mode
        if mode == "Liste":
            update_tree(self.t_hosts, rows)
        elif mode == "Nach FRITZ!Box":
            groups = self._host_groups(rows)
            open_state = {iid: self.t_hosts.item(iid, "open") for iid in self.t_hosts.get_children()}
            sel = self.t_hosts.selection()
            self.t_hosts.delete(*self.t_hosts.get_children())
            for gname, items in groups:
                gid = "grp:" + gname
                self.t_hosts.insert("", "end", iid=gid, text=f"{gname}  ({len(items)})", tags=("group",),
                                    open=open_state.get(gid, True))
                for iid, vals, tags in items:
                    self.t_hosts.insert(gid, "end", iid=iid, values=vals, tags=tags)
            if sel and self.t_hosts.exists(sel[0]):
                self.t_hosts.selection_set(sel[0])
        else:
            self._hmap_rows = rows
            self._draw_hmap()

    def _box_of(self, mac, active):
        """Name der FRITZ!Box, über die ein Gerät verbunden ist."""
        if not active:
            return "Offline"
        ap = self.mesh_map.get((mac or "").upper())
        if ap:
            return ap["model"] or ap["ap"]
        return self.main_info.get("model", "FRITZ!Box") if not self.mesh_map else "Nicht zugeordnet"

    def _host_groups(self, rows):
        by_mac = {(x["mac"] or x["ip"] or x["name"]): x for x in self.data["hosts"]}
        groups = {}
        for r in rows:
            x = by_mac.get(r[0], {})
            groups.setdefault(self._box_of(x.get("mac"), x.get("active")), []).append(r)
        main = self.main_info.get("model", "")
        order = lambda g: (0 if g == main else 2 if g in ("Nicht zugeordnet", "Offline") else 1, g)  # noqa: E731
        return sorted(groups.items(), key=lambda kv: order(kv[0]))

    def _draw_hmap(self):
        from dashboard import draw_icon, rrect as _rr
        c = self.hmap
        c.delete("all")
        rows = getattr(self, "_hmap_rows", [])
        w = max(400, c.winfo_width())
        groups = [g for g in self._host_groups(rows) if g[0] != "Offline"]
        if not groups:
            c.create_text(w / 2, 80, text="Noch keine Geräte – warte auf die FRITZ!Box …", fill=MUTED,
                          font=("Segoe UI", 11))
            return
        by_mac = {(x["mac"] or x["ip"] or x["name"]): x for x in self.data["hosts"]}
        roles = {b["model"]: b["role"] for b in self.mesh_boxes}
        n = len(groups)
        colw = (w - 20) / n
        top, cardh = 14, 74
        centers = []
        for i, (gname, items) in enumerate(groups):
            x0 = 10 + i * colw
            cx = x0 + colw / 2
            centers.append(cx)
            is_box = gname not in ("Nicht zugeordnet",)
            _rr(c, x0 + 6, top, x0 + colw - 6, top + cardh, 14, fill="#1b2c47" if i == 0 and is_box else CARD,
                outline="#2f4f80" if is_box else CARD2)
            draw_icon(c, "Router" if is_box else "Geräte", x0 + 14, top + 15, OK if is_box else MUTED)
            c.create_text(x0 + 66, top + 26, text=self._short(gname) if is_box else gname, anchor="w", fill=FG,
                          font=("Segoe UI Semibold", 12))
            c.create_text(x0 + 66, top + 48, text=(roles.get(gname, "Haupt-Box" if i == 0 else "") + f" · {len(items)} Geräte"),
                          anchor="w", fill=MUTED, font=("Segoe UI", 9))
            y = top + cardh + 22
            for iid, vals, tags in items:
                x = by_mac.get(iid, {})
                ap = self.mesh_map.get((x.get("mac") or "").upper(), {})
                via = ap.get("via") or ("LAN" if x.get("iface") == "Ethernet" else "WLAN")
                col = {"LAN": "#3ecf8e", "2,4 GHz": "#f0883e", "5 GHz": "#4f8ff7", "6 GHz": "#b392f0"}.get(via, MUTED)
                c.create_line(cx, top + cardh, cx, y - 8, fill=CARD2) if y == top + cardh + 22 else None
                _rr(c, x0 + 10, y - 13, x0 + colw - 10, y + 13, 8, fill=CARD, outline="")
                c.create_oval(x0 + 18, y - 4, x0 + 26, y + 4, fill=col, outline="")
                name = vals[1] or vals[2]
                maxch = max(8, int((colw - 120) / 7))
                c.create_text(x0 + 34, y, text=name[:maxch] + ("…" if len(name) > maxch else ""), anchor="w",
                              fill=FG, font=("Segoe UI", 9))
                c.create_text(x0 + colw - 16, y, text=via, anchor="e", fill=col, font=("Segoe UI", 8))
                y += 30
        # Mesh-Verbindungen zwischen den Boxen
        for i in range(1, n):
            if groups[i][0] == "Nicht zugeordnet":
                continue
            x1, x2 = centers[0] + colw / 2 - 6, centers[i] - colw / 2 + 6
            yy = top + cardh / 2
            if i == 1:
                c.create_line(x1, yy, x2, yy, fill=ACCENT, width=2, dash=(6, 4))
            else:
                c.create_line(centers[0], top + cardh, centers[0], top + cardh + 8, centers[i], top + cardh + 8,
                              centers[i], top + cardh, fill=ACCENT, width=2, dash=(6, 4))
        lx = 16
        ly = max(top + cardh + 22 + 30 * max(len(g[1]) for g in groups), 200) + 10
        for label, col in (("LAN", "#3ecf8e"), ("2,4 GHz", "#f0883e"), ("5 GHz", "#4f8ff7"), ("6 GHz", "#b392f0")):
            c.create_oval(lx, ly - 4, lx + 8, ly + 4, fill=col, outline="")
            c.create_text(lx + 14, ly, text=label, anchor="w", fill=MUTED, font=("Segoe UI", 9))
            lx += 90
        c.configure(scrollregion=(0, 0, w, ly + 20))

    @staticmethod
    def _dedupe(rows):
        seen, out = set(), []
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                out.append(r)
        return out

    def _on_wlan(self, wl, rebuild=True):
        if rebuild:
            main = self._short(self.main_info.get("model", "")) if self.main_info else ""
            for w in wl:
                w.setdefault("box", "main")
                w.setdefault("box_name", main)
            wl = wl + [w for w in self.data["wlan"] if w.get("box", "main") != "main"]
        self.data["wlan"] = wl
        rows = []
        for x in wl:
            st = "aktiv" if x["enabled"] else "aus"
            ssid = x["ssid"] + (f"  ·  {x['box_name']}" if x.get("box_name") else "")
            vals = ("●" if x["enabled"] else "○", x["index"], x["band"], ssid, st, x["channel"] or "–",
                    x["standard"] or "–", "–" if x["clients"] is None else x["clients"])
            rows.append((self._wkey(x), vals, ("on" if x["enabled"] else "off",)))
        update_tree(self.t_wlan, rows)
        keys = []
        gw = self.guest_wlan()
        for x in wl:
            if x is gw:
                continue  # das Gast-WLAN bekommt seine eigene Kachel
            k = "wlan:" + self._wkey(x)
            keys.append(k)
            self.dash.put("Netzwerk", k, {"type": "wlan", "wlan": x})
        self.dash.remove_missing("Netzwerk", "wlan:", keys)
        self.dash.commit()
        if not gw:
            self.dash.remove_missing("Netzwerk", "guest:", [])
            self.dash.commit()
        if gw:
            self._guest_tile()
            if not self.guest or self.guest.get("index") != gw["index"] or self.guest.get("ssid") != gw["ssid"]:
                self.fetch_guest(force=not self.guest or self.guest.get("index") != gw["index"])
            else:
                self.fetch_guest()

    def _on_ext_devs(self, payload):
        """Neuer Stand von einem fremden Anbieter (Shelly, Hue …)."""
        pid, devs, err, urgent = payload
        if devs is None:
            self.ext_devs.pop(pid, None)
            self.ext_err.pop(pid, None)
        else:
            self.ext_devs[pid] = devs
            if err != self.ext_err.get(pid):
                c = next((x for x in self.cfg.get("integrations", []) if x["id"] == pid), {})
                if err:
                    log.warning("Anbieter %s: %s", c.get("name") or pid, err)
                    self.log("App", c.get("name") or "Anbieter", f"⚠ {err}")
                elif pid in self.ext_err:
                    self.log("App", c.get("name") or "Anbieter", "wieder verbunden")
            self.ext_err[pid] = err
        # sofort anzeigen, wenn es eilt oder die FRITZ!Box gerade nichts liefert – sonst mit ihrem nächsten Takt
        if urgent or not self.client or time.time() - self._fritz_t > 8:
            self._on_smart(self._fritz_devs, _ext=True)

    def all_ext_devs(self):
        return [d for pid in sorted(self.ext_devs) for d in self.ext_devs[pid]]

    def _sensor_events(self, devs):
        """Melder auswerten: Wasser/Rauch → dringende Push, Tür/Fenster → Logbuch (+ Push im Urlaub)."""
        first = not self._sensor_prev
        for d in devs:
            s = d.get("sensor")
            if not s or not d.get("present", True):
                continue
            prev = self._sensor_prev.get(d["ain"]) or {}
            self._sensor_prev[d["ain"]] = dict(s)
            for key, icon, title in (("water", "💧", "Wasser erkannt"), ("smoke", "🔥", "RAUCHALARM"),
                                     ("alarm", "🚨", "Alarm")):
                if s.get(key) and not prev.get(key):
                    self.log("Gerät", d["name"], f"{icon} {title}!")
                    if not self.cfg.get("satellite"):
                        self.notify("alarm", f"{icon} {title}: {d['name']}", "Bitte sofort nachsehen!", warn=True)
                elif prev.get(key) and not s.get(key):
                    self.log("Gerät", d["name"], "Alarm vorbei")
            c, pc = s.get("contact"), prev.get("contact")
            if c and pc and c != pc:
                self.log("Gerät", d["name"], "geöffnet" if c == "open" else "geschlossen")
                vc = self.vacation_cfg()
                if c == "open" and vc["active"] and not self.cfg.get("satellite") and not self._anyone_home():
                    self.notify("contact", f"🏖 {d['name']} wurde geöffnet", "Ihr seid im Urlaub – bitte nachsehen.",
                                warn=True)
            b, pb = s.get("button"), prev.get("button")
            if b and b[0] and (not pb or tuple(pb) != tuple(b)) and not first and prev:
                self.log("Gerät", d["name"], "Taster gedrückt" + (f" ({b[1]})" if b[1] else ""))

    def _on_smart(self, devs, tiles=True, _ext=False):
        if not _ext:
            self._fritz_devs, self._fritz_t = devs, time.time()
        devs = list(self._fritz_devs) + self.all_ext_devs()
        try:
            self._sensor_events(devs)
        except Exception:
            log.exception("Melder auswerten")
        self.data["smart"] = devs
        self.l_smart.configure(text="Doppelklick: Steckdose schalten bzw. Temperatur einstellen", foreground=MUTED)
        rows = []
        for d in devs:
            state = d["switch"] if d["switch"] is not None else d["simpleonoff"]
            st_txt = "–" if state is None else ("AN" if state else "AUS")
            temp = fmt_c(d["temp"])
            if d["humidity"] is not None:
                temp += f" · {d['humidity']} %"
            hkr = "–"
            if d["hkr"]:
                hkr = f"{fmt_c(d['hkr']['ist'])} / {fmt_c(d['hkr']['soll'])}"
                if d["hkr"]["window_open"]:
                    hkr += " (Fenster)"
            bat = f"{d['battery']} %" if d["battery"] else "–"
            if d["batterylow"]:
                bat += " ⚠"
            vals = ("●" if d["present"] else "○", d["name"], d["kind"], d["product"], st_txt, temp,
                    "–" if d["power_w"] is None else f"{d['power_w']:.1f} W".replace(".", ","),
                    "–" if d["energy_kwh"] is None else f"{d['energy_kwh']:.2f} kWh".replace(".", ","),
                    hkr, bat)
            tag = "off" if not d["present"] else ("warn" if d["batterylow"] else "on")
            rows.append((d["ain"], vals, (tag,)))
        self._apply_sort("smart", rows)
        update_tree(self.t_smart, rows)
        online = sum(1 for d in devs if d["present"] and not d["is_group"])
        total = sum(1 for d in devs if not d["is_group"])
        power = sum(d["power_w"] or 0 for d in devs if not d["is_group"])
        try:
            self._record_smart(devs)
        except Exception:
            log.exception("Verlauf (Smart Home)")
        try:
            self._battery_record(devs)
        except Exception:
            log.exception("Batterien")
        try:
            self._vacation_tick()
        except Exception:
            log.exception("Urlaubsmodus")
        try:
            self._appliances_update(devs)
        except Exception:
            log.exception("Waschmaschine & Co.")
        if tiles:
            self._smart_tiles(devs, power)
            self._engine_update()
        v, s = self.c["smart"]
        v.configure(text=f"{online} von {total} erreichbar", fg=FG)
        s.configure(text=f"aktuell {power:.1f} W an Steckdosen".replace(".", ","))

    def _on_smart_err(self, text):
        self.l_smart.configure(text="⚠ " + text, foreground=BAD)
        self.c["smart"][0].configure(text="nicht verfügbar", fg=MUTED)
        self.c["smart"][1].configure(text=text[:60])

    def _on_ping(self, res):
        self.data["ping"] = res
        now = time.strftime("%d.%m. %H:%M:%S")
        names_ = {t["host"]: t.get("name") or t["host"] for t in self.cfg.get("ping_targets", [])}
        for h, (ok, ms) in res.items():
            self.store.add_series("pingok:" + h, 1 if ok else 0)
            self.store.name_series("ping:" + h, names_.get(h, h))
            m = re.search(r"[\d.]+", ms or "")
            if ok and m:
                self.store.add_series("ping:" + h, float(m.group()))
        for h, (ok, _) in res.items():
            if ok:
                self.last_seen[h] = now
        self._render_ping()
        self._ping_tiles()
        names = {t["host"]: t["name"] for t in self.cfg.get("ping_targets", [])}
        for h, (ok, _) in res.items():
            st = self._ping_state.get(h)
            if ok:
                self._ping_fail[h] = 0
                if st == "down":
                    self.notify("ping", f"{names.get(h, h)} ist wieder erreichbar ✅", h)
                self._ping_state[h] = "ok"
            else:
                self._ping_fail[h] = self._ping_fail.get(h, 0) + 1
                if self._ping_fail[h] >= 2 and st != "down":
                    if st == "ok":
                        self.notify("ping", f"{names.get(h, h)} nicht erreichbar ⚠️", h, True)
                    self._ping_state[h] = "down"
        self._engine_update()

    def _render_ping(self):
        rows = []
        for t in self.cfg.get("ping_targets", []):
            r = self.data["ping"].get(t["host"])
            if r is None:
                vals, tag = ("…", t["name"], t["host"], "prüfe …", "", self.last_seen.get(t["host"], "–")), "off"
            else:
                ok, rtt = r
                vals = ("●", t["name"], t["host"], "erreichbar" if ok else "nicht erreichbar", rtt,
                        self.last_seen.get(t["host"], "–"))
                tag = "on" if ok else "bad"
            rows.append((t["host"], vals, (tag,)))
        update_tree(self.t_ping, self._dedupe(rows))

    # ---------------- Dashboard-Kacheln ----------------
    def _smart_tiles(self, devs, total_power):
        dash = self.dash
        keys = []
        plugs = [d for d in devs if d["power_w"] is not None and not d["is_group"]]
        if plugs:
            dash.total_hist = (dash.total_hist + [total_power])[-40:]
            t_ = (self.power_rep or {}).get("total") or {}
            dash.put("Smart Home", "dect:_energy", {"type": "energy", "power": total_power, "count": len(plugs),
                                                    "month_eur": t_.get("month_eur"), "prog_eur": t_.get("prog_eur"),
                                                    "over_budget": t_.get("over_budget")})
            keys.append("dect:_energy")
        for d in devs:
            k = "dect:" + d["ain"]
            keys.append(k)
            val = d["power_w"] if d["power_w"] is not None else (
                d["hkr"]["ist"] if d["hkr"] and isinstance(d["hkr"]["ist"], float) else d["temp"])
            if d["present"] and val is not None:
                h = dash.power_hist.setdefault(d["ain"], collections.deque(maxlen=120))
                h.append(val)
            # bestätigte Solltemperatur -> Vorschau löschen
            pend = dash.pending_temp.get(d["ain"])
            if pend is not None and d["hkr"] and d["hkr"]["soll"] == pend and d["ain"] not in self._temp_after:
                dash.pending_temp.pop(d["ain"], None)
            dash.put("Smart Home", k, {"type": "dect", "dev": d})
            if d["energy_kwh"] is not None and d["present"] and not d["is_group"]:
                self.store.add_energy(d["ain"], d["name"], d["energy_kwh"] * 1000, d["power_w"] or 0)
            on = d["switch"] if d["switch"] is not None else d["simpleonoff"]
            if on is not None and d["present"]:
                was = self._sw_prev.get(d["ain"])
                if was is not None and was != on:
                    self.log("Gerät", d["name"], "eingeschaltet" if on else "ausgeschaltet")
                self._sw_prev[d["ain"]] = on
            prev = self._dect_prev.get(d["ain"])
            if prev and not d["is_group"]:
                if prev[0] and not d["present"]:
                    self.notify("smart", f"{d['name']} nicht erreichbar", "Das DECT-Gerät meldet sich nicht mehr.", True)
                if not prev[1] and d["batterylow"]:
                    self.notify("smart", f"{d['name']}: Batterie schwach 🔋", "Bitte bald die Batterien tauschen.", True)
            self._dect_prev[d["ain"]] = (d["present"], d["batterylow"])
        if any(d.get("hkr") for d in devs):
            dash.put("Smart Home", "dect:_heating", {"type": "heating"})
            keys.append("dect:_heating")
        bats = self.battery_items()
        if bats:
            dash.put("Smart Home", "dect:_battery", {"type": "battery", "items": bats[:3], "count": len(bats),
                                                     "low": sum(1 for b in bats if b["low"])})
            keys.append("dect:_battery")
        dash.remove_missing("Smart Home", "dect:", keys)
        dash.commit()
        for w in list(self.details):
            w.refresh()

    def _ping_tiles(self):
        keys = []
        for t in self.cfg.get("ping_targets", []):
            k = "ping:" + t["host"]
            keys.append(k)
            r = self.data["ping"].get(t["host"])
            self.dash.put("Netzwerk", k, {"type": "ping", "name": t["name"], "host": t["host"],
                                          "ok": None if r is None else r[0], "rtt": "" if r is None else r[1]})
        self.dash.remove_missing("Netzwerk", "ping:", keys)
        self.dash.commit()

    def _plug_wh(self, serial):
        pc = next((p for p in self.cfg.get("printers", []) if p["serial"].strip() == serial), {})
        ain = pc.get("plug_ain")
        d = next((x for x in self.data["smart"] if ain and x["ain"] == ain), None)
        return d["energy_kwh"] * 1000 if d and d["energy_kwh"] is not None else None

    def _on_printer(self, payload):
        serial, summ = payload
        if serial not in self.printers:
            return
        st, prev = summ["state"], self._pstate.get(serial)
        if summ.get("error") and summ["error"] != self._perr_txt.get(serial):
            self._perr_txt[serial] = summ["error"]
            log.warning("Drucker %s: %s", summ["name"], summ["error"])
        busy = st in ("RUNNING", "PREPARE", "PAUSE", "SLICING")
        wh = self._plug_wh(serial)
        op = self.store.open_print(serial)
        if summ["connected"] and st:
            if busy:
                if not op:
                    self.store.start_print(serial, summ["name"], summ["file"], wh)
                    op_new = self.store.open_print(serial)
                    pr_ = self.printers.get(serial)
                    if op_new and pr_:
                        self._fetch_usage(serial, op_new[0], dict(pr_.raw))
                elif op[2] is None and wh is not None:
                    self.store.set_start_wh(op[0], wh)
            elif op:
                result = {"FINISH": "fertig", "FAILED": "fehlgeschlagen"}.get(st, "abgebrochen")
                self.store.end_print(serial, wh, result)
                try:
                    self._fil_consume(serial, op[0], st, summ)
                except Exception:
                    log.exception("Filament-Lager")
                self.after(500, lambda: self.refresh_energy(reschedule=False))
            # Benachrichtigungen bei Statuswechsel
            if prev and prev != st:
                file = summ["file"] or "Druck"
                if st == "FINISH":
                    last = self.store.prints(1)
                    extra = ""
                    if last and last[0][4] is not None and last[0][5] is not None:
                        kwh = (last[0][5] - last[0][4]) / 1000
                        extra = f" · {fnum(kwh, 2)} kWh · {self.eur(kwh)}"
                    try:
                        rows = self.store.prints_between(time.time() - 7 * 86400, time.time() + 60, serial)
                        if rows and rows[0][8]:
                            _, rc = self.print_cost(rows[0])
                            extra = f" · {fnum(rows[0][8], 0)} g · Selbstkosten {pricing.eur(rc['cost'])}"
                    except Exception:
                        log.exception("Druckkosten")
                    img = None
                    pr = self.printers.get(serial)
                    if pr and self.cfg.get("push", {}).get("printer_photo", True):
                        url = bambu_url(pr.ip, pr.code)
                        img = lambda url=url: grab_frame(url)  # noqa: E731 – läuft im Push-Thread
                    self.notify("printer", f"{summ['name']}: Druck fertig 🎉", file + extra, image_fn=img)
                elif st == "FAILED":
                    self.notify("printer", f"{summ['name']}: Druck fehlgeschlagen ⚠️", file, True)
                elif st == "PAUSE" and serial not in self._user_paused and \
                        (summ.get("print_error") or 0) not in printwatch.SPAGHETTI_ERR:  # dafür gibt's die 🍝-Nachricht
                    self.notify("printer", f"{summ['name']}: Druck pausiert ⏸️",
                                "Bitte am Drucker nachsehen (z. B. Filament leer).", True)
                if st != "PAUSE":
                    self._user_paused.discard(serial)
            err = summ.get("print_error") or 0
            self._printwatch(serial, summ, st)
            if err and not self._perr.get(serial) and err not in printwatch.SPAGHETTI_ERR:
                self.notify("printer", f"{summ['name']} meldet einen Fehler ⚠️",
                            f"Fehlercode {err:08X} – Details am Druckerdisplay.", True)
            self._perr[serial] = err
            self._pstate[serial] = st
        # Kostenzeile für die Kachel
        cost = ""
        op = self.store.open_print(serial)
        if busy and op and op[2] is not None and wh is not None:
            kwh = max(0.0, (wh - op[2]) / 1000)
            cost = f"Dieser Druck bisher: {fnum(kwh, 2)} kWh · {self.eur(kwh)}"
        elif not busy:
            last = self.store.prints(1)
            if last and last[0][4] is not None and last[0][5] is not None:
                kwh = (last[0][5] - last[0][4]) / 1000
                cost = f"Letzter Druck: {fnum(kwh, 2)} kWh · {self.eur(kwh)}"
        summ["cost_text"] = cost
        if busy and isinstance(summ.get("percent"), (int, float)):
            self._plast_pct[serial] = summ["percent"]
        try:
            self._fil_sync(serial, summ)
        except Exception:
            log.exception("Filament-Lager (AMS)")
        try:
            self._tl_update(serial, summ)
            summ["tl"] = self.tl_state(serial)
        except Exception:
            log.exception("Zeitraffer")
        self.dash.put("3D-Druck", "printer:" + serial, {"type": "printer", "p": summ}, cols=2, rows=2)
        try:
            self._ams_update(serial, summ)
        except Exception:
            log.exception("AMS")
        self.dash.commit()
        if summ["connected"] and st and prev != st:
            if prev:
                self.log("Drucker", summ["name"], summ["state_text"] + (f" – {summ['file']}" if summ["file"] else ""))
            self._engine_update()

    def tile_action(self, key, action, model):
        kind, _, ident = key.partition(":")
        if key == "dect:_battery":
            self.battery_dialog()
            return
        if key == "dect:_heating":
            self.heating_dialog()
            return
        if key == "calls:list":
            self.calls_dialog()
            return
        if key == "lists:main":
            self.lists_dialog()
            return
        if kind == "plug":
            def run(k=key):
                ok, msg = self.plugins.tap(k)
                self.results.put(("status", ("🧩 " if ok else "⚠ ") + msg))
            threading.Thread(target=run, daemon=True, name="Erweiterung-Tippen").start()
            return
        if kind == "ams":
            self.filament_dialog()
            return
        if kind == "dect" and model.get("type") == "dect":
            d = model["dev"]
            if action == "toggle" and d["present"]:
                # sofort sichtbar umschalten, Bestätigung kommt mit dem nächsten Update
                for f in ("switch", "simpleonoff"):
                    if d[f] is not None:
                        d[f] = not d[f]
                self.dash.tiles[key].redraw()
                self.jobs.put(("action", lambda c, dev=dict(d): c.toggle(dev)))
            elif action in ("mplay", "mpause", "mvolup", "mvoldown") and d.get("media"):
                k = {"mplay": "play", "mpause": "pause", "mvolup": "vol_up", "mvoldown": "vol_down"}[action]
                self.jobs.put(("action", lambda c, dev=dict(d), k=k: c.press(dev, k)))
                self._on_status(f"{d['name']}: {_media_keys()[k]}")
            elif action == "remote" and d.get("media"):
                from pages import RemoteDialog
                RemoteDialog(self, self, d["ain"])
            elif action in ("minus", "plus") and not d["hkr"] and d.get("level") is not None:
                d["level"] = max(0, min(100, (d["level"] or 0) + (10 if action == "plus" else -10)))
                self.dash.tiles[key].redraw()
                ain = d["ain"]
                if ain in self._temp_after:
                    self.after_cancel(self._temp_after[ain])

                def send_lvl(dev=dict(d)):
                    self._temp_after.pop(dev["ain"], None)
                    pct = dev["level"]
                    self.jobs.put(("action", lambda c: c.set_level(dev, pct) if pct else c.set_switch(dev, False)))
                self._temp_after[ain] = self.after(700, send_lvl)
            elif action in ("minus", "plus") and d["hkr"]:
                cur = self.dash.pending_temp.get(d["ain"])
                if cur is None:
                    cur = d["hkr"]["soll"] if isinstance(d["hkr"]["soll"], float) else 20.0
                cur = max(8.0, min(28.0, cur + (0.5 if action == "plus" else -0.5)))
                self.dash.pending_temp[d["ain"]] = cur
                self.dash.tiles[key].redraw()
                ain = d["ain"]
                if ain in self._temp_after:
                    self.after_cancel(self._temp_after[ain])

                def send(ain=ain):
                    self._temp_after.pop(ain, None)
                    val = self.dash.pending_temp.get(ain)
                    if val is not None:
                        self.jobs.put(("action", lambda c: c.set_thermostat(ain, val)))
                self._temp_after[ain] = self.after(900, send)
            elif action == "open":
                DetailWindow(self, d["ain"])
        elif kind == "wlan" and action == "toggle":
            self._wlan_toggle_obj(model["wlan"])
        elif kind == "printer":
            pr = self.printers.get(ident)
            if action == "timelapse":
                try:
                    self._on_status(self.tl_arm(ident, bool(model.get("tl_on"))))
                except RuntimeError as e:
                    self._on_error(str(e))
                return
            if action == "filament":
                self.filament_dialog()
                return
            if action == "edit":
                self.printer_dialog(ident)
                return
            if action == "remove":
                if messagebox.askyesno(APP, f"Drucker „{model['p']['name']}“ entfernen?"):
                    self.remove_printer(ident)
                return
            if not pr or (not pr.connected and action != "camera"):
                return
            try:
                if action == "toggle":
                    pr.set_light(not model["p"]["light"])
                elif action == "pause":
                    self._user_paused.add(ident)
                    pr.pause()
                elif action == "camera":
                    self.open_camera("printer:" + ident)
                elif action == "resume":
                    pr.resume()
                elif action == "stop":
                    if messagebox.askyesno(APP, "Druck wirklich abbrechen?\nDas kann nicht rückgängig gemacht werden.",
                                           icon="warning"):
                        pr.stop_print()
            except Exception as e:
                self._on_error(f"Drucker: {e}")
        elif kind == "cam":
            self.open_camera(key)
        elif kind == "cal":
            self.show_page("calendar")
        elif kind == "person":
            self.persons_dialog()
        elif kind == "box":
            if ident == "main":
                self.show_page("overview")
            else:
                self.boxes_dialog()
        elif kind == "scene":
            rule = next((r for r in self.cfg.get("automations", []) if r["id"] == ident), None)
            if rule:
                self.engine.trigger(rule, reason="Kachel")
                model["busy"] = True
                self.dash.tiles[key].redraw()

                def done(key=key):
                    t = self.dash.tiles.get(key)
                    if t:
                        t.model["busy"] = False
                        t.redraw()
                self.after(1800, done)
        elif kind == "weather":
            self.weather_dialog()
        elif kind == "guest":
            if action == "toggle":
                try:
                    self.set_guest(not model["wlan"]["enabled"], source="Kachel")
                except RuntimeError as e:
                    self._on_error(str(e))
            else:
                self.guest_dialog()
        elif kind == "dev":
            if action == "remove":
                if messagebox.askyesno(APP, f"„{model.get('name')}“ aus Hauskern entfernen?\n(Am Gerät selbst ändert "
                                            "sich nichts.)"):
                    self.remove_device(ident)
            elif action in ("edit", "open"):
                self.edit_device(ident)
        elif kind == "wol":
            if action == "remove":
                self.remove_wol(ident)
            elif action == "run" or (action == "open" and not model.get("online")):
                try:
                    self.wake(ident, model.get("name", ""))
                    model["busy_until"] = time.time() + 120
                    model["busy"] = True
                    self.dash.tiles[key].redraw()
                except RuntimeError as e:
                    self._on_error(str(e))
        elif kind == "ping":
            self.nb.select(self.tab_ping)
        elif kind == "net":
            self.nb.select(self.tab_hosts if ident == "hosts" else self.tab_overview)

    # ---------------- 3D-Drucker ----------------
    def start_printers(self):
        for p in self.printers.values():
            p.stop()
        self.printers = {}
        for cfg in self.cfg.get("printers", []):
            code = unprotect(cfg.get("code", "")) or cfg.get("code_plain", "")
            pr = BambuPrinter(cfg["name"], cfg["ip"], cfg["serial"], code,
                              lambda serial, summ: self.results.put(("printer", (serial, summ))))
            self.printers[pr.serial] = pr
            self.dash.put("3D-Druck", "printer:" + pr.serial, {"type": "printer", "p": pr.summary()}, cols=2, rows=2)
            pr.start()
        self.dash.remove_missing("3D-Druck", "printer:", ["printer:" + k for k in self.printers])
        self.dash.commit()

    def remove_printer(self, serial):
        self.cfg["printers"] = [p for p in self.cfg.get("printers", []) if p["serial"].strip() != serial]
        self.save()
        self.start_printers()

    def printer_dialog(self, serial=None, prefill=None):
        cur = next((p for p in self.cfg.get("printers", []) if p["serial"].strip() == serial), None) if serial \
            else (prefill or None)
        plugs = [(d["ain"], d["name"]) for d in self.data["smart"] if d["power_w"] is not None and not d["is_group"]]
        code = (unprotect(cur.get("code", "")) or cur.get("code_plain", "")) if cur and serial else ""
        dlg = PrinterDialog(self, cur, code, plugs)
        self.wait_window(dlg)
        if not dlg.result:
            return
        self.save_printer(dlg.result, old_serial=serial)

    def printer_code(self, entry):
        return unprotect(entry.get("code", "")) or entry.get("code_plain", "")

    def save_printer(self, r, old_serial=None):
        entry = {"name": r["name"], "ip": r["ip"], "serial": r["serial"], "plug_ain": r.get("plug_ain", "")}
        enc = protect(r["code"])
        if enc:
            entry["code"] = enc
        else:
            entry["code_plain"] = r["code"]
        lst = [p for p in self.cfg.get("printers", []) if p["serial"].strip() not in (old_serial, r["serial"])]
        lst.append(entry)
        self.cfg["printers"] = lst
        self.save()
        self.log("3D-Druck", r["name"], "gespeichert")
        self.start_printers()

    def save(self):
        save_cfg(self.cfg)

    # ---------------- Sortierung ----------------
    def _sort_by(self, key, col):
        cur = self.sort.get(key)
        self.sort[key] = (col, not cur[1] if cur and cur[0] == col else False)
        if key == "hosts":
            self._render_hosts()
        elif key == "smart":
            self._on_smart(self._fritz_devs, tiles=False, _ext=True)

    def _apply_sort(self, key, rows):
        if key not in self.sort:
            return
        col, rev = self.sort[key]
        idx = self.cols[key].index(col)
        if col == "ip":
            rows.sort(key=lambda r: ip_key(r[1][idx]), reverse=rev)
        else:
            rows.sort(key=lambda r: str(r[1][idx]).lower(), reverse=rev)

    # ---------------- Aktionen ----------------
    def _selected(self, tree):
        sel = tree.selection()
        return sel[0] if sel else None

    def _host_open(self):
        iid = self._selected(self.t_hosts)
        if iid and not iid.startswith("grp:"):
            ip = self.t_hosts.item(iid, "values")[2]
            if ip:
                webbrowser.open(f"http://{ip}")

    def _host_to_ping(self):
        iid = self._selected(self.t_hosts)
        if not iid or iid.startswith("grp:"):
            messagebox.showinfo(APP, "Bitte zuerst ein Gerät auswählen.")
            return
        vals = self.t_hosts.item(iid, "values")
        host, name = (vals[2] or vals[1]), (vals[1] or vals[2])
        if not host:
            return
        targets = self.cfg.setdefault("ping_targets", [])
        if any(t["host"] == host for t in targets):
            messagebox.showinfo(APP, f"„{name}“ ist schon im Ping-Monitor.")
            return
        targets.append({"name": name, "host": host})
        save_cfg(self.cfg)
        self._render_ping()
        self.jobs.put(("refresh",))
        self.nb.select(self.tab_ping)

    def _ping_add(self):
        host = simpledialog.askstring(APP, "IP-Adresse oder Hostname:", parent=self)
        if not host:
            return
        name = simpledialog.askstring(APP, "Anzeigename:", initialvalue=host, parent=self) or host
        self.cfg.setdefault("ping_targets", []).append({"name": name.strip(), "host": host.strip()})
        save_cfg(self.cfg)
        self._render_ping()
        self.jobs.put(("refresh",))

    def _ping_remove(self):
        iid = self._selected(self.t_ping)
        if iid:
            self.cfg["ping_targets"] = [t for t in self.cfg["ping_targets"] if t["host"] != iid]
            save_cfg(self.cfg)
            self._render_ping()

    @staticmethod
    def _wkey(x):
        return str(x["index"]) if x.get("box", "main") == "main" else f"{x['box']}-{x['index']}"

    def _wlan_toggle(self):
        iid = self._selected(self.t_wlan)
        if not iid:
            messagebox.showinfo(APP, "Bitte zuerst ein WLAN auswählen.")
            return
        w = next((x for x in self.data["wlan"] if self._wkey(x) == iid), None)
        if w:
            self._wlan_toggle_obj(w)

    def _wlan_toggle_obj(self, w):
        action = "ausschalten" if w["enabled"] else "einschalten"
        warn = ("\n\nAchtung: Wenn dein PC über dieses WLAN verbunden ist, verlierst du die Verbindung!"
                if w["enabled"] else "")
        where = f" auf {w['box_name']}" if w.get("box_name") else ""
        if messagebox.askyesno(APP, f"WLAN „{w['ssid']}“ ({w['band']}{where}) wirklich {action}?{warn}"):
            idx, en, box = w["index"], not w["enabled"], w.get("box", "main")
            if box == "main":
                self.jobs.put(("action", lambda c: c.set_wlan(idx, en), "full"))
            else:
                cl = self.extra_clients.get(box)
                if cl:
                    self.jobs.put(("action", lambda c: cl.set_wlan(idx, en), "full"))

    def _smart_dev(self):
        iid = self._selected(self.t_smart)
        return next((d for d in self.data["smart"] if d["ain"] == iid), None) if iid else None

    def _smart_toggle(self):
        d = self._smart_dev()
        if not d:
            messagebox.showinfo(APP, "Bitte zuerst ein Gerät auswählen.")
            return
        if d["switch"] is None and d["simpleonoff"] is None and not d["is_group"]:
            messagebox.showinfo(APP, f"„{d['name']}“ kann nicht geschaltet werden.")
            return
        self.jobs.put(("action", lambda c: c.toggle(d)))

    def _smart_temp(self):
        d = self._smart_dev()
        if not d or not d["hkr"]:
            messagebox.showinfo(APP, "Bitte einen Heizkörperregler auswählen.")
            return
        cur = d["hkr"]["soll"]
        s = simpledialog.askstring(
            APP, f"Neue Solltemperatur für „{d['name']}“\n(8 bis 28 °C, oder „aus“ / „an“):",
            initialvalue="" if isinstance(cur, str) or cur is None else str(cur).replace(".", ","), parent=self)
        if not s:
            return
        s = s.strip().lower().replace("°c", "").replace(",", ".").strip()
        if s in ("aus", "an"):
            val = s
        else:
            try:
                val = float(s)
            except ValueError:
                messagebox.showwarning(APP, "Bitte eine Zahl wie 21 oder 20,5 eingeben.")
                return
            if not 8 <= val <= 28:
                messagebox.showwarning(APP, "Erlaubt sind 8 bis 28 °C.")
                return
        ain = d["ain"]
        self.jobs.put(("action", lambda c: c.set_thermostat(ain, val)))

    def _smart_double(self):
        d = self._smart_dev()
        if d and d["hkr"]:
            self._smart_temp()
        elif d:
            self._smart_toggle()

    # ---------------- Einstellungen / Beenden ----------------
    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg, self.password)
        self.wait_window(dlg)
        r = dlg.result
        if not r:
            return
        reconnect = (r["host"], r["user"], r["password"]) != (self.cfg["host"], self.cfg["user"], self.password) \
            or not self.client or r.get("router", "fritz") != self.router_kind()
        self.cfg["router"] = r.get("router", "fritz")
        if self.cfg["router"] == "none":
            reconnect = False
            self.client = None
        self._fritz_only_hints()
        self.cfg.update(host=r["host"], user=r["user"], interval=r["interval"], price_ct=r["price_ct"],
                        tray=r["tray"], notify=r["notify"])
        w = dlg.web
        if w["logout"]:
            self.cfg["web_sessions"] = []
        if w["enabled"] and not self.cfg.get("web_users"):
            messagebox.showinfo(APP, "Lege bitte zuerst einen Handy-Nutzer mit PIN an.")
            self.users_dialog()
            if not self.cfg.get("web_users"):
                w["enabled"] = False
        changed = (w["enabled"], w["port"]) != (self.cfg.get("web_enabled"), self.cfg.get("web_port"))
        self.cfg.update(web_enabled=w["enabled"], web_port=w["port"])
        if changed:
            self.start_web(show_info=w["enabled"])
        self.password = r["password"]
        self.cfg["password"] = protect(r["password"]) if r["save"] else ""
        save_cfg(self.cfg)
        try:
            set_autostart(APP, os.path.abspath(__file__), r["autostart"])
        except OSError as e:
            messagebox.showwarning(APP, f"Autostart konnte nicht gesetzt werden: {e}")
        self.refresh_energy(reschedule=False)
        if reconnect:
            self.client = None
            self.hist.clear()
            self.jobs.put(("connect",))

    def _quit(self):
        self.stop.set()  # zuerst: ab jetzt nichts mehr anzeigen
        self.probe_stop()
        for w in self.watchers.values():
            w.stop()
        try:
            self.store.flush_series()
        except Exception:
            log.exception("Verlauf speichern")
        if self.web:
            self.web.stop()
        self.engine.stop()
        try:
            self.integrations.stop()
        except Exception:
            pass
        try:
            self.plugins.stop()
        except Exception:  # noqa: BLE001
            log.exception("Erweiterungen beenden")
        for v in list(self.camviews.values()):
            v.close()
        for e in self.camobjs.values():
            if e.get("thumb"):
                e["thumb"].stop()
        self.tray.close()
        for p in self.printers.values():
            p.stop()
        self.stop.set()
        self.jobs.put(("quit",))
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


class _NoTray:
    ok = False

    def notify(self, title, text, warning=False):
        log.info("Hinweis: %s – %s", title, text)

    def close(self):
        pass


def _silence_dialogs():
    """Im Hintergrundbetrieb sieht niemand Dialoge – sie würden nur hängen bleiben. Stattdessen ins Protokoll."""
    def note(level):
        return lambda title=None, message=None, **kw: log.log(level, "Dialog: %s", message or title)
    messagebox.showinfo = note(20)
    messagebox.showwarning = note(30)
    messagebox.showerror = note(40)
    messagebox.askyesno = lambda *a, **kw: False


def reset_twofa_cli():
    """Notausgang am Heimserver, wenn kein Admin mehr reinkommt:
        sudo systemctl stop hauskern
        python3 ~/Hauskern/Hauskern.pyw --2fa-zuruecksetzen [Name]
        sudo systemctl start hauskern"""
    import totp
    i = sys.argv.index("--2fa-zuruecksetzen")
    name = " ".join(a for a in sys.argv[i + 1:] if not a.startswith("--")).strip().lower()
    cfg = load_cfg()
    hit = [u for u in cfg.get("web_users") or [] if totp.enabled(u) and (not name or u.get("name", "").lower() == name)]
    for u in hit:
        totp.disable(u)
    if hit:
        save_cfg(cfg)
    print(f"Zwei-Faktor ausgeschaltet für: {', '.join(u['name'] for u in hit)}" if hit else
          "Niemand gefunden, bei dem Zwei-Faktor an ist" + (f" (Name „{name}“)" if name else ""))
    print("Jetzt Hauskern wieder starten:  sudo systemctl start hauskern\n"
          "(Lief der Dienst dabei noch, kann er die Änderung überschreiben – dann erst „sudo systemctl stop hauskern“ "
          "und diesen Befehl noch einmal.)")


def main():
    if "--2fa-zuruecksetzen" in sys.argv:
        reset_twofa_cli()
        return
    if "--after-update" in sys.argv:
        time.sleep(2.5)  # der alte Prozess gibt noch Port & Fenster frei
    applog.setup(_data_dir(), VERSION)
    try:  # bereitgelegte Sicherung einspielen – bevor Einstellungen und Datenbank geöffnet werden
        import backup
        backup.apply_pending(_data_dir(), log=log.warning)
    except Exception:
        log.exception("Sicherung einspielen")
    if HEADLESS:
        _silence_dialogs()
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # scharfe Schrift bei Skalierung
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
