# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
pages.py – Seiten und Dialoge: Kameras, Kamera-Vollansicht, Automationen, Logbuch.
"""
import base64
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import automations as A
from camera import CameraStream, Recorder, SetupError, check_ffmpeg, find_ffmpeg, install_ffmpeg
from charts import dstr
from dashboard import ACCENT, BAD, BG, CARD, CARD2, FG, MUTED, OK, WARN, AMBER, draw_icon, rrect
from onvif import OnvifCamera, OnvifError, discover

APP = "Hauskern"


def media_dir(kind):
    base = os.path.join(os.path.expanduser("~"), "Pictures" if kind == "foto" else "Videos")
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    return os.path.join(base, APP)


def open_folder(path):
    try:
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
    except OSError:
        pass


def ui_later(w, fn):
    """Ergebnis aus einem Hintergrund-Thread anzeigen – aber nur, wenn das Fenster noch offen ist."""
    def run():
        try:
            if not getattr(w, "_closed", False) and w.winfo_exists():
                fn()
        except tk.TclError:
            pass  # Fenster wurde in der Zwischenzeit geschlossen
    try:
        w.after(0, run)
    except (tk.TclError, RuntimeError):
        pass


def _center(win, master):
    win.update_idletasks()
    x = master.winfo_rootx() + (master.winfo_width() - win.winfo_width()) // 2
    y = master.winfo_rooty() + (master.winfo_height() - win.winfo_height()) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


# =========================================================================== #
# Kamera-Vollansicht (IP-Kameras und Drucker)
# =========================================================================== #
class CamView(tk.Toplevel):
    """url_func(sub: bool) -> RTSP-URL (darf blockieren). onvif: OnvifCamera oder None."""

    def __init__(self, app, key, name, url_func, onvif=None, on_close=None):
        super().__init__(app)
        self.app, self.key, self.name = app, key, name
        self.url_func, self.onvif, self.on_close = url_func, onvif, on_close
        self.title(f"{name} – Kamera")
        self.configure(bg=BG)
        self.geometry("1180x700")
        self.minsize(720, 460)
        self.stream = self.rec = None
        self.img = None
        self.last_no = -1
        self.sub = False
        self.speed = tk.DoubleVar(value=0.5)
        self._ptz_q = queue.Queue()
        self._cancel = False
        self._closed = False
        threading.Thread(target=self._ptz_worker, daemon=True).start()

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.lbl = tk.Label(body, bg="#000000", fg=MUTED, text="Verbinde …", font=("Segoe UI", 11), justify="center")
        self.lbl.pack(side="left", fill="both", expand=True)
        self.side = ttk.Frame(body, padding=(12, 0, 0, 0))
        if onvif is not None and onvif.has_ptz:
            self.side.pack(side="right", fill="y")
            self._build_ptz()

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        self.l_status = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_status.pack(side="left")
        self.b_setup = ttk.Button(bar, text="Kamera einrichten", style="Accent.TButton", command=self.setup)
        ttk.Button(bar, text="Vollbild", command=self.toggle_full).pack(side="right")
        self.b_rec = ttk.Button(bar, text="● Aufnahme", command=self.toggle_rec)
        self.b_rec.pack(side="right", padx=6)
        ttk.Button(bar, text="Schnappschuss", command=self.snapshot).pack(side="right")
        if onvif is not None:
            self.b_q = ttk.Button(bar, text="Qualität: HD", command=self.toggle_quality)
            self.b_q.pack(side="right", padx=6)
        ttk.Button(bar, text="Ordner", command=lambda: open_folder(media_dir("foto"))).pack(side="right", padx=6)
        ttk.Button(bar, text="Neu verbinden", command=self.connect).pack(side="right")

        self.bind("<F11>", lambda e: self.toggle_full())
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        for k, v in (("<Left>", (-1, 0)), ("<Right>", (1, 0)), ("<Up>", (0, 1)), ("<Down>", (0, -1))):
            self.bind(k, lambda e, v=v: self._move(*v))
            self.bind(k.replace("<", "<KeyRelease-"), lambda e: self._stop_move())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.connect()
        self._tick()

    # ---------------- PTZ-Bedienfeld ----------------
    def _build_ptz(self):
        s = self.side
        ttk.Label(s, text="Steuerung", style="H1.TLabel").pack(anchor="w")
        ttk.Label(s, text="Gedrückt halten · Pfeiltasten gehen auch", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        pad = tk.Frame(s, bg=BG)
        pad.pack(pady=4)
        dirs = [("↖", -1, 1), ("↑", 0, 1), ("↗", 1, 1), ("←", -1, 0), ("■", 0, 0), ("→", 1, 0),
                ("↙", -1, -1), ("↓", 0, -1), ("↘", 1, -1)]
        for i, (txt, x, y) in enumerate(dirs):
            b = tk.Label(pad, text=txt, width=4, height=2, bg=CARD2 if txt != "■" else "#343944", fg=FG,
                         font=("Segoe UI Semibold", 13), cursor="hand2")
            b.grid(row=i // 3, column=i % 3, padx=3, pady=3)
            if txt == "■":
                b.bind("<Button-1>", lambda e: self._stop_move())
            else:
                b.bind("<ButtonPress-1>", lambda e, x=x, y=y: self._move(x, y))
                b.bind("<ButtonRelease-1>", lambda e: self._stop_move())
        zf = tk.Frame(s, bg=BG)
        zf.pack(pady=(8, 4))
        for txt, z in (("Zoom −", -1), ("Zoom +", 1)):
            b = tk.Label(zf, text=txt, width=9, height=2, bg=CARD2, fg=FG, font=("Segoe UI Semibold", 10),
                         cursor="hand2")
            b.pack(side="left", padx=3)
            b.bind("<ButtonPress-1>", lambda e, z=z: self._move(0, 0, z))
            b.bind("<ButtonRelease-1>", lambda e: self._stop_move())
        ttk.Label(s, text="Geschwindigkeit", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Scale(s, from_=0.1, to=1.0, variable=self.speed, orient="horizontal").pack(fill="x")
        ttk.Label(s, text="Positionen", style="H1.TLabel").pack(anchor="w", pady=(16, 4))
        self.lb = tk.Listbox(s, height=8, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.lb.pack(fill="x")
        self.lb.bind("<Double-1>", lambda e: self._goto())
        pf = ttk.Frame(s)
        pf.pack(fill="x", pady=6)
        ttk.Button(pf, text="Anfahren", command=self._goto).pack(side="left")
        ttk.Button(pf, text="Speichern …", command=self._save_preset).pack(side="left", padx=4)
        ttk.Button(pf, text="Löschen", command=self._del_preset).pack(side="left")
        self.presets = []
        self._load_presets()

    def _ptz_worker(self):
        while True:
            job = self._ptz_q.get()
            if job is None:
                return
            try:
                job()
            except OnvifError as e:
                ui_later(self, lambda e=e: self.l_status.configure(text=f"⚠ {e}", foreground=BAD))
            except Exception:
                pass

    def _move(self, x, y, z=0):
        if self.onvif is None or not self.onvif.has_ptz:
            return
        sp = self.speed.get()
        self._ptz_q.put(lambda: self.onvif.move(x * sp, y * sp, z * sp))

    def _stop_move(self):
        if self.onvif is not None and self.onvif.has_ptz:
            self._ptz_q.put(self.onvif.stop)

    def _load_presets(self):
        def job():
            pr = self.onvif.presets()
            ui_later(self, lambda: self._show_presets(pr))
        self._ptz_q.put(job)

    def _show_presets(self, pr):
        if self._closed:
            return
        self.presets = pr
        self.lb.delete(0, "end")
        for p in pr:
            self.lb.insert("end", p["name"])
        if not pr:
            self.lb.insert("end", "(noch keine gespeichert)")

    def _sel_preset(self):
        i = self.lb.curselection()
        return self.presets[i[0]] if i and i[0] < len(self.presets) else None

    def _goto(self):
        p = self._sel_preset()
        if p:
            self._ptz_q.put(lambda: self.onvif.goto_preset(p["token"]))
            self.app.log("Kamera", self.name, f"Position „{p['name']}“ angefahren")

    def _save_preset(self):
        name = simpledialog.askstring(APP, "Name für die aktuelle Position:", parent=self)
        if name:
            self._ptz_q.put(lambda: (self.onvif.save_preset(name.strip()), self._load_presets()))

    def _del_preset(self):
        p = self._sel_preset()
        if p and messagebox.askyesno(APP, f"Position „{p['name']}“ löschen?", parent=self):
            self._ptz_q.put(lambda: (self.onvif.remove_preset(p["token"]), self._load_presets()))

    # ---------------- Stream ----------------
    def connect(self):
        if self.stream:
            self.stream.stop()
            self.stream = None
        self.last_no = -1
        self.b_setup.pack_forget()
        if not find_ffmpeg() and os.name != "nt":
            self.lbl.configure(image="", text="Für Livebilder wird ffmpeg benötigt:\n\nsudo apt install ffmpeg")
            return
        if not find_ffmpeg():
            self.lbl.configure(image="", text=(
                "Für Livebilder braucht die App einmalig das kostenlose Videoprogramm ffmpeg.\n\n"
                "Klick auf „Kamera einrichten“ – die App lädt es selbst herunter (ca. 90 MB),\n"
                "prüft die Datei und legt sie in ihren eigenen Ordner. Nichts wird installiert."))
            self.b_setup.pack(side="right", padx=6)
            return
        self.lbl.configure(image="", text="Verbinde …")

        def start():
            try:
                url = self.url_func(self.sub)
            except Exception as e:  # noqa: BLE001
                ui_later(self, lambda m=str(e): self.lbl.configure(text=f"Kamera nicht erreichbar:\n{m}"))
                return
            st = CameraStream(url, fps=12, width=640 if self.sub else 1280)
            ok = st.start()
            ui_later(self, lambda: self._started(st, ok, url))
        threading.Thread(target=start, daemon=True).start()

    def _started(self, st, ok, url):
        if self._closed:
            st.stop()
            return
        self.stream, self.url = st, url
        if not ok:
            self.lbl.configure(text=st.error)

    def toggle_quality(self):
        self.sub = not self.sub
        self.b_q.configure(text="Qualität: SD" if self.sub else "Qualität: HD")
        self.connect()

    def toggle_full(self):
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    def snapshot(self):
        if not (self.stream and self.stream.frame):
            return
        folder = media_dir("foto")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{self.name} {time.strftime('%Y-%m-%d %H-%M-%S')}.png")
        with open(path, "wb") as f:
            f.write(self.stream.frame)
        self.l_status.configure(text=f"📸 Gespeichert: {path}", foreground=OK)
        self.app.log("Kamera", self.name, "Schnappschuss gespeichert")

    def toggle_rec(self):
        if self.rec and self.rec.active:
            path = self.rec.stop()
            self.b_rec.configure(text="● Aufnahme")
            self.l_status.configure(text=f"🎬 Gespeichert: {path}", foreground=OK)
            self.app.log("Kamera", self.name, "Aufnahme beendet")
            return
        if not getattr(self, "url", None):
            return
        try:
            self.rec = Recorder(self.url, media_dir("video"), self.name)
            self.rec.start()
            self.app.log("Kamera", self.name, "Aufnahme gestartet")
        except (SetupError, OSError) as e:
            messagebox.showwarning(APP, str(e), parent=self)

    def _tick(self):
        if self._closed:
            return
        s = self.stream
        if self.rec and self.rec.active:
            secs = int(time.time() - self.rec.started)
            self.b_rec.configure(text=f"■ Stopp {secs // 60:02d}:{secs % 60:02d}")
        if s and s.frame_no != self.last_no and s.frame:
            self.last_no = s.frame_no
            self._retries = 0
            try:
                img = tk.PhotoImage(data=base64.b64encode(s.frame).decode())
                w, h = max(1, self.lbl.winfo_width()), max(1, self.lbl.winfo_height())
                f = max(1, -(-max(img.width() / w, img.height() / h) // 1))
                self.img = img.subsample(int(f)) if f > 1 else img
                self.lbl.configure(image=self.img, text="")
                if not (self.rec and self.rec.active):
                    self.l_status.configure(text="● Live · " + time.strftime("%H:%M:%S"), foreground=OK)
            except tk.TclError:
                pass
        elif s and not s.running and s.error:
            # automatisch neu verbinden (max. 3x hintereinander)
            if getattr(self, "_retries", 0) < 3 and not getattr(self, "_retry_pending", False):
                self._retries = getattr(self, "_retries", 0) + 1
                self._retry_pending = True

                def again():
                    self._retry_pending = False
                    if not self._closed:
                        self.connect()
                self.after(3000, again)
            if self.last_no < 0:
                hint = ""
                if "Smart App Control" in s.error:
                    hint = ("\n\nWindows sperrt ffmpeg. Freigabe unter Windows-Sicherheit →\n"
                            "App- & Browsersteuerung → Smart App Control.")
                elif self.key.startswith("printer:"):
                    hint = ("\n\nSo klappt's beim Bambu-Drucker:\n"
                            "1. Am Druckerdisplay: Einstellungen → Netzwerk → „LAN Only Liveview“ einschalten\n"
                            "2. Bambu Studio / Handy-App kurz schließen – der Drucker erlaubt nur eine lokale Live-Verbindung\n"
                            "3. Drucker-IP und Zugangscode prüfen (Rechtsklick auf die Druckerkachel → Bearbeiten)")
                elif "nicht erreichbar" in s.error:
                    hint = "\n\nIst die Kamera an und im selben Netz?"
                self.lbl.configure(image="", text=s.error + hint)
            self.l_status.configure(text="⚠ " + s.error, foreground=BAD)
        self.after(80, self._tick)

    # ---------------- ffmpeg einrichten ----------------
    def setup(self):
        self.b_setup.pack_forget()
        self.pb = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pb.pack(fill="x", padx=10, pady=(0, 6), after=self.lbl.master)
        st = self._setup = {"done": 0, "total": 0, "result": None, "error": None}

        def run():
            try:
                exe = install_ffmpeg(lambda d, t: st.update(done=d, total=t), lambda: self._cancel)
                st["result"] = check_ffmpeg(exe)
            except SetupError as e:
                st["error"] = str(e)
            except Exception as e:
                st["error"] = f"{type(e).__name__}: {e}"
        threading.Thread(target=run, daemon=True).start()
        self._setup_tick()

    def _setup_tick(self):
        if self._closed:
            return
        st = self._setup
        if st["error"] or st["result"]:
            self.pb.destroy()
            if st["error"]:
                self.lbl.configure(text=f"Einrichtung fehlgeschlagen: {st['error']}\n\n"
                                        "Ist der PC mit dem Internet verbunden? Einfach nochmal versuchen.")
                self.b_setup.configure(text="Nochmal versuchen")
                self.b_setup.pack(side="right", padx=6)
            elif st["result"][0]:
                self.l_status.configure(text="ffmpeg bereit ✓", foreground=OK)
                self.connect()
            else:
                self.lbl.configure(text=st["result"][1] + "\n\nffmpeg wurde heruntergeladen, darf aber nicht starten.\n"
                                   "Freigabe unter Windows-Sicherheit → App- & Browsersteuerung → Smart App Control.")
            return
        if st["total"]:
            self.pb.configure(value=st["done"] * 100 / st["total"])
            self.lbl.configure(text=f"Lade ffmpeg herunter … {st['done'] / 1e6:.0f} von {st['total'] / 1e6:.0f} MB")
        else:
            self.lbl.configure(text=f"Lade ffmpeg herunter … {st['done'] / 1e6:.0f} MB")
        self.after(200, self._setup_tick)

    def close(self):
        self._closed = self._cancel = True
        if self.rec and self.rec.active:
            self.rec.stop()
            self.app.log("Kamera", self.name, "Aufnahme beendet")
        if self.stream:
            self.stream.stop()
        self._stop_move()
        self._ptz_q.put(None)
        if self.on_close:
            self.on_close()
        self.destroy()


# =========================================================================== #
# Kamera hinzufügen / suchen
# =========================================================================== #
BRANDS = [("tapo", "TP-Link Tapo", 2020), ("reolink", "Reolink", 8000), ("hik", "Hikvision / Dahua / Annke", 80),
          ("onvif", "Andere (ONVIF)", 80), ("rtsp", "Nur RTSP-Adresse", 0)]


def guess_brand(info):
    txt = f"{info.get('name', '')} {info.get('hardware', '')}".lower()
    if "tapo" in txt or info.get("port") == 2020:
        return "tapo"
    if "reolink" in txt:
        return "reolink"
    if any(b in txt for b in ("hikvision", "dahua", "annke")):
        return "hik"
    return "onvif"


class CameraDialog(tk.Toplevel):
    def __init__(self, master, cur=None, password=""):
        super().__init__(master)
        cur = cur or {}
        self.title("Kamera bearbeiten" if cur.get("id") else "Kamera hinzufügen")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="IP-Kamera (ONVIF / RTSP)", style="H1.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        brand = cur.get("brand") or ("tapo" if int(cur.get("port", 80) or 80) == 2020 else "onvif")
        ttk.Label(f, text="Hersteller").grid(row=10, column=0, sticky="w", pady=4)
        self.v_brand = tk.StringVar(value=next(n for k, n, _ in BRANDS if k == brand))
        cb = ttk.Combobox(f, textvariable=self.v_brand, values=[n for _, n, _ in BRANDS], state="readonly", width=28)
        cb.grid(row=10, column=1, columnspan=2, sticky="w", pady=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._brand_changed())
        self.v = {k: tk.StringVar(value=str(val)) for k, val in (
            ("name", cur.get("name", "Kamera")), ("host", cur.get("host", "")), ("port", cur.get("port", 80)),
            ("user", cur.get("user", "")), ("pw", password), ("rtsp", cur.get("rtsp", "")))}
        rows = [("name", "Name", ""), ("host", "IP-Adresse", ""), ("port", "ONVIF-Port", ""),
                ("user", "Benutzer", ""), ("pw", "Passwort", "•"), ("rtsp", "RTSP-Adresse (optional)", "")]
        for i, (k, lbl, show) in enumerate(rows, start=11):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Entry(f, textvariable=self.v[k], width=40 if k == "rtsp" else 30, show=show).grid(
                row=i, column=1, columnspan=2, sticky="we", pady=4)
        mo = cur.get("motion") or {}
        mf = ttk.Frame(f)
        mf.grid(row=17, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.v_motion = tk.BooleanVar(value=bool(mo.get("enabled")))
        ttk.Checkbutton(mf, text="Bewegung erkennen", variable=self.v_motion).pack(side="left")
        ttk.Label(mf, text="  über").pack(side="left")
        self.v_mmode = tk.StringVar(value=dict(MOTION_MODES).get(mo.get("mode", "auto")))
        ttk.Combobox(mf, textvariable=self.v_mmode, values=[n for _, n in MOTION_MODES], state="readonly",
                     width=26).pack(side="left", padx=6)
        ttk.Label(mf, text="Empfindlichkeit").pack(side="left", padx=(6, 0))
        self.v_msens = tk.StringVar(value=mo.get("sens", "mittel"))
        ttk.Combobox(mf, textvariable=self.v_msens, values=["niedrig", "mittel", "hoch"], state="readonly",
                     width=8).pack(side="left", padx=6)
        ttk.Label(f, text="Was bei Bewegung passiert, legst du unter Automationen fest (Auslöser „Kamera erkennt "
                          "Bewegung“) –\nz. B. nur wenn niemand zu Hause ist.", style="Muted.TLabel",
                  justify="left").grid(row=18, column=0, columnspan=3, sticky="w")
        self.hint = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.hint.grid(row=19, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._brand_changed(initial=True)
        self.l_test = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_test.grid(row=20, column=0, columnspan=3, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=21, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(b, text="Verbindung testen", command=self._test).pack(side="right", padx=6)
        self.cur = cur
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        _center(self, master)

    HINTS = {
        "tapo": "Tapo-App → Kamera → ⚙ → Erweiterte Einstellungen → Kamerakonto: Benutzer + Passwort anlegen\n"
                "und hier eintragen (nicht dein TP-Link-Cloud-Konto!). ONVIF-Port 2020.\n"
                "Schwenken/Neigen klappt bei aktueller Firmware per ONVIF. Falls nicht: Firmware in der Tapo-App updaten.\n"
                "Hinweis: Tapo Care, SD-Karten-Aufnahme und Fremd-Apps laufen nur zu zweit gleichzeitig.",
        "reolink": "ONVIF in der Reolink-App bzw. im Web-Interface unter Netzwerk → Erweitert → Port-Einstellungen\n"
                   "aktivieren. ONVIF-Port meist 8000.",
        "hik": "ONVIF im Web-Interface der Kamera aktivieren und einen ONVIF-Benutzer anlegen. Port meist 80.",
        "onvif": "ONVIF in der Kamera aktivieren. Den Port findest du in deren Netzwerkeinstellungen.",
        "rtsp": "Ohne ONVIF: RTSP-Adresse eintragen, z. B. rtsp://192.168.178.50:554/stream1 (dann ohne Schwenken).",
    }

    def _brand(self):
        return next(k for k, n, _ in BRANDS if n == self.v_brand.get())

    def _brand_changed(self, initial=False):
        b = self._brand()
        port = next(p for k, _, p in BRANDS if k == b)
        if port and (not initial or not self.v["port"].get() or self.v["port"].get() == "80"):
            self.v["port"].set(str(port))
        self.hint.configure(text=self.HINTS[b])

    def _vals(self):
        r = {k: v.get().strip() for k, v in self.v.items()}
        r["pw"] = self.v["pw"].get()
        r["brand"] = self._brand()
        try:
            r["port"] = int(r["port"] or 80)
        except ValueError:
            r["port"] = 80
        return r

    def _test(self):
        r = self._vals()
        if not r["host"] and not r["rtsp"]:
            return
        self.l_test.configure(text="Teste …", foreground=MUTED)

        def run():
            if not r["host"] or r["brand"] == "rtsp":
                msg, col = "Nur RTSP – wird beim Öffnen getestet", MUTED
            else:
                try:
                    c = OnvifCamera(r["host"], r["port"], r["user"], r["pw"]).connect()
                    model = " ".join(x for x in (c.info.get("Manufacturer"), c.info.get("Model")) if x) or "Kamera"
                    msg = f"✓ {model} gefunden · {len(c.profiles)} Videoprofile · " + \
                          ("schwenkbar (PTZ)" if c.has_ptz else "feste Kamera")
                    col = OK
                except OnvifError as e:
                    msg, col = f"⚠ {e}", BAD
            ui_later(self, lambda: self.l_test.configure(text=msg, foreground=col) if self.winfo_exists() else None)
        threading.Thread(target=run, daemon=True).start()

    def _ok(self):
        r = self._vals()
        if not r["host"] and not r["rtsp"]:
            messagebox.showwarning(APP, "Bitte IP-Adresse oder RTSP-Adresse eintragen.", parent=self)
            return
        r["id"] = self.cur.get("id") or f"c{int(time.time() * 1000) % 10**9}"
        r["name"] = r["name"] or "Kamera"
        r["motion"] = {"enabled": self.v_motion.get(), "sens": self.v_msens.get(),
                       "mode": next(k for k, n in MOTION_MODES if n == self.v_mmode.get())}
        self.result = r
        self.destroy()


MOTION_MODES = [("auto", "automatisch"), ("camera", "Erkennung der Kamera"), ("image", "Bildvergleich (Hauskern)")]


class DiscoverDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Kameras im Netzwerk suchen")
        self.configure(bg=BG)
        self.geometry("620x380")
        self.transient(master)
        self.result = None
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        self.l = ttk.Label(f, text="Suche läuft … (ca. 4 Sekunden)", style="Muted.TLabel")
        self.l.pack(anchor="w")
        self.tree = ttk.Treeview(f, columns=("name", "host", "port"), show="headings", height=10)
        for c, t, w in (("name", "Name", 260), ("host", "Adresse", 160), ("port", "ONVIF-Port", 90)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, pady=8)
        self.tree.bind("<Double-1>", lambda e: self._ok())
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Übernehmen", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(b, text="Nochmal suchen", command=self.search).pack(side="left")
        self.found = []
        self.grab_set()
        self.search()

    def search(self):
        self.l.configure(text="Suche läuft … (ca. 4 Sekunden)")

        def run():
            try:
                res = discover(4.0)
            except OSError as e:
                ui_later(self, lambda m=str(e): self.l.configure(text=f"Suche nicht möglich: {m}"))
                return
            ui_later(self, lambda: self._show(res))
        threading.Thread(target=run, daemon=True).start()

    def _show(self, res):
        if not self.winfo_exists():
            return
        self.found = res
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(res):
            self.tree.insert("", "end", iid=str(i), values=(c["name"] or c["hardware"] or "Kamera", c["host"], c["port"]))
        self.l.configure(text=f"{len(res)} Kamera(s) gefunden. Doppelklick zum Übernehmen." if res else
                         "Keine Kamera gefunden. Ist ONVIF in der Kamera aktiviert? Du kannst sie auch von Hand "
                         "hinzufügen.")

    def _ok(self):
        sel = self.tree.selection()
        if sel:
            c = self.found[int(sel[0])]
            self.result = {"name": c["name"] or c["hardware"] or "Kamera", "host": c["host"], "port": c["port"],
                           "brand": guess_brand(c)}
            self.destroy()


class CamerasPage(ttk.Frame):
    """Übersicht aller Kameras mit Vorschaubildern."""
    CW, CH = 330, 230

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Klick = Vollansicht mit Steuerung · Rechtsklick = Bearbeiten / Entfernen",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Fotos & Videos", command=lambda: open_folder(media_dir("video"))).pack(side="right")
        ttk.Button(bar, text="Im Netzwerk suchen", command=app.discover_cameras).pack(side="right", padx=6)
        ttk.Button(bar, text="+ Kamera", style="Accent.TButton", command=lambda: app.camera_dialog()).pack(side="right")
        self.area = tk.Frame(self, bg=BG)
        self.area.pack(fill="both", expand=True)
        self.area.bind("<Configure>", lambda e: self.refresh())
        self.cards = {}

    def refresh(self):
        cams = self.app.camera_list()  # [(key, name, status, img, ptz)]
        keys = [c[0] for c in cams]
        for k in list(self.cards):
            if k not in keys:
                self.cards.pop(k).destroy()
        cols = max(1, self.area.winfo_width() // (self.CW + 12))
        if not cams:
            if "_empty" not in self.cards:
                lbl = tk.Label(self.area, bg=BG, fg=MUTED, font=("Segoe UI", 11), justify="center",
                               text="Noch keine Kameras eingerichtet 📷\n\nKlick auf „Im Netzwerk suchen“ oder „+ Kamera“.")
                lbl.grid(row=0, column=0, padx=20, pady=40)
                self.cards["_empty"] = lbl
            return
        if "_empty" in self.cards:
            self.cards.pop("_empty").destroy()
        for i, (key, name, status, img, ptz) in enumerate(cams):
            c = self.cards.get(key)
            if c is None:
                c = tk.Canvas(self.area, width=self.CW, height=self.CH, bg=BG, highlightthickness=0, cursor="hand2")
                c.bind("<Button-1>", lambda e, k=key: self.app.open_camera(k))
                c.bind("<Button-3>", lambda e, k=key: self._menu(e, k))
                self.cards[key] = c
            c.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="nw")
            c.delete("all")
            rrect(c, 1, 1, self.CW - 1, self.CH - 1, 14, fill=CARD, outline=CARD2)
            if img is not None:
                c.create_image(self.CW / 2, 8 + 90, image=img)
            else:
                c.create_rectangle(10, 10, self.CW - 10, 186, fill="#0d0e11", outline="")
                draw_icon(c, "Kamera", self.CW / 2 - 22, 76, MUTED)
            col = OK if status.startswith("●") else (BAD if status.startswith("⚠") else MUTED)
            c.create_text(14, 204, text=name, anchor="w", fill=FG, font=("Segoe UI Semibold", 11))
            c.create_text(self.CW - 14, 204, text=status + (" · PTZ" if ptz else ""), anchor="e", fill=col,
                          font=("Segoe UI", 9))

    def _menu(self, e, key):
        m = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG, activebackground=ACCENT, bd=0)
        m.add_command(label="Öffnen", command=lambda: self.app.open_camera(key))
        if key.startswith("cam:"):
            m.add_command(label="Bearbeiten …", command=lambda: self.app.camera_dialog(key[4:]))
            m.add_command(label="Entfernen", command=lambda: self.app.remove_camera(key[4:]))
        m.tk_popup(e.x_root, e.y_root)


# =========================================================================== #
# Automationen
# =========================================================================== #
class Form(ttk.Frame):
    """Baut Eingabefelder aus einer Spezifikation: [(key, label, kind, options)].
    kind: text, number, time, choice (options = [(wert, anzeigetext)]), days."""

    def __init__(self, master, spec, values):
        super().__init__(master)
        self.spec, self.vars = spec, {}
        for i, (key, label, kind, opts) in enumerate(spec):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="nw", pady=3, padx=(0, 10))
            val = values.get(key)
            if kind == "choice":
                names = [n for _, n in opts] or ["(nichts verfügbar)"]
                cur = next((n for v, n in opts if v == val), names[0])
                var = tk.StringVar(value=cur)
                ttk.Combobox(self, textvariable=var, values=names, state="readonly", width=34).grid(
                    row=i, column=1, sticky="w", pady=3)
                self.vars[key] = (var, kind, opts)
            elif kind == "days":
                fr = ttk.Frame(self)
                fr.grid(row=i, column=1, sticky="w", pady=3)
                dv = []
                for d, name in enumerate(A.DAYS):
                    b = tk.BooleanVar(value=(not val) or d in val)
                    ttk.Checkbutton(fr, text=name, variable=b).pack(side="left", padx=(0, 4))
                    dv.append(b)
                self.vars[key] = (dv, kind, opts)
            else:
                var = tk.StringVar(value="" if val is None else str(val).replace(".", ",") if kind == "number"
                                   else str(val))
                ttk.Entry(self, textvariable=var, width=36 if kind == "text" else 10).grid(
                    row=i, column=1, sticky="w", pady=3)
                self.vars[key] = (var, kind, opts)

    def values(self):
        out = {}
        for key, (var, kind, opts) in self.vars.items():
            if kind == "choice":
                out[key] = next((v for v, n in opts if n == var.get()), opts[0][0] if opts else None)
            elif kind == "days":
                days = [i for i, b in enumerate(var) if b.get()]
                out[key] = [] if len(days) == 7 else days
            elif kind == "number":
                try:
                    out[key] = float(var.get().replace(",", "."))
                except ValueError:
                    raise ValueError("Bitte eine Zahl eingeben")
            elif kind == "time":
                v = var.get().strip()
                if A._hm(v) is None:
                    raise ValueError("Uhrzeit bitte als HH:MM, z. B. 23:00")
                out[key] = v
            else:
                out[key] = var.get().strip()
        return out


ON_OFF = [("on", "an"), ("off", "aus")]


def trigger_spec(typ, ent):
    if typ == "time":
        return [("at", "Uhrzeit (HH:MM)", "time", None), ("days", "Tage", "days", None)]
    if typ == "switch":
        return [("ain", "Gerät", "choice", ent["switches"]), ("to", "wird", "choice", ON_OFF)]
    if typ == "power":
        return [("ain", "Steckdose", "choice", ent["power"]), ("op", "Leistung", "choice", [("<", "unter"), (">", "über")]),
                ("value", "Watt", "number", None), ("minutes", "für mindestens (Min.)", "number", None)]
    if typ == "temp":
        return [("ain", "Sensor", "choice", ent["temps"]), ("op", "Temperatur", "choice", [("<", "unter"), (">", "über")]),
                ("value", "°C", "number", None), ("minutes", "für mindestens (Min.)", "number", None)]
    if typ == "printer":
        return [("serial", "Drucker", "choice", ent["printers"]),
                ("to", "Status wird", "choice", list(A.PRINTER_STATES.items()))]
    if typ == "ping":
        return [("host", "Ping-Ziel", "choice", ent["pings"]),
                ("to", "wird", "choice", [("down", "nicht erreichbar"), ("up", "wieder erreichbar")])]
    if typ == "internet":
        return [("to", "Internet", "choice", [("down", "fällt aus"), ("up", "ist wieder da")])]
    if typ == "presence":
        return [("person", "Wer / was", "choice", ent["persons"] + [("_any", "Irgendwer (1. kommt / Letzter geht)")]),
                ("to", "", "choice", [("home", "kommt nach Hause / ist da"), ("away", "geht weg / ist weg")])]
    if typ == "sensor":
        return [("ain", "Melder / Taster", "choice", ent.get("sensors") or [("", "– noch kein Melder oder Taster –")]),
                ("to", "wenn er", "choice", A.SENSOR_EVENTS)]
    if typ == "weather":
        return [("to", "Wenn", "choice", [("rain_start", "es anfängt zu regnen"), ("rain_stop", "der Regen aufhört"),
                                          ("rain_soon", "Regen in der nächsten Stunde angesagt ist")])]
    if typ == "motion":
        cams = [(c, n) for c, n in ent["cameras"] if c in ent.get("motion_cams", [])] or ent["cameras"]
        return [("cam", "Kamera", "choice", cams),
                ("what", "Was", "choice", [("any", "jede Bewegung"), ("person", "eine Person"), ("animal", "ein Tier"),
                                          ("cat", "eine Katze"), ("dog", "einen Hund"), ("vehicle", "ein Fahrzeug")]),
                ("cooldown", "danach Pause (Min.)", "number", None)]
    if typ == "calendar":
        return [("match", "Titel enthält (leer = jeder)", "text", None),
                ("when", "Zeitpunkt", "choice", [("start", "Beginn"), ("end", "Ende")]),
                ("offset", "Minuten vorher", "number", None)]
    return []


def action_spec(typ, ent):
    if typ == "switch":
        return [("ain", "Gerät", "choice", ent["switches"]),
                ("mode", "Schalten", "choice", ON_OFF + [("toggle", "umschalten")])]
    if typ == "thermostat":
        return [("ain", "Heizkörperregler", "choice", ent["thermostats"]), ("value", "Solltemperatur °C", "number", None)]
    if typ == "level":
        return [("ain", "Lampe", "choice", ent.get("lamps") or [("", "– keine dimmbare Lampe –")]),
                ("value", "Helligkeit % (0 = aus)", "number", None)]
    if typ == "media":
        from prov_media import KEY_CHOICES
        return [("ain", "Gerät", "choice", ent.get("media") or [("", "– erst Sky Q / Fernseher einbinden –")]),
                ("key", "Taste", "choice", KEY_CHOICES),
                ("value", "Sendernummer (nur bei „Sender wählen“)", "number", None)]
    if typ == "wlan":
        return [("index", "WLAN", "choice", ent["wlans"]), ("mode", "Schalten", "choice", ON_OFF)]
    if typ == "notify":
        return [("text", "Text", "text", None),
                ("cam", "Kamerabild anhängen", "choice", [("", "– ohne Bild –")] + ent["cameras"])]
    if typ == "printer_light":
        return [("serial", "Drucker", "choice", ent["printers"]), ("mode", "Licht", "choice", ON_OFF)]
    if typ == "camera_preset":
        return [("cam", "Kamera", "choice", ent["cameras"]), ("preset", "Positionsname", "text", None)]
    if typ == "scene":
        return [("id", "Szene", "choice", ent["scenes"])]
    if typ == "delay":
        return [("minutes", "Minuten", "number", None)]
    if typ == "record":
        return [("cam", "Kamera", "choice", ent["cameras"]), ("seconds", "Länge (Sekunden)", "number", None)]
    if typ == "wol":
        return [("mac", "Gerät", "choice", ent.get("wol") or [("", "– erst unter Geräte → ⏻ Aufwecken anlegen –")])]
    return []


TRIGGER_DEFAULTS = {"sensor": {"to": "open"}, "time": {"at": "22:00"}, "power": {"op": "<", "value": 5, "minutes": 10},
                    "temp": {"op": "<", "value": 18, "minutes": 0}, "printer": {"to": "FINISH"},
                    "ping": {"to": "down"}, "internet": {"to": "down"}, "switch": {"to": "on"},
                    "calendar": {"match": "Urlaub", "when": "start", "offset": 0},
                    "presence": {"person": "_any", "to": "home"}, "motion": {"cooldown": 5, "what": "any"},
                    "weather": {"to": "rain_start"}}
ACTION_DEFAULTS = {"switch": {"mode": "off"}, "level": {"value": 50}, "media": {"key": "pause"}, "thermostat": {"value": 20}, "wlan": {"mode": "off"},
                   "notify": {"text": "Hallo von deinem Zuhause 👋"}, "printer_light": {"mode": "off"},
                   "delay": {"minutes": 5}, "record": {"seconds": 30}}


def describe_trigger(t, names):
    typ = t.get("type")
    n = lambda k: names.get(t.get(k), t.get(k) or "?")  # noqa: E731
    if typ == "manual":
        return "Manuell (Szene)"
    if typ == "time":
        days = t.get("days") or []
        return f"um {t.get('at')} Uhr" + (" (" + ", ".join(A.DAYS[d] for d in days) + ")" if days else " (täglich)")
    if typ == "switch":
        return f"{n('ain')} wird {'an' if t.get('to') == 'on' else 'aus'}"
    if typ in ("power", "temp"):
        unit = "W" if typ == "power" else "°C"
        return (f"{n('ain')} {'unter' if t.get('op') == '<' else 'über'} {t.get('value')} {unit}"
                + (f" für {t.get('minutes'):g} Min." if t.get("minutes") else ""))
    if typ == "printer":
        return f"{n('serial')}: {A.PRINTER_STATES.get(t.get('to'), t.get('to'))}"
    if typ == "ping":
        return f"{n('host')} {'weg' if t.get('to') == 'down' else 'wieder da'}"
    if typ == "internet":
        return "Internet " + ("fällt aus" if t.get("to") == "down" else "wieder da")
    if typ == "presence":
        who = "Irgendwer" if t.get("person", "_any") == "_any" else n("person")
        if t.get("person", "_any") == "_any":
            return "Erste Person kommt nach Hause" if t.get("to") == "home" else "Letzte Person geht weg"
        return f"{who} {'kommt nach Hause' if t.get('to') == 'home' else 'geht weg'}"
    if typ == "sensor":
        return f"{n('ain')} {dict(A.SENSOR_EVENTS).get(t.get('to'), t.get('to'))}"
    if typ == "weather":
        return {"rain_start": "Es fängt an zu regnen", "rain_stop": "Regen hört auf",
                "rain_soon": "Regen in der nächsten Stunde"}.get(t.get("to"), "Wetter")
    if typ == "motion":
        cd = float(t.get("cooldown", 5) or 0)
        what = {"person": "eine Person", "animal": "ein Tier", "cat": "eine Katze", "dog": "einen Hund",
                "vehicle": "ein Fahrzeug"}.get(t.get("what") or "any")
        return (f"{n('cam')} sieht {what}" if what else f"Bewegung an {n('cam')}") + (
            f" (danach {cd:g} Min. Pause)" if cd else "")
    if typ == "calendar":
        off = float(t.get("offset", 0) or 0)
        what = f"Termin „{t['match']}“" if t.get("match") else "jeder Termin"
        return f"{what}: {'Beginn' if t.get('when', 'start') == 'start' else 'Ende'}" + (f", {off:g} Min. vorher" if off else "")
    return typ or "?"


def describe_action(a, names):
    typ = a.get("type")
    n = lambda k: names.get(a.get(k), a.get(k) or "?")  # noqa: E731
    mode = {"on": "an", "off": "aus", "toggle": "umschalten"}.get(a.get("mode"), "")
    return {
        "switch": lambda: f"{n('ain')} {mode}",
        "level": lambda: f"{n('ain')} auf {int(float(a.get('value', 50)))} %",
        "media": lambda: f"{n('ain')}: " + (f"Sender {int(float(a.get('value') or 0))}" if a.get("key") == "channel"
                                            else _key_label(a.get("key"))),
        "thermostat": lambda: f"{n('ain')} auf {a.get('value')} °C",
        "wlan": lambda: f"WLAN {n('index')} {mode}",
        "notify": lambda: f"Hinweis: „{a.get('text', '')}“" + (f" + Bild {n('cam')}" if a.get("cam") else ""),
        "printer_light": lambda: f"Licht {n('serial')} {mode}",
        "camera_preset": lambda: f"{n('cam')} → „{a.get('preset', '')}“",
        "scene": lambda: f"Szene „{n('id')}“",
        "delay": lambda: f"{a.get('minutes', 0):g} Min. warten",
        "record": lambda: f"{n('cam')} {float(a.get('seconds', 30) or 30):g} s aufnehmen",
        "wol": lambda: f"{n('mac')} aufwecken",
    }.get(typ, lambda: typ or "?")()


def _key_label(k):
    from prov_media import KEYS
    return KEYS.get(k, k or "?")


def device_choices(ent):
    """Für „nur wenn …“: Gerät an/aus (Steckdosen, Lampen, Sky, Fernseher …)."""
    out = [("", "– egal –")]
    for ain, nm in ent.get("switches", []):
        out += [(f"{ain}|on", f"{nm} ist an"), (f"{ain}|off", f"{nm} ist aus")]
    return out


def presence_choices(ent):
    out = [("", "– egal –"), ("_any:away", "niemand zu Hause"), ("_any:home", "jemand zu Hause")]
    for pid, nm in ent.get("persons", []):
        item = pid in ent.get("items", [])
        out += [(f"{pid}:home", f"{nm} {'ist da' if item else 'zu Hause'}"),
                (f"{pid}:away", f"{nm} {'ist weg' if item else 'unterwegs'}")]
    return out


def describe_condition(c, names):
    if not c:
        return ""
    parts = []
    pc = c.get("presence")
    if pc and pc.get("who"):
        if pc["who"] == "_any":
            parts.append("niemand zu Hause" if pc.get("state") == "away" else "jemand zu Hause")
        else:
            parts.append(f"{names.get(pc['who'], '?')} {'da' if pc.get('state') == 'home' else 'weg'}")
    dc = c.get("device")
    if dc and dc.get("ain"):
        parts.append(f"{names.get(dc['ain'], '?')} {'an' if dc.get('state') == 'on' else 'aus'}")
    if c.get("from") and c.get("to"):
        parts.append(f"{c['from']}–{c['to']} Uhr")
    if c.get("days"):
        parts.append(", ".join(A.DAYS[d] for d in c["days"]))
    return "nur wenn " + " · ".join(parts) if parts else ""


class ActionDialog(tk.Toplevel):
    def __init__(self, master, ent, act=None):
        super().__init__(master)
        self.title("Aktion")
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(False, False)
        self.ent, self.result = ent, None
        act = act or {"type": "switch"}
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Was soll passieren?").grid(row=0, column=0, sticky="w", padx=(0, 10))
        names = list(A.ACTION_TYPES.values())
        self.v_type = tk.StringVar(value=A.ACTION_TYPES.get(act["type"], names[0]))
        cb = ttk.Combobox(f, textvariable=self.v_type, values=names, state="readonly", width=34)
        cb.grid(row=0, column=1, sticky="w", pady=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._build({}))
        self.holder = ttk.Frame(f)
        self.holder.grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        b = ttk.Frame(f)
        b.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")
        self.form = None
        self._build(act)
        self.grab_set()
        _center(self, master)

    def _typ(self):
        return next(k for k, v in A.ACTION_TYPES.items() if v == self.v_type.get())

    def _build(self, values):
        typ = self._typ()
        if self.form:
            self.form.destroy()
        vals = dict(ACTION_DEFAULTS.get(typ, {}))
        vals.update({k: v for k, v in values.items() if k != "type"})
        self.form = Form(self.holder, action_spec(typ, self.ent), vals)
        self.form.pack()

    def _ok(self):
        try:
            vals = self.form.values()
        except ValueError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        vals["type"] = self._typ()
        if vals["type"] == "wlan" and vals.get("index") is not None:
            vals["index"] = int(vals["index"])
        self.result = vals
        self.destroy()


class AutomationDialog(tk.Toplevel):
    def __init__(self, master, ent, names, rule):
        super().__init__(master)
        self.title("Automation bearbeiten")
        self.configure(bg=BG)
        self.transient(master)
        self.geometry("640x660")
        self.ent, self.names, self.result = ent, names, None
        self.rule = rule
        self.actions = [dict(a) for a in rule.get("actions", [])]
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Name").pack(side="left")
        self.v_name = tk.StringVar(value=rule.get("name", ""))
        ttk.Entry(top, textvariable=self.v_name, width=34).pack(side="left", padx=8)
        self.v_en = tk.BooleanVar(value=rule.get("enabled", True))
        ttk.Checkbutton(top, text="aktiv", variable=self.v_en).pack(side="left")

        ttk.Label(f, text="WENN …", style="H1.TLabel").pack(anchor="w", pady=(14, 2))
        tf = ttk.Frame(f)
        tf.pack(fill="x")
        tnames = list(A.TRIGGER_TYPES.values())
        t = rule.get("trigger", {"type": "manual"})
        self.v_trig = tk.StringVar(value=A.TRIGGER_TYPES.get(t.get("type"), tnames[0]))
        cb = ttk.Combobox(tf, textvariable=self.v_trig, values=tnames, state="readonly", width=36)
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self._build_trigger({}))
        self.t_holder = ttk.Frame(f)
        self.t_holder.pack(fill="x", pady=6)
        self.t_form = None
        self._build_trigger(t)

        ttk.Label(f, text="NUR WENN … (optional)", style="H1.TLabel").pack(anchor="w", pady=(10, 2))
        cf = ttk.Frame(f)
        cf.pack(fill="x")
        c = rule.get("condition") or {}
        self.v_cond = tk.BooleanVar(value=bool(c.get("from") and c.get("to")))
        ttk.Checkbutton(cf, text="nur zwischen", variable=self.v_cond).pack(side="left")
        self.v_from = tk.StringVar(value=c.get("from", "22:00"))
        self.v_to = tk.StringVar(value=c.get("to", "06:00"))
        ttk.Entry(cf, textvariable=self.v_from, width=6).pack(side="left", padx=4)
        ttk.Label(cf, text="und").pack(side="left")
        ttk.Entry(cf, textvariable=self.v_to, width=6).pack(side="left", padx=4)
        ttk.Label(cf, text="Uhr", style="Muted.TLabel").pack(side="left")
        pf = ttk.Frame(f)
        pf.pack(fill="x", pady=(4, 0))
        ttk.Label(pf, text="Anwesenheit:").pack(side="left", padx=(0, 6))
        self.pchoices = presence_choices(self.ent)
        pc = c.get("presence") or {}
        cur_key = f"{pc.get('who')}:{pc.get('state')}" if pc.get("who") else ""
        self.v_pres = tk.StringVar(value=dict(self.pchoices).get(cur_key, "– egal –"))
        ttk.Combobox(pf, textvariable=self.v_pres, values=[n for _, n in self.pchoices], state="readonly",
                     width=30).pack(side="left")
        gf = ttk.Frame(f)
        gf.pack(fill="x", pady=(4, 0))
        ttk.Label(gf, text="Gerät:").pack(side="left", padx=(0, 6))
        self.dchoices = device_choices(self.ent)
        dc = c.get("device") or {}
        cur_d = f"{dc.get('ain')}|{dc.get('state')}" if dc.get("ain") else ""
        self.v_devc = tk.StringVar(value=dict(self.dchoices).get(cur_d, "– egal –"))
        ttk.Combobox(gf, textvariable=self.v_devc, values=[n for _, n in self.dchoices], state="readonly",
                     width=30).pack(side="left")
        df = ttk.Frame(f)
        df.pack(fill="x", pady=(4, 0))
        ttk.Label(df, text="an diesen Tagen:").pack(side="left", padx=(0, 6))
        self.v_days = []
        for d, nm in enumerate(A.DAYS):
            b = tk.BooleanVar(value=not c.get("days") or d in c.get("days", []))
            ttk.Checkbutton(df, text=nm, variable=b).pack(side="left", padx=(0, 3))
            self.v_days.append(b)

        ttk.Label(f, text="DANN …", style="H1.TLabel").pack(anchor="w", pady=(14, 2))
        af = ttk.Frame(f)
        af.pack(fill="both", expand=True)
        self.lb = tk.Listbox(af, height=7, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda e: self._edit_action())
        ab = ttk.Frame(af, padding=(8, 0, 0, 0))
        ab.pack(side="right", fill="y")
        for txt, cmd in (("+ Aktion", self._add_action), ("Bearbeiten", self._edit_action),
                         ("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)), ("Entfernen", self._del_action)):
            ttk.Button(ab, text=txt, command=cmd).pack(fill="x", pady=2)
        self._render_actions()

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.grab_set()
        _center(self, master)

    def _trig_type(self):
        return next(k for k, v in A.TRIGGER_TYPES.items() if v == self.v_trig.get())

    def _build_trigger(self, values):
        typ = self._trig_type()
        if self.t_form:
            self.t_form.destroy()
        vals = dict(TRIGGER_DEFAULTS.get(typ, {}))
        vals.update({k: v for k, v in values.items() if k != "type"})
        spec = trigger_spec(typ, self.ent)
        if typ == "manual":
            self.t_form = ttk.Label(self.t_holder, text="Erscheint als Szenen-Kachel auf „Zuhause“ – ein Klick führt "
                                                        "die Aktionen aus.", style="Muted.TLabel")
        else:
            self.t_form = Form(self.t_holder, spec, vals)
        self.t_form.pack(anchor="w")

    def _render_actions(self):
        self.lb.delete(0, "end")
        for i, a in enumerate(self.actions, 1):
            self.lb.insert("end", f"{i}. {describe_action(a, self.names)}")
        if not self.actions:
            self.lb.insert("end", "Noch keine Aktion – klick auf „+ Aktion“")

    def _sel(self):
        s = self.lb.curselection()
        return s[0] if s and s[0] < len(self.actions) else None

    def _add_action(self):
        d = ActionDialog(self, self.ent)
        self.wait_window(d)
        if d.result:
            self.actions.append(d.result)
            self._render_actions()

    def _edit_action(self):
        i = self._sel()
        if i is None:
            return
        d = ActionDialog(self, self.ent, self.actions[i])
        self.wait_window(d)
        if d.result:
            self.actions[i] = d.result
            self._render_actions()

    def _move(self, delta):
        i = self._sel()
        if i is None or not 0 <= i + delta < len(self.actions):
            return
        self.actions[i], self.actions[i + delta] = self.actions[i + delta], self.actions[i]
        self._render_actions()
        self.lb.selection_set(i + delta)

    def _del_action(self):
        i = self._sel()
        if i is not None:
            del self.actions[i]
            self._render_actions()

    def _ok(self):
        typ = self._trig_type()
        try:
            trig = self.t_form.values() if isinstance(self.t_form, Form) else {}
        except ValueError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        trig["type"] = typ
        cond = {}
        if self.v_cond.get():
            if A._hm(self.v_from.get()) is None or A._hm(self.v_to.get()) is None:
                messagebox.showwarning(APP, "Zeitfenster bitte als HH:MM angeben.", parent=self)
                return
            cond = {"from": self.v_from.get().strip(), "to": self.v_to.get().strip()}
        days = [i for i, b in enumerate(self.v_days) if b.get()]
        if len(days) < 7:
            cond["days"] = days
        pkey = next((k for k, n in self.pchoices if n == self.v_pres.get()), "")
        if pkey:
            who, _, state = pkey.rpartition(":")
            cond["presence"] = {"who": who, "state": state}
        dkey = next((k for k, n in self.dchoices if n == self.v_devc.get()), "")
        if dkey:
            ain, _, state = dkey.rpartition("|")
            cond["device"] = {"ain": ain, "state": state}
        if not self.actions:
            messagebox.showwarning(APP, "Bitte mindestens eine Aktion hinzufügen.", parent=self)
            return
        r = dict(self.rule)
        r.update(name=self.v_name.get().strip() or "Automation", enabled=self.v_en.get(), trigger=trig,
                 condition=cond, actions=self.actions)
        self.result = r
        self.destroy()


class AutomationsPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="WENN etwas passiert → DANN tu etwas. Automationen mit „Manuell“ erscheinen als Szene.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Vorlagen …", command=self._templates).pack(side="right")
        ttk.Button(bar, text="+ Automation", style="Accent.TButton", command=self.new).pack(side="right", padx=6)
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("st", "name", "trig", "act"), show="headings", selectmode="browse")
        for c, t, w, a in (("st", "", 40, "center"), ("name", "Name", 220, "w"), ("trig", "Wenn", 280, "w"),
                           ("act", "Dann", 420, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=True)
        self.tree.tag_configure("off", foreground=MUTED)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(self)
        b.pack(fill="x", pady=(8, 0))
        for txt, cmd in (("Bearbeiten", self.edit), ("Jetzt ausführen", self.run), ("Aktiv an/aus", self.toggle),
                         ("Duplizieren", self.dup), ("Löschen", self.delete)):
            ttk.Button(b, text=txt, command=cmd).pack(side="left", padx=(0, 6))

    def refresh(self):
        names = self.app.entity_names()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for r in self.app.cfg.get("automations", []):
            acts = " → ".join(describe_action(a, names) for a in r.get("actions", []))
            self.tree.insert("", "end", iid=r["id"], tags=() if r.get("enabled", True) else ("off",),
                             values=("●" if r.get("enabled", True) else "○", r["name"],
                                     describe_trigger(r.get("trigger", {}), names) +
                                     ("  ·  " + describe_condition(r.get("condition"), names)
                                      if describe_condition(r.get("condition"), names) else ""), acts))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def _rule(self):
        s = self.tree.selection()
        return next((r for r in self.app.cfg.get("automations", []) if s and r["id"] == s[0]), None)

    def _edit_rule(self, rule, is_new=False):
        d = AutomationDialog(self, self.app.entities(), self.app.entity_names(), rule)
        self.wait_window(d)
        if d.result:
            lst = self.app.cfg.setdefault("automations", [])
            if is_new:
                lst.append(d.result)
            else:
                lst[:] = [d.result if r["id"] == d.result["id"] else r for r in lst]
            self.app.automations_changed()

    def new(self):
        self._edit_rule(A.new_automation(), True)

    def edit(self):
        r = self._rule()
        if r:
            self._edit_rule(r)

    def run(self):
        r = self._rule()
        if r:
            self.app.engine.trigger(r, reason="von Hand")

    def toggle(self):
        r = self._rule()
        if r:
            r["enabled"] = not r.get("enabled", True)
            self.app.automations_changed()

    def dup(self):
        r = self._rule()
        if r:
            n = A.new_automation(r["name"] + " (Kopie)")
            n.update({k: v for k, v in r.items() if k not in ("id", "name")})
            self.app.cfg["automations"].append(n)
            self.app.automations_changed()

    def delete(self):
        r = self._rule()
        if r and messagebox.askyesno(APP, f"„{r['name']}“ löschen?"):
            self.app.cfg["automations"] = [x for x in self.app.cfg["automations"] if x["id"] != r["id"]]
            self.app.automations_changed()

    def _templates(self):
        ent = self.app.entities()
        m = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG, activebackground=ACCENT, bd=0)
        plug = ent["switches"][0][0] if ent["switches"] else None
        pr = ent["printers"][0][0] if ent["printers"] else None
        pc = self.app.printer_plug(pr) if pr else None
        tpl = [
            ("Drucker nach Druck ausschalten (10 Min. abkühlen)",
             {"type": "printer", "serial": pr, "to": "FINISH"},
             [{"type": "delay", "minutes": 10}, {"type": "switch", "ain": pc or plug, "mode": "off"},
              {"type": "notify", "text": "Drucker wurde ausgeschaltet 🔌"}]),
            ("Standby-Killer (unter 3 W für 30 Min. → aus)",
             {"type": "power", "ain": plug, "op": "<", "value": 3, "minutes": 30},
             [{"type": "switch", "ain": plug, "mode": "off"}]),
            ("Gäste-WLAN nachts aus (23:00)", {"type": "time", "at": "23:00"},
             [{"type": "wlan", "index": ent["wlans"][-1][0] if ent["wlans"] else 3, "mode": "off"}]),
            ("Szene: Feierabend (alles aus)", {"type": "manual"},
             [{"type": "switch", "ain": a, "mode": "off"} for a, _ in ent["switches"][:4]]
             or [{"type": "notify", "text": "Schönen Feierabend 🍻"}]),
            ("Hinweis, wenn Internet ausfällt", {"type": "internet", "to": "down"},
             [{"type": "notify", "text": "Internet ist weg ⚠️"}]),
        ]
        cam = (ent.get("motion_cams") or [c for c, _ in ent["cameras"]] or [None])[0]
        if cam:
            tpl.append(("Bewegung, während niemand zu Hause ist → Push mit Bild + Video",
                        {"type": "motion", "cam": cam, "cooldown": 5},
                        [{"type": "notify", "text": "Bewegung erkannt 👀", "cam": cam},
                         {"type": "record", "cam": cam, "seconds": 30}],
                        {"presence": {"who": "_any", "state": "away"}}))
        for name, trig, acts, *cond in tpl:
            def add(name=name, trig=trig, acts=acts, cond=cond):
                r = A.new_automation(name.split(" (")[0].split(" →")[0])
                r["trigger"], r["actions"] = dict(trig), [dict(a) for a in acts]
                if cond:
                    r["condition"] = dict(cond[0])
                self._edit_rule(r, True)
            m.add_command(label=name, command=add)
        m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())


# =========================================================================== #
# Logbuch
# =========================================================================== #
class LogbookPage(ttk.Frame):
    KINDS = ["Alle", "Gerät", "Anwesenheit", "Drucker", "Netzwerk", "Automation", "Kamera", "App"]

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Bereich:").pack(side="left")
        self.v_kind = tk.StringVar(value="Alle")
        cb = ttk.Combobox(bar, textvariable=self.v_kind, values=self.KINDS, state="readonly", width=14)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text="Suchen:").pack(side="left", padx=(12, 0))
        self.v_search = tk.StringVar()
        self.v_search.trace_add("write", lambda *a: self.refresh())
        ttk.Entry(bar, textvariable=self.v_search, width=24).pack(side="left", padx=6)
        ttk.Button(bar, text="🐞 Problem melden", command=lambda: app.bug_dialog("allgemein")).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Fehlerprotokoll speichern …", command=app.save_support_package).pack(side="right")
        ttk.Button(bar, text="Fehlerprotokoll öffnen", command=app.open_log).pack(side="right", padx=6)
        ttk.Label(bar, text="Die letzten 90 Tage werden gespeichert", style="Muted.TLabel").pack(side="right", padx=8)
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("ts", "kind", "name", "text"), show="headings")
        for c, t, w in (("ts", "Zeit", 140), ("kind", "Bereich", 100), ("name", "Name", 200), ("text", "Ereignis", 560)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=True)
        for k, col in (("Drucker", "#f0883e"), ("Automation", ACCENT), ("Netzwerk", WARN), ("Kamera", "#b392f0"),
                       ("Anwesenheit", OK)):
            self.tree.tag_configure(k, foreground=col)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

    def refresh(self):
        kind = self.v_kind.get()
        rows = self.app.store.events(None if kind == "Alle" else kind, 800, self.v_search.get().strip())
        self.tree.delete(*self.tree.get_children())
        today = time.strftime("%d.%m.%Y")
        for i, (ts, k, name, text) in enumerate(rows):
            d = time.strftime("%d.%m.%Y", time.localtime(ts))
            when = time.strftime("Heute %H:%M:%S" if d == today else "%d.%m. %H:%M", time.localtime(ts))
            self.tree.insert("", "end", iid=str(i), values=(when, k, name, text), tags=(k,))


# =========================================================================== #
# Kalender
# =========================================================================== #
CAL_COLORS = [("#4285F4", "Blau"), ("#34C759", "Grün"), ("#FF9500", "Orange"), ("#FF3B30", "Rot"),
              ("#AF52DE", "Lila"), ("#FFCC00", "Gelb"), ("#5AC8FA", "Hellblau"), ("#8E8E93", "Grau")]
CAL_TYPES = [("icloud", "Apple iCloud"), ("google", "Google Kalender"), ("ics", "Anderer iCal-Link")]
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]


def day_label(d, today=None):
    import datetime as _dt
    today = today or _dt.date.today()
    if d == today:
        return "Heute"
    if d == today + _dt.timedelta(days=1):
        return "Morgen"
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]}"


def event_time(e):
    if e["allday"]:
        days = (e["end"] - e["start"]).days
        return "ganztägig" if days <= 1 else f"ganztägig ({days} Tage)"
    if e["end"] > e["start"]:
        return f"{e['start']:%H:%M}–{e['end']:%H:%M}"
    return f"{e['start']:%H:%M}"


class SourceDialog(tk.Toplevel):
    def __init__(self, master, cur=None, password=""):
        super().__init__(master)
        cur = cur or {}
        self.title("Kalender verbinden")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result, self.cur = None, cur
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Kalender verbinden", style="H1.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        typ = cur.get("type", "icloud")
        self.v_type = tk.StringVar(value=dict(CAL_TYPES)[typ])
        self.v_name = tk.StringVar(value=cur.get("name", ""))
        self.v_color = tk.StringVar(value=dict(CAL_COLORS).get(cur.get("color"), "Blau"))
        self.v_url = tk.StringVar(value=cur.get("url", ""))
        self.v_user = tk.StringVar(value=cur.get("user", ""))
        self.v_pw = tk.StringVar(value=password)
        ttk.Label(f, text="Art").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 12))
        cb = ttk.Combobox(f, textvariable=self.v_type, values=[n for _, n in CAL_TYPES], state="readonly", width=34)
        cb.grid(row=1, column=1, sticky="w", pady=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._show())
        ttk.Label(f, text="Name").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_name, width=36).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Farbe").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_color, values=[n for _, n in CAL_COLORS], state="readonly",
                     width=16).grid(row=3, column=1, sticky="w", pady=4)
        self.box = ttk.Frame(f)
        self.box.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.l_test = ttk.Label(f, text="", style="Muted.TLabel", wraplength=480, justify="left")
        self.l_test.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(b, text="Verbindung testen", command=self._test).pack(side="right", padx=6)
        self._show()
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        _center(self, master)

    def _type(self):
        return next(k for k, n in CAL_TYPES if n == self.v_type.get())

    def _show(self):
        for w in self.box.winfo_children():
            w.destroy()
        t = self._type()
        if t == "icloud":
            rows = [("Apple-ID (E-Mail)", self.v_user, ""), ("App-spezifisches Passwort", self.v_pw, "•")]
            hint = ("So bekommst du das Passwort: account.apple.com → Anmelden und Sicherheit →\n"
                    "App-spezifische Passwörter → „+“ → Name z. B. „Hauskern“. Das Passwort sieht aus wie\n"
                    "abcd-efgh-ijkl-mnop. Dein normales Apple-Passwort funktioniert hier nicht.\n"
                    "Alle deine iCloud-Kalender werden automatisch übernommen.")
            if not self.v_name.get():
                self.v_name.set("iCloud")
        else:
            rows = [("iCal-Adresse", self.v_url, "")]
            hint = ("Google: calendar.google.com → ⚙ Einstellungen → links deinen Kalender anklicken →\n"
                    "„Kalender integrieren“ → „Privatadresse im iCal-Format“ kopieren und hier einfügen.\n"
                    "Die Adresse endet auf …/basic.ics. Gib sie niemandem weiter.\n"
                    "Für jeden weiteren Google-Kalender (z. B. Familie) einfach noch einen hinzufügen."
                    if t == "google" else
                    "Jede iCal-/webcal-Adresse geht, z. B. Feiertage, Müllabfuhr, Outlook-Freigaben,\n"
                    "Schulferien oder ein freigegebener iCloud-Kalender.")
            if not self.v_name.get():
                self.v_name.set("Google" if t == "google" else "Kalender")
        for i, (lbl, var, show) in enumerate(rows):
            ttk.Label(self.box, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Entry(self.box, textvariable=var, width=46 if lbl == "iCal-Adresse" else 32, show=show).grid(
                row=i, column=1, sticky="w", pady=4)
        ttk.Label(self.box, text=hint, style="Muted.TLabel", justify="left").grid(row=len(rows), column=0,
                                                                                  columnspan=2, sticky="w", pady=(6, 0))

    def _src(self):
        t = self._type()
        src = {"id": self.cur.get("id") or f"k{int(time.time() * 1000) % 10**9}", "type": "ics" if t != "icloud" else "icloud",
               "kind": t, "name": self.v_name.get().strip() or dict(CAL_TYPES)[t],
               "color": next(c for c, n in CAL_COLORS if n == self.v_color.get())}
        if t == "icloud":
            src["user"] = self.v_user.get().strip()
        else:
            src["url"] = self.v_url.get().strip()
        return src

    def _test(self):
        import calendar_sync as CS
        import datetime as _dt
        src, pw = self._src(), self.v_pw.get().strip()
        self.l_test.configure(text="Teste …", foreground=MUTED)

        def run():
            try:
                now = _dt.datetime.now()
                ev = CS.load_source(src, pw, now, now + _dt.timedelta(days=30))
                nxt = next((e for e in ev if e["start"] >= now), None)
                msg = f"✓ Verbunden – {len(ev)} Termine in den nächsten 30 Tagen" + (
                    f"\nNächster: {day_label(nxt['start'].date())} – {nxt['title']}" if nxt else "")
                col = OK
            except CS.CalendarError as e:
                msg, col = f"⚠ {e}", BAD
            except Exception as e:
                msg, col = f"⚠ {type(e).__name__}: {e}", BAD
            ui_later(self, lambda: self.l_test.configure(text=msg, foreground=col) if self.winfo_exists() else None)
        threading.Thread(target=run, daemon=True).start()

    def _ok(self):
        src = self._src()
        if src["type"] == "icloud" and not (src["user"] and self.v_pw.get().strip()):
            messagebox.showwarning(APP, "Bitte Apple-ID und app-spezifisches Passwort eintragen.", parent=self)
            return
        if src["type"] == "ics" and not src.get("url", "").lower().startswith(("http", "webcal")):
            messagebox.showwarning(APP, "Bitte die iCal-Adresse (beginnt mit https:// oder webcal://) einfügen.",
                                   parent=self)
            return
        src["password"] = self.v_pw.get().strip()
        self.result = src
        self.destroy()


class CalendarPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        import datetime as _dt
        self.app = app
        self.month = _dt.date.today().replace(day=1)
        self.sel_day = _dt.date.today()
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="‹", width=3, command=lambda: self._shift(-1)).pack(side="left")
        ttk.Button(bar, text="Heute", command=self._today).pack(side="left", padx=4)
        ttk.Button(bar, text="›", width=3, command=lambda: self._shift(1)).pack(side="left")
        self.l_month = ttk.Label(bar, text="", style="H1.TLabel")
        self.l_month.pack(side="left", padx=12)
        ttk.Button(bar, text="Kalender verwalten …", command=self.manage).pack(side="right")
        ttk.Button(bar, text="Aktualisieren", command=lambda: app.refresh_calendars(force=True)).pack(side="right", padx=6)
        self.l_status = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_status.pack(side="right", padx=8)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.grid_c = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.grid_c.pack(side="left", fill="both", expand=True)
        self.grid_c.bind("<Configure>", lambda e: self.draw_month())
        self.grid_c.bind("<Button-1>", self._click)
        right = ttk.Frame(body, padding=(12, 0, 0, 0), width=380)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self.l_day = ttk.Label(right, text="", style="H1.TLabel")
        self.l_day.pack(anchor="w")
        self.tree = ttk.Treeview(right, columns=("time", "title"), show="headings", selectmode="none")
        self.tree.heading("time", text="Zeit")
        self.tree.heading("title", text="Termin")
        self.tree.column("time", width=120, anchor="w")
        self.tree.column("title", width=240, anchor="w")
        self.tree.tag_configure("head", foreground=FG, font=("Segoe UI Semibold", 10))
        self.tree.tag_configure("muted", foreground=MUTED)
        self.tree.pack(fill="both", expand=True, pady=(6, 0))
        self._cells = []

    def _shift(self, n):
        m = self.month.month - 1 + n
        self.month = self.month.replace(year=self.month.year + m // 12, month=m % 12 + 1)
        self.refresh()

    def _today(self):
        import datetime as _dt
        self.month = _dt.date.today().replace(day=1)
        self.sel_day = _dt.date.today()
        self.refresh()

    def _events_on(self, d):
        import datetime as _dt
        s = _dt.datetime.combine(d, _dt.time())
        e = s + _dt.timedelta(days=1)
        return [ev for ev in self.app.cal_events if ev["start"] < e and (ev["end"] > s or ev["start"] >= s)]

    def refresh(self):
        self.l_month.configure(text=f"{MONTHS[self.month.month - 1]} {self.month.year}")
        st = self.app.cal_status_text()
        self.l_status.configure(text=st, foreground=BAD if st.startswith("⚠") else MUTED)
        self.draw_month()
        self.show_day(self.sel_day)

    def draw_month(self):
        import datetime as _dt
        c = self.grid_c
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 100 or h < 100:
            return
        head = 26
        cw, ch = w / 7, (h - head) / 6
        for i, n in enumerate(["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]):
            c.create_text(i * cw + 10, 12, text=n, anchor="w", fill=MUTED, font=("Segoe UI Semibold", 9))
        first = self.month - _dt.timedelta(days=self.month.weekday())
        today = _dt.date.today()
        self._cells = []
        for i in range(42):
            d = first + _dt.timedelta(days=i)
            x, y = (i % 7) * cw, head + (i // 7) * ch
            inmonth = d.month == self.month.month
            sel = d == self.sel_day
            rrect(c, x + 2, y + 2, x + cw - 2, y + ch - 2, 8, fill="#1b2c47" if sel else (CARD if inmonth else BG),
                  outline=ACCENT if d == today else CARD2)
            c.create_text(x + 10, y + 13, text=str(d.day), anchor="w",
                          fill=(ACCENT if d == today else FG) if inmonth else "#3b404b", font=("Segoe UI Semibold", 10))
            evs = self._events_on(d)
            lines = int((ch - 30) // 17)
            for j, ev in enumerate(evs[:max(0, lines)]):
                if j == lines - 1 and len(evs) > lines:
                    c.create_text(x + 8, y + 32 + j * 17, text=f"+{len(evs) - j} weitere", anchor="w", fill=MUTED,
                                  font=("Segoe UI", 8))
                    break
                col = ev["color"] or ACCENT
                c.create_rectangle(x + 6, y + 25 + j * 17, x + 9, y + 38 + j * 17, fill=col, outline="")
                label = ev["title"] if ev["allday"] else f"{ev['start']:%H:%M} {ev['title']}"
                maxch = int((cw - 18) / 6.2)
                c.create_text(x + 13, y + 32 + j * 17, text=label[:maxch] + ("…" if len(label) > maxch else ""),
                              anchor="w", fill=FG if inmonth else MUTED, font=("Segoe UI", 8))
            self._cells.append((x, y, x + cw, y + ch, d))

    def _click(self, e):
        for x1, y1, x2, y2, d in self._cells:
            if x1 <= e.x < x2 and y1 <= e.y < y2:
                self.sel_day = d
                if d.month != self.month.month:
                    self.month = d.replace(day=1)
                self.refresh()
                return

    def show_day(self, d):
        import datetime as _dt
        self.l_day.configure(text=day_label(d))
        self.tree.delete(*self.tree.get_children())
        n = 0
        for k in range(14):  # gewählter Tag + die folgenden zwei Wochen als Agenda
            day = d + _dt.timedelta(days=k)
            evs = self._events_on(day)
            if not evs and k:
                continue
            self.tree.insert("", "end", iid=f"h{k}", values=("", day_label(day)), tags=("head",))
            if not evs:
                self.tree.insert("", "end", iid=f"e{k}-0", values=("", "keine Termine 🎉"), tags=("muted",))
            for j, ev in enumerate(evs):
                col = ev["color"] or ACCENT
                tag = "c" + col.lstrip("#")
                self.tree.tag_configure(tag, foreground=col)
                title = ev["title"] + (f" · {ev['location']}" if ev["location"] else "")
                self.tree.insert("", "end", iid=f"e{k}-{j}", values=(f"● {event_time(ev)}", title), tags=(tag,))
                n += 1

    def manage(self):
        d = SourcesDialog(self, self.app)
        self.wait_window(d)


class SourcesDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Kalender verwalten")
        self.configure(bg=BG)
        self.geometry("560x360")
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Verbundene Kalender", style="H1.TLabel").pack(anchor="w")
        self.tree = ttk.Treeview(f, columns=("name", "type", "state"), show="headings", height=8, selectmode="browse")
        for c, t, w in (("name", "Name", 180), ("type", "Art", 140), ("state", "Status", 180)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, pady=8)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="+ Kalender", style="Accent.TButton", command=self.add).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.remove).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        self.render()
        self.grab_set()
        _center(self, master)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        for s in self.app.cfg.get("calendars", []):
            st = self.app.cal_status.get(s["id"], "")
            self.tree.insert("", "end", iid=s["id"], values=(s["name"], dict(CAL_TYPES).get(s.get("kind", s["type"]),
                                                                                           s["type"]), st))
        if not self.app.cfg.get("calendars"):
            self.tree.insert("", "end", iid="_", values=("Noch kein Kalender verbunden", "", ""))

    def _sel(self):
        s = self.tree.selection()
        return next((c for c in self.app.cfg.get("calendars", []) if s and c["id"] == s[0]), None)

    def _edit_src(self, cur=None):
        d = SourceDialog(self, cur, self.app.cal_password(cur) if cur else "")
        self.wait_window(d)
        if d.result:
            self.app.save_calendar(d.result)
            self.render()

    def add(self):
        self._edit_src()

    def edit(self):
        c = self._sel()
        if c:
            self._edit_src(c)

    def remove(self):
        c = self._sel()
        if c and messagebox.askyesno(APP, f"Kalender „{c['name']}“ entfernen?", parent=self):
            self.app.remove_calendar(c["id"])
            self.render()


# =========================================================================== #
# Weitere FRITZ!Boxen (z. B. Mesh-Repeater)
# =========================================================================== #
class BoxDialog(tk.Toplevel):
    def __init__(self, master, cur=None, password=""):
        super().__init__(master)
        cur = cur or {}
        self.title("FRITZ!Box hinzufügen")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result, self.cur = None, cur
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Weitere FRITZ!Box", style="H1.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.v = {k: tk.StringVar(value=v) for k, v in (("name", cur.get("name", "")), ("host", cur.get("host", "")),
                                                        ("user", cur.get("user", "")), ("pw", password))}
        for i, (k, lbl, show) in enumerate((("name", "Name (z. B. 7530 AX Büro)", ""), ("host", "IP-Adresse", ""),
                                            ("user", "Benutzername", ""), ("pw", "Passwort", "•")), start=1):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Entry(f, textvariable=self.v[k], width=30, show=show).grid(row=i, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Die IP der zweiten Box findest du in der Haupt-Box unter Heimnetz → Mesh bzw. Netzwerk.\n"
                          "Auch hier muss „Zugriff für Anwendungen zulassen“ aktiv sein.\n"
                          "Im Mesh-Repeater-Betrieb gilt oft das Passwort der Haupt-Box.",
                  style="Muted.TLabel", justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        _center(self, master)

    def _ok(self):
        r = {k: v.get().strip() for k, v in self.v.items()}
        if not r["host"]:
            messagebox.showwarning(APP, "Bitte die IP-Adresse der Box eintragen.", parent=self)
            return
        self.result = {"id": self.cur.get("id") or f"b{int(time.time() * 1000) % 10**9}", "name": r["name"],
                       "host": r["host"], "user": r["user"], "password": self.v["pw"].get()}
        self.destroy()


class BoxesDialog(tk.Toplevel):
    def __init__(self, master, app, mesh_known):
        super().__init__(master)
        self.app = app
        self.title("FRITZ!Boxen im Heimnetz")
        self.configure(bg=BG)
        self.geometry("620x380")
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="FRITZ!Boxen im Heimnetz", style="H1.TLabel").pack(anchor="w")
        hint = "Die Haupt-Box kommt aus den Einstellungen. Weitere Boxen siehst du hier."
        if mesh_known:
            hint += "\nIm Mesh gefunden: " + ", ".join(f"{m['model']} ({m['role']})" for m in mesh_known) + \
                    "\nMit Zugangsdaten zeigt die App auch deren WLAN, Laufzeit und FRITZ!OS."
        ttk.Label(f, text=hint, style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 6))
        self.tree = ttk.Treeview(f, columns=("name", "host", "state"), show="headings", height=7, selectmode="browse")
        for c, t, w in (("name", "Name", 200), ("host", "Adresse", 140), ("state", "Status", 220)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="+ FRITZ!Box", style="Accent.TButton", command=self.add).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.remove).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        self.render()
        self.grab_set()
        _center(self, master)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", iid="_main", values=(self.app.main_info.get("model", "Haupt-Box"),
                                                         self.app.cfg["host"], "Haupt-Box ✓"))
        for b in self.app.cfg.get("boxes", []):
            info = self.app.extra_info.get(b["id"], {})
            st = ("⚠ " + info["error"]) if info.get("error") else ("✓ " + info["ov"].get("model", "verbunden")
                                                                   if info.get("ov") else "verbinde …")
            self.tree.insert("", "end", iid=b["id"], values=(b.get("name") or b["host"], b["host"], st))

    def _sel(self):
        s = self.tree.selection()
        return next((b for b in self.app.cfg.get("boxes", []) if s and b["id"] == s[0]), None)

    def add(self):
        d = BoxDialog(self, None, self.app.password)
        self.wait_window(d)
        if d.result:
            self.app.save_box(d.result)
            self.render()

    def edit(self):
        b = self._sel()
        if not b:
            return
        from_pw = self.app.box_password(b)
        d = BoxDialog(self, b, from_pw)
        self.wait_window(d)
        if d.result:
            self.app.save_box(d.result)
            self.render()

    def remove(self):
        b = self._sel()
        if b and messagebox.askyesno(APP, f"„{b.get('name') or b['host']}“ entfernen?", parent=self):
            self.app.remove_box(b["id"])
            self.render()


# =========================================================================== #
# Anwesenheit
# =========================================================================== #
class PersonDialog(tk.Toplevel):
    def __init__(self, master, hosts, cur=None, described=None):
        super().__init__(master)
        cur = cur or {}
        self.described = described or {}  # MAC -> Einordnung (discover.classify)
        self.kind_touched = bool(cur)
        self.title("Person / Gegenstand")
        self.configure(bg=BG)
        self.geometry("620x560")
        self.transient(master)
        self.result, self.cur = None, cur
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Person oder Gegenstand", style="H1.TLabel").pack(anchor="w")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(8, 0))
        ttk.Label(top, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.v_name = tk.StringVar(value=cur.get("name", ""))
        ttk.Entry(top, textvariable=self.v_name, width=28).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(top, text="Als „weg“ nach (Min.)").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        self.v_delay = tk.StringVar(value=str(cur.get("delay", 10)))
        ttk.Entry(top, textvariable=self.v_delay, width=6).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(top, text="Art").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=3)
        self.v_kind = tk.StringVar(value="Gegenstand (z. B. Auto, Uhr, Tablet)" if cur.get("kind") == "item"
                                   else "Person")
        cb = ttk.Combobox(top, textvariable=self.v_kind, values=["Person", "Gegenstand (z. B. Auto, Uhr, Tablet)"],
                          state="readonly", width=34)
        cb.grid(row=2, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: setattr(self, "kind_touched", True))
        self.l_det = ttk.Label(top, text="", style="Muted.TLabel")
        self.l_det.grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="Welche Geräte gehören dazu? (Mehrfachauswahl mit Strg + Klick)").pack(anchor="w", pady=(10, 2))
        self.lb = tk.Listbox(f, selectmode="multiple", bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none", height=14)
        self.lb.pack(fill="both", expand=True)
        known = {m.upper() for m in cur.get("macs", [])}
        seen = set()
        self.macs = []
        rows = sorted(hosts, key=lambda h: (h["mac"].upper() not in known, not h["active"], (h["name"] or "").lower()))
        for h in rows:
            mac = (h["mac"] or "").upper()
            if not mac or mac in seen:
                continue
            seen.add(mac)
            wl = "WLAN" if h["iface"] == "802.11" else ("LAN" if h["iface"] == "Ethernet" else "")
            c = self.described.get(mac, {}).get("category", "")
            self.lb.insert("end", f"{'●' if h['active'] else '○'}  {h['name'] or '(ohne Namen)'}   ·   {h['ip'] or '–'}"
                                  f"   ·   {mac}   {wl}" + (f"   ·   {c}" if c and c != "Unbekannt" else ""))
            self.macs.append(mac)
            if mac in known:
                self.lb.selection_set("end")
        for mac in known - seen:  # Gerät gerade nicht in der Liste der FRITZ!Box
            self.lb.insert("end", f"○  (unbekannt)   ·   {mac}")
            self.macs.append(mac)
            self.lb.selection_set("end")
        ttk.Label(f, text="Am besten das Handy nehmen. Wichtig beim iPhone: Einstellungen → WLAN → (i) bei deinem "
                          "WLAN →\n„Private WLAN-Adresse“ auf „Fest“ oder „Aus“ stellen, sonst wechselt die Adresse und "
                          "die Erkennung klappt nicht.\nHandys trennen das WLAN im Standby manchmal kurz – deshalb gibt "
                          "es die Wartezeit, bevor jemand als „weg“ gilt.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.lb.bind("<<ListboxSelect>>", lambda e: self._guess())
        self.grab_set()
        _center(self, master)

    def _guess(self):
        """Erstes ausgewähltes Gerät einordnen: Handy → Person, sonst Gegenstand (solange Art nicht selbst gewählt)."""
        sel = self.lb.curselection()
        if not sel:
            self.l_det.configure(text="")
            return
        d = self.described.get(self.macs[sel[0]], {})
        cat = d.get("category") or "nicht sicher erkannt"
        person = d.get("role") == "person"
        if not self.kind_touched:
            self.v_kind.set("Person" if person else "Gegenstand (z. B. Auto, Uhr, Tablet)")
        self.l_det.configure(text=f"🔎 Erkannt: {cat} → {'Person' if person else 'Gegenstand'} vorgeschlagen")
        if not self.v_name.get().strip() and not person and d.get("category") not in (None, "", "Unbekannt"):
            self.v_name.set(d["category"])

    def _ok(self):
        name = self.v_name.get().strip()
        macs = [self.macs[i] for i in self.lb.curselection()]
        try:
            delay = max(0.0, float(self.v_delay.get().replace(",", ".")))
        except ValueError:
            delay = 10.0
        if not name or not macs:
            messagebox.showwarning(APP, "Bitte einen Namen eingeben und mindestens ein Gerät auswählen.", parent=self)
            return
        self.result = {"id": self.cur.get("id") or f"p{int(time.time() * 1000) % 10**9}", "name": name, "macs": macs,
                       "delay": delay, "kind": "person" if self.v_kind.get() == "Person" else "item"}
        self.destroy()


class PersonsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Anwesenheit")
        self.configure(bg=BG)
        self.geometry("560x360")
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Personen & Gegenstände", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Die App erkennt anhand der Geräte im WLAN, wer zu Hause bzw. was da ist – Personen über ihr "
                          "Handy,\nGegenstände über alles mit WLAN (Auto, Uhr, Tablet …). Es passiert dabei nichts "
                          "automatisch –\nwas geschehen soll, legst du selbst unter Automationen fest (Auslöser "
                          "„Anwesenheit“ oder Bedingung „Anwesenheit“).", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(2, 6))
        self.tree = ttk.Treeview(f, columns=("name", "state", "dev"), show="headings", height=7, selectmode="browse")
        for c, t, w in (("name", "Name", 140), ("state", "Status", 160), ("dev", "Geräte", 200)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="+ Person / Gegenstand", style="Accent.TButton", command=self.add).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.remove).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        self.render()
        self.grab_set()
        _center(self, master)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.app.cfg.get("persons", []):
            st = self.app.presence.get(p["id"], {}).get("state")
            item = p.get("kind") == "item"
            txt = {"home": "✔ da" if item else "🏠 zu Hause", "away": "weg" if item else "unterwegs"}.get(st, "noch unbekannt")
            self.tree.insert("", "end", iid=p["id"], values=(("🔑 " if item else "") + p["name"], txt,
                                                              f"{len(p['macs'])} Gerät(e)"))

    def _sel(self):
        s = self.tree.selection()
        return next((p for p in self.app.cfg.get("persons", []) if s and p["id"] == s[0]), None)

    def _edit(self, cur=None):
        try:
            described = {d["mac"]: d for d in self.app.describe_hosts()}
        except Exception:  # noqa: BLE001 – Einordnung ist nur eine Hilfe
            described = {}
        d = PersonDialog(self, self.app.data["hosts"], cur, described)
        self.wait_window(d)
        if d.result:
            self.app.save_person(d.result)
            self.render()

    def add(self):
        self._edit()

    def edit(self):
        p = self._sel()
        if p:
            self._edit(p)

    def remove(self):
        p = self._sel()
        if p and messagebox.askyesno(APP, f"„{p['name']}“ entfernen?", parent=self):
            self.app.remove_person(p["id"])
            self.render()


# =========================================================================== #
# Raspberry Pi
# =========================================================================== #
class PiDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Heimserver")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Hauskern auf dem Heimserver", style="H1.TLabel").grid(row=0, column=0, columnspan=3,
                                                                                 sticky="w")
        ttk.Label(f, text="Der Heimserver läuft rund um die Uhr, der PC kann aus bleiben. Bedienen kannst du dann alles über "
                          "die Handy-Oberfläche –\nauch hier am PC im Browser. Einstellungen machst du weiter hier und "
                          "schickst sie mit einem Klick an den Heimserver.", style="Muted.TLabel", justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))

        ttk.Label(f, text="1. Erstes Einrichten", style="H1.TLabel").grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Erstellt „Hauskern-Umzug.zip“ (Einstellungen inkl. Passwörtern + Verlauf). Zusammen mit dem "
                          "Hauskern-Ordner auf den Heimserver kopieren\nund dort install.sh starten. Die Datei danach löschen – "
                          "sie enthält deine Zugangsdaten im Klartext.", style="Muted.TLabel",
                  justify="left").grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Button(f, text="Umzugspaket auf dem Desktop erstellen", command=self.package).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 14))

        ttk.Label(f, text="2. Einstellungen später übertragen", style="H1.TLabel").grid(row=5, column=0, columnspan=3,
                                                                                       sticky="w")
        self.v_host = tk.StringVar(value=app.cfg.get("pi_host") or "raspberrypi")
        self.v_port = tk.StringVar(value=str(app.cfg.get("pi_port", 8770)))
        self.v_pin = tk.StringVar()
        for i, (lbl, var, show, w) in enumerate((("Adresse des Heimservers", self.v_host, "", 24), ("Port", self.v_port, "", 8),
                                                 ("PIN der Handy-Oberfläche", self.v_pin, "•", 14)), start=6):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 12))
            ttk.Entry(f, textvariable=var, width=w, show=show).grid(row=i, column=1, sticky="w", pady=3)
        b = ttk.Frame(f)
        b.grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 4))
        ttk.Button(b, text="Einstellungen an den Heimserver senden", style="Accent.TButton", command=self.send).pack(side="left")
        ttk.Button(b, text="Im Browser öffnen", command=self.open_browser).pack(side="left", padx=6)
        self.l = ttk.Label(f, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.l.grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 10))

        ttk.Label(f, text="3. Dieser PC", style="H1.TLabel").grid(row=11, column=0, columnspan=3, sticky="w")
        self.v_sat = tk.BooleanVar(value=app.cfg.get("satellite", False))
        ttk.Checkbutton(f, text="Der Heimserver übernimmt – auf diesem PC keine Automationen ausführen",
                        variable=self.v_sat, command=self._sat).grid(row=12, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Wichtig, sobald der Heimserver läuft: sonst würden Automationen doppelt ausgeführt.",
                  style="Muted.TLabel").grid(row=13, column=0, columnspan=3, sticky="w")
        bb = ttk.Frame(f)
        bb.grid(row=14, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(bb, text="Probelauf wie auf dem Heimserver …", command=lambda: (self.destroy(), app.probe_dialog())).pack(
            side="left")
        ttk.Button(bb, text="Schließen", command=self.destroy).pack(side="right")
        self.grab_set()
        _center(self, master)

    def _remember(self):
        self.app.cfg["pi_host"] = self.v_host.get().strip()
        try:
            self.app.cfg["pi_port"] = int(self.v_port.get())
        except ValueError:
            pass
        self.app.save()

    def _sat(self):
        self.app.cfg["satellite"] = self.v_sat.get()
        self._remember()
        self.app.log("App", APP, "PC-Modus: " + ("Raspberry Heimserver übernimmt" if self.v_sat.get() else "PC steuert selbst"))

    def package(self):
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            p = self.app.make_migration_package(desk if os.path.isdir(desk) else os.path.expanduser("~"))
        except Exception as e:
            messagebox.showerror(APP, f"Konnte das Paket nicht erstellen: {e}", parent=self)
            return
        self.l.configure(text=f"✓ Erstellt: {p}\nAchtung: enthält deine Passwörter – nach der Installation auf dem Heimserver "
                              "bitte löschen.", foreground=OK)

    def send(self):
        self._remember()
        host, port, pin = self.v_host.get().strip(), self.v_port.get().strip(), self.v_pin.get().strip()
        if not (host and pin):
            self.l.configure(text="Bitte Adresse und PIN eingeben.", foreground=BAD)
            return
        self.l.configure(text="Sende …", foreground=MUTED)

        def run(code=""):
            ok, msg = self.app.send_config_to_pi(host, port, pin, code)
            if ok:
                self.app.log("App", APP, f"Einstellungen an den Heimserver ({host}) gesendet")
            if not ok and msg.startswith("2FA:"):  # Admin hat Zwei-Faktor → Code abfragen und nochmal
                ui_later(self, lambda: ask(msg[4:], bool(code)) if self.winfo_exists() else None)
                return
            ui_later(self, lambda: self.l.configure(text=("✓ " if ok else "⚠ ") + msg, foreground=OK if ok else BAD)
                       if self.winfo_exists() else None)

        def ask(msg, again):
            from tkinter import simpledialog
            code = simpledialog.askstring(APP, ("⚠ " + msg + "\n\n" if again else "") +
                                          "Zwei-Faktor ist an.\nBitte den 6-stelligen Code aus deiner "
                                          "Authenticator-App eingeben:", parent=self)
            if not code or not code.strip():
                self.l.configure(text="Abgebrochen – ohne Code übernimmt der Heimserver nichts.", foreground=BAD)
                return
            self.l.configure(text="Sende …", foreground=MUTED)
            threading.Thread(target=run, args=(code.strip(),), daemon=True).start()
        threading.Thread(target=run, daemon=True).start()

    def open_browser(self):
        import webbrowser
        self._remember()
        webbrowser.open(f"http://{self.v_host.get().strip()}:{self.v_port.get().strip()}")


# =========================================================================== #
# Push-Nachrichten
# =========================================================================== #
class PushDialog(tk.Toplevel):
    def __init__(self, master, app, kinds):
        import push as P
        super().__init__(master)
        self.app, self.P = app, P
        self.title("Push-Nachrichten")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        c = app.push_cfg()
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Push-Nachrichten aufs Handy", style="H1.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.v_en = tk.BooleanVar(value=c.get("enabled", False))
        ttk.Checkbutton(f, text="Push-Nachrichten senden", variable=self.v_en).grid(row=1, column=0, columnspan=3,
                                                                                  sticky="w", pady=(6, 2))
        ttk.Label(f, text="Dienst").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 12))
        self.v_svc = tk.StringVar(value=P.SERVICES.get(c.get("service", "ntfy")))
        cb = ttk.Combobox(f, textvariable=self.v_svc, values=list(P.SERVICES.values()), state="readonly", width=24)
        cb.grid(row=2, column=1, sticky="w", pady=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._show())
        self.vals = {
            "ntfy": {k: tk.StringVar(value=c.get("ntfy", {}).get(k, d)) for k, d in
                     (("server", "https://ntfy.sh"), ("topic", ""), ("token", ""))},
            "pushover": {k: tk.StringVar(value=c.get("pushover", {}).get(k, "")) for k in ("token", "user")},
            "telegram": {k: tk.StringVar(value=c.get("telegram", {}).get(k, "")) for k in ("token", "chat_id")},
        }
        self.box = ttk.Frame(f)
        self.box.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))

        ttk.Label(f, text="Was soll aufs Handy?").grid(row=4, column=0, sticky="nw", pady=(6, 0))
        kf = ttk.Frame(f)
        kf.grid(row=4, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ck = c.get("kinds", {})
        self.v_kinds = {}
        for i, (k, lbl) in enumerate(list(kinds.items()) + [("automation", "Automationen (Aktion „Hinweis“)")]):
            v = tk.BooleanVar(value=ck.get(k, k != "presence"))
            ttk.Checkbutton(kf, text=lbl, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 14), pady=1)
            self.v_kinds[k] = v
        self.v_photo = tk.BooleanVar(value=c.get("printer_photo", True))
        ttk.Checkbutton(f, text="Bei „Druck fertig“ ein Foto aus der Druckerkamera mitschicken",
                        variable=self.v_photo).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 2))
        q = c.get("quiet", {})
        qf = ttk.Frame(f)
        qf.grid(row=6, column=0, columnspan=3, sticky="w", pady=2)
        self.v_q = tk.BooleanVar(value=q.get("enabled", False))
        ttk.Checkbutton(qf, text="Ruhezeit von", variable=self.v_q).pack(side="left")
        self.v_qf, self.v_qt = tk.StringVar(value=q.get("from", "22:00")), tk.StringVar(value=q.get("to", "07:00"))
        ttk.Entry(qf, textvariable=self.v_qf, width=6).pack(side="left", padx=4)
        ttk.Label(qf, text="bis").pack(side="left")
        ttk.Entry(qf, textvariable=self.v_qt, width=6).pack(side="left", padx=4)
        ttk.Label(qf, text="– dann nur Wichtiges (Fehler, Internet weg …)", style="Muted.TLabel").pack(side="left")

        self.v_week = tk.BooleanVar(value=bool((app.cfg.get("weekly") or {}).get("enabled")))
        ttk.Checkbutton(f, text="📅 Wochenrückblick sonntags um 18 Uhr (Strom, Drucke, Filament, Batterien, Anrufe …)",
                        variable=self.v_week).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.l = ttk.Label(f, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.l.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
        b = ttk.Frame(f)
        b.grid(row=9, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(b, text="Test-Nachricht senden", command=self._test).pack(side="right", padx=6)
        self._show()
        self.grab_set()
        _center(self, master)

    def _svc(self):
        return next(k for k, n in self.P.SERVICES.items() if n == self.v_svc.get())

    def _show(self):
        for w in self.box.winfo_children():
            w.destroy()
        svc = self._svc()
        v = self.vals[svc]
        if svc == "ntfy":
            rows = [("Thema (Topic)", v["topic"], ""), ("Server", v["server"], ""), ("Zugangs-Token (optional)", v["token"], "•")]
            hint = ("1. App „ntfy“ aus dem App Store laden.\n"
                    "2. Hier auf „Zufälliges Thema“ klicken und speichern.\n"
                    "3. In der ntfy-App auf „+“ → genau dieses Thema eintragen (Server: ntfy.sh) → abonnieren.\n"
                    "Das Thema ist wie ein Passwort: Wer es kennt, kann deine Nachrichten mitlesen – nicht weitergeben.\n"
                    "Die Nachrichten laufen über den Server von ntfy.sh.")
        elif svc == "pushover":
            rows = [("API-Token (Application)", v["token"], "•"), ("User-Key", v["user"], "•")]
            hint = ("1. Bei pushover.net registrieren und die Pushover-App installieren (nach der Testzeit einmalig kostenpflichtig).\n"
                    "2. Der User-Key steht im Dashboard.\n"
                    "3. Unter „Create an Application/API Token“ eine App „Hauskern“ anlegen → API-Token kopieren.")
        else:
            rows = [("Bot-Token", v["token"], "•"), ("Chat-ID", v["chat_id"], "")]
            hint = ("1. In Telegram @BotFather öffnen → /newbot → Namen vergeben → Token kopieren.\n"
                    "2. Deinem neuen Bot einmal „Hallo“ schreiben.\n"
                    "3. Hier auf „Chat-ID ermitteln“ klicken.")
        for i, (lbl, var, show) in enumerate(rows):
            ttk.Label(self.box, text=lbl).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 12))
            ttk.Entry(self.box, textvariable=var, width=40, show=show).grid(row=i, column=1, sticky="w", pady=3)
        if svc == "ntfy":
            ttk.Button(self.box, text="Zufälliges Thema", command=self._rand_topic).grid(row=0, column=2, padx=6)
        if svc == "telegram":
            ttk.Button(self.box, text="Chat-ID ermitteln", command=self._find_chat).grid(row=1, column=2, padx=6)
        ttk.Label(self.box, text=hint, style="Muted.TLabel", justify="left").grid(row=len(rows), column=0, columnspan=3,
                                                                                  sticky="w", pady=(6, 0))

    def _rand_topic(self):
        import secrets as _s
        self.vals["ntfy"]["topic"].set("hauskern-" + _s.token_urlsafe(12).replace("_", "x").replace("-", "y").lower())

    def _find_chat(self):
        tok = self.vals["telegram"]["token"].get().strip()
        self.l.configure(text="Suche Chat …", foreground=MUTED)

        def run():
            try:
                cid, name = self.P.telegram_find_chat(tok)
                ui_later(self, lambda: (self.vals["telegram"]["chat_id"].set(cid),
                                       self.l.configure(text=f"✓ Chat gefunden: {name} ({cid})", foreground=OK)))
            except Exception as e:
                ui_later(self, lambda e=e: self.l.configure(text=f"⚠ {e}", foreground=BAD))
        threading.Thread(target=run, daemon=True).start()

    def _collect(self):
        c = {"enabled": self.v_en.get(), "service": self._svc(), "printer_photo": self.v_photo.get(),
             "kinds": {k: v.get() for k, v in self.v_kinds.items()},
             "quiet": {"enabled": self.v_q.get(), "from": self.v_qf.get().strip(), "to": self.v_qt.get().strip()}}
        for svc, vars_ in self.vals.items():
            c[svc] = {k: v.get().strip() for k, v in vars_.items()}
        return c

    def _test(self):
        c = self._collect()
        self.l.configure(text="Sende …", foreground=MUTED)

        def run():
            try:
                self.app.pusher.send_now(c, "Hauskern 🏠", "Test erfolgreich – Push-Nachrichten funktionieren! 🎉",
                                         click=self.app.web_click_url())
                msg, col = "✓ Gesendet – schau aufs Handy 📱", OK
            except Exception as e:
                msg, col = f"⚠ {e}", BAD
            ui_later(self, lambda: self.l.configure(text=msg, foreground=col) if self.winfo_exists() else None)
        threading.Thread(target=run, daemon=True).start()

    def _ok(self):
        c = self._collect()
        if c["enabled"] and c["service"] == "ntfy" and not c["ntfy"]["topic"]:
            messagebox.showwarning(APP, "Bitte ein Thema eintragen oder „Zufälliges Thema“ klicken.", parent=self)
            return
        self.app.save_push_cfg(c)
        self.app.set_weekly(self.v_week.get())
        self.destroy()


# =========================================================================== #
# Diagnose & Probelauf
# =========================================================================== #
DIAG_ICON = {"ok": "✅", "warn": "⚠️", "bad": "❌", "info": "ℹ️"}


class DiagTree(ttk.Frame):
    """Ergebnisliste der Diagnose, nach Bereichen gruppiert; Tipps stehen direkt unter Problemen."""

    def __init__(self, master, height=18):
        super().__init__(master)
        self.tree = ttk.Treeview(self, columns=("detail",), show="tree headings", height=height)
        self.tree.heading("#0", text="Prüfung")
        self.tree.heading("detail", text="Ergebnis")
        self.tree.column("#0", width=300, stretch=False)
        self.tree.column("detail", width=640, stretch=True)
        self.tree.tag_configure("group", foreground=FG, font=("Segoe UI Semibold", 10))
        self.tree.tag_configure("bad", foreground=BAD)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("info", foreground=MUTED)
        self.tree.tag_configure("hint", foreground="#f6c15a")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._sig = None

    def set(self, results):
        sig = repr(results)
        if sig == self._sig:
            return
        self._sig = sig
        t = self.tree
        t.delete(*t.get_children())
        groups = {}
        for r in results:
            g = groups.get(r["group"])
            if g is None:
                worst = max((x["status"] for x in results if x["group"] == r["group"]),
                            key=lambda s: ("info", "ok", "warn", "bad").index(s))
                g = groups[r["group"]] = t.insert("", "end", text=f"{DIAG_ICON[worst]}  {r['group']}", open=True,
                                                  tags=("group",))
            iid = t.insert(g, "end", text=f"{DIAG_ICON[r['status']]}  {r['name']}", values=(r["detail"],),
                           tags=(r["status"],), open=True)
            if r.get("hint") and r["status"] in ("warn", "bad"):
                t.insert(iid, "end", text="", values=("→ " + r["hint"],), tags=("hint",))


class DiagnosePage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.b_run = ttk.Button(bar, text="Jetzt prüfen", style="Accent.TButton", command=self.run)
        self.b_run.pack(side="left")
        self.l_sum = ttk.Label(bar, text="", font=("Segoe UI Semibold", 11))
        self.l_sum.pack(side="left", padx=14)
        self.l_info = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_info.pack(side="left")
        ttk.Button(bar, text="Probelauf wie auf dem Heimserver …", command=app.probe_dialog).pack(side="right")
        ttk.Button(bar, text="Als Text kopieren", command=self.copy).pack(side="right", padx=6)
        ttk.Label(self, text="Prüft alle Verbindungen und Einstellungen und sagt dir, was zu tun ist. Kamerabilder werden "
                             "dabei kurz abgerufen – das dauert bis zu einer Minute.", style="Muted.TLabel").pack(
            anchor="w", pady=(0, 6))
        self.dt = DiagTree(self)
        self.dt.pack(fill="both", expand=True)

    def run(self):
        self.app.run_diagnose("Knopf")
        self.refresh()

    def refresh(self):
        import diagnose
        a = self.app
        self.b_run.configure(state="disabled" if a.diag_running else "normal",
                             text="Prüfe …" if a.diag_running else "Jetzt prüfen")
        if a.diag_results:
            self.l_sum.configure(text=diagnose.summary_text(a.diag_results))
            when = time.strftime("%d.%m. %H:%M", time.localtime(a.diag_time))
            self.l_info.configure(text=(a.diag_progress + " · " if a.diag_running else "") + f"zuletzt {when}")
            self.dt.set(a.diag_results)
        else:
            self.l_sum.configure(text="")
            self.l_info.configure(text=a.diag_progress if a.diag_running else "Noch keine Prüfung")

    def copy(self):
        import diagnose
        if not self.app.diag_results:
            return
        self.clipboard_clear()
        self.clipboard_append(diagnose.as_text(self.app.diag_results))
        messagebox.showinfo(APP, "Ergebnis kopiert – du kannst es einfach in den Chat einfügen 🙂", parent=self)


class ProbeDialog(tk.Toplevel):
    """Startet Hauskern ein zweites Mal ohne Fenster – genau so, wie es später auf dem Heimserver läuft."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Probelauf wie auf dem Heimserver")
        self.configure(bg=BG)
        self.geometry("980x640")
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Probelauf wie auf dem Heimserver", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Hauskern startet hier ein zweites Mal – ohne Fenster und mit genau den Einstellungen, die der "
                          "Heimserver bekommen würde.\nSo merken wir Probleme jetzt und nicht erst, wenn der Heimserver im Schrank steht. "
                          "Automationen und Push-Nachrichten\nbleiben dabei aus (sonst käme alles doppelt). "
                          "Schließt du dieses Fenster, endet der Probelauf.", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(2, 10))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.b_start = ttk.Button(bar, text="Probelauf starten", style="Accent.TButton", command=self.start)
        self.b_start.pack(side="left")
        self.b_diag = ttk.Button(bar, text="Diagnose wiederholen", command=self.again, state="disabled")
        self.b_diag.pack(side="left", padx=6)
        self.b_stop = ttk.Button(bar, text="Beenden", command=self.stop, state="disabled")
        self.b_stop.pack(side="left")
        self.l_state = ttk.Label(bar, text="", font=("Segoe UI Semibold", 10))
        self.l_state.pack(side="left", padx=14)
        self.l_url = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.l_url.pack(anchor="w", pady=(8, 4))
        self.dt = DiagTree(f, height=12)
        self.dt.pack(fill="both", expand=True, pady=(4, 6))
        ttk.Label(f, text="Warnungen und Fehler aus dem Protokoll des Probelaufs:", style="Muted.TLabel").pack(anchor="w")
        self.t_log = tk.Text(f, height=6, bg=CARD, fg=FG, relief="flat", font=("Consolas", 9), wrap="none")
        self.t_log.pack(fill="x", pady=(2, 8))
        ttk.Button(f, text="Schließen", command=self.close).pack(anchor="e")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._log_sig = None
        self._alive = True
        _center(self, master)
        if app.probe_running():
            self._buttons(True)
        self.tick()

    def _buttons(self, running):
        self.b_start.configure(state="disabled" if running else "normal")
        self.b_diag.configure(state="normal" if running else "disabled")
        self.b_stop.configure(state="normal" if running else "disabled")

    def start(self):
        ok, msg = self.app.probe_start()
        if not ok:
            messagebox.showwarning(APP, msg, parent=self)
            return
        self._buttons(True)

    def again(self):
        self.app.probe_request_diag()
        self.l_state.configure(text="Diagnose läuft …")

    def stop(self):
        self.app.probe_stop()
        self._buttons(False)

    def close(self):
        self._alive = False
        self.app.probe_stop()
        self.destroy()

    def tick(self):
        if not self._alive:
            return
        try:
            st = self.app.probe_poll()
        except Exception:  # noqa: BLE001
            st = None
        if st:
            self._show(st)
        self.after(2000, self.tick)

    def _show(self, st):
        import diagnose
        if not st["running"]:
            self._buttons(False)
        up = int(st["uptime"])
        d = st["diag"]
        if st["running"]:
            if d:
                txt = f"läuft seit {up // 60}:{up % 60:02d} · " + diagnose.summary_text(d["results"])
            else:
                txt = f"läuft seit {up // 60}:{up % 60:02d} · erste Diagnose nach ca. 1 Minute …"
        elif self.app.probe_started:
            txt = "beendet" + (" – siehe Protokoll unten" if st["errors"] else "")
        else:
            txt = "noch nicht gestartet"
        self.l_state.configure(text=txt, foreground=BAD if st["errors"] else FG)
        if st["running"] and st["urls"]:
            self.l_url.configure(text="Teste parallel die Handy-Oberfläche des Probelaufs (gleiche PIN):  " +
                                      "   ".join(st["urls"][:2]))
        else:
            self.l_url.configure(text="")
        if d:
            self.dt.set(d["results"])
        sig = tuple(st["log"])
        if sig != self._log_sig:
            self._log_sig = sig
            self.t_log.configure(state="normal")
            self.t_log.delete("1.0", "end")
            self.t_log.insert("end", "\n".join(st["log"]) or "– nichts Auffälliges –")
            self.t_log.see("end")
            self.t_log.configure(state="disabled")


# =========================================================================== #
# Verlauf (Diagramme)
# =========================================================================== #
class HistoryPage(ttk.Frame):
    RANGES = [("24 Std", 86400, 300), ("7 Tage", 7 * 86400, 1800), ("30 Tage", 30 * 86400, 7200),
              ("12 Monate", 365 * 86400, 86400)]

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        from charts import Chart
        self.app = app
        self.sources, self.range_i, self.offset, self._busy, self._req = [], 0, 0, False, 0
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Anzeigen:").pack(side="left")
        self.v_src = tk.StringVar()
        self.cb = ttk.Combobox(bar, textvariable=self.v_src, state="readonly", width=34)
        self.cb.pack(side="left", padx=6)
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.b_next = ttk.Button(bar, text="›", width=3, command=lambda: self.shift(-1))
        self.b_next.pack(side="right")
        self.l_win = ttk.Label(bar, text="", style="Muted.TLabel", width=26, anchor="center")
        self.l_win.pack(side="right", padx=4)
        ttk.Button(bar, text="‹", width=3, command=lambda: self.shift(1)).pack(side="right")
        self.rb = []
        for i, (lbl, _, _) in enumerate(reversed(self.RANGES)):
            b = ttk.Button(bar, text=lbl, command=lambda i=len(self.RANGES) - 1 - i: self.set_range(i))
            b.pack(side="right", padx=2)
            self.rb.insert(0, b)
        grid = ttk.Frame(self)
        grid.pack(fill="x")
        self.cards = []
        for c in range(4):
            grid.columnconfigure(c, weight=1, uniform="h")
            f = ttk.Frame(grid, style="Card.TFrame", padding=(14, 10))
            f.grid(row=0, column=c, sticky="nsew", padx=5, pady=5)
            t = ttk.Label(f, text="", style="CardTitle.TLabel")
            t.pack(anchor="w")
            v = ttk.Label(f, text="", style="CardValue.TLabel")
            v.pack(anchor="w")
            self.cards.append((t, v))
        cf = ttk.Frame(self, style="Card.TFrame", padding=10)
        cf.pack(fill="both", expand=True, padx=5, pady=5)
        self.l_c1 = ttk.Label(cf, text="", style="CardTitle.TLabel")
        self.l_c1.pack(anchor="w")
        self.c1 = Chart(cf, height=260)
        self.c1.pack(fill="both", expand=True, pady=(4, 0))
        self.low = ttk.Frame(self, style="Card.TFrame", padding=10)
        self.l_c2 = ttk.Label(self.low, text="", style="CardTitle.TLabel")
        self.l_c2.pack(anchor="w")
        self.c2 = Chart(self.low, height=160)
        self.tw = ttk.Frame(self.low, style="Card.TFrame")
        self.t_out = ttk.Treeview(self.tw, columns=("start", "end", "dur"), show="headings", height=5)
        for c, t, w in (("start", "Beginn", 170), ("end", "Ende", 170), ("dur", "Dauer", 120)):
            self.t_out.heading(c, text=t)
            self.t_out.column(c, width=w, anchor="w")
        self.t_out.pack(fill="both", expand=True)
        self._update_range_buttons()

    # ------------------------------------------------------------ #
    def set_range(self, i):
        self.range_i, self.offset = i, 0
        self._update_range_buttons()
        self.refresh()

    def shift(self, d):
        self.offset = max(0, self.offset + d)
        self.refresh()

    def _update_range_buttons(self):
        for i, b in enumerate(self.rb):
            b.configure(style="Accent.TButton" if i == self.range_i else "TButton")

    def window(self):
        _, span, bucket = self.RANGES[self.range_i]
        t1 = time.time() - self.offset * span
        return t1 - span, t1, bucket

    def refresh(self):
        self.sources = self.app.history_sources()
        labels = [s["label"] for s in self.sources]
        self.cb.configure(values=labels)
        if self.v_src.get() not in labels and labels:
            self.v_src.set(labels[0])
        src = next((s for s in self.sources if s["label"] == self.v_src.get()), None)
        t0, t1, bucket = self.window()
        fmt = "%d.%m. %H:%M" if self.range_i == 0 else "%d.%m.%Y"
        self.l_win.configure(text=f"{time.strftime(fmt, time.localtime(t0))} – "
                             f"{'jetzt' if self.offset == 0 else time.strftime(fmt, time.localtime(t1))}")
        self.b_next.configure(state="normal" if self.offset else "disabled")
        if not src:
            self.c1.show([], t0, t1, empty="Noch keine Geräte – sobald Hauskern verbunden ist, geht's los")
            return
        self._req += 1
        req = self._req

        def work():
            try:
                res = self.app.history_data(src, t0, t1, bucket)
            except Exception as e:  # noqa: BLE001
                res = {"error": str(e)}
            ui_later(self, lambda: self._render(req, src, t0, t1, bucket, res))
        threading.Thread(target=work, daemon=True, name="Verlauf").start()

    def _render(self, req, src, t0, t1, bucket, r):
        if req != self._req:
            return  # inzwischen etwas anderes gewählt
        if "error" in r:
            self.c1.show([], t0, t1, empty="⚠ " + r["error"])
            return
        self.l_c1.configure(text=r.get("title", ""))
        self.c1.show(r["series"], t0, t1, unit=r.get("unit", ""), bucket=r.get("bucket", bucket),
                     y_zero=r.get("y_zero", False), kind=r.get("chart", "line"))
        stats = (r.get("stats", []) + [("", "")] * 4)[:4]
        for (t, v), (tl, vl) in zip(stats, self.cards):
            tl.configure(text=t)
            vl.configure(text=v)
        self.c2.pack_forget()
        self.tw.pack_forget()
        low = r.get("lower")
        if not low:
            self.low.pack_forget()
            return
        self.low.pack(fill="both", expand=False, padx=5, pady=5)
        self.l_c2.configure(text=low["title"])
        if low["kind"] in ("outages", "table"):
            if low["kind"] == "outages":
                cols = ["Beginn", "Ende", "Dauer"]
                rows = [[dstr(a) + time.strftime(" (%Y)", time.localtime(a)),
                         time.strftime("%d.%m. %H:%M", time.localtime(b)), fmt_span(b - a)] for a, b in low["rows"]]
                empty = "Keine Ausfälle in diesem Zeitraum 🎉"
            else:
                cols, rows, empty = low["cols"], low["rows"], "Keine Einträge in diesem Zeitraum"
            ids = [f"c{i}" for i in range(len(cols))]
            self.t_out.configure(columns=ids)
            for cid_, t in zip(ids, cols):
                self.t_out.heading(cid_, text=t)
                self.t_out.column(cid_, width=260 if t in ("Datei", "Beginn") else 150, anchor="w")
            self.t_out.delete(*self.t_out.get_children())
            for i, row in enumerate(rows):
                self.t_out.insert("", "end", iid=str(i), values=row)
            if not rows:
                self.t_out.insert("", "end", values=[empty] + [""] * (len(cols) - 1))
            self.tw.pack(fill="both", expand=True, pady=(4, 0))
        else:
            self.c2.show(low["series"], t0, t1, unit=low.get("unit", ""), kind=low.get("chart", "line"),
                         bucket=low.get("bucket", bucket), label_fmt=low.get("label_fmt"))
            self.c2.pack(fill="both", expand=True, pady=(4, 0))


def fmt_span(s):
    s = int(s)
    if s < 3600:
        return f"{max(1, s // 60)} Min"
    if s < 86400:
        return f"{s // 3600} Std {s % 3600 // 60} Min"
    return f"{s // 86400} T {s % 86400 // 3600} Std"


# =========================================================================== #
# Wetter
# =========================================================================== #
class WeatherDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        from charts import Chart
        self.app = app
        self.title("Wetter")
        self.configure(bg=BG)
        self.geometry("880x720")
        self.transient(master)
        c = app.weather_cfg()
        self.loc = {k: c.get(k) for k in ("name", "lat", "lon")}
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        self.l_head = ttk.Label(f, text="Wetter", style="H1.TLabel")
        self.l_head.pack(anchor="w")
        self.l_now = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.l_now.pack(anchor="w", pady=(2, 8))
        lf = ttk.Frame(f, style="Card.TFrame", padding=12)
        lf.pack(fill="x")
        ttk.Label(lf, text="Standort", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.v_en = tk.BooleanVar(value=c.get("enabled", True))
        ttk.Checkbutton(lf, text="Wetter anzeigen", variable=self.v_en).grid(row=0, column=3, sticky="e")
        self.v_mode = tk.StringVar(value=c.get("mode", "auto"))
        ttk.Radiobutton(lf, text="Automatisch ermitteln (grob über deine Internet-Adresse, auf die Stadt genau)",
                        variable=self.v_mode, value="auto", command=self._mode).grid(row=1, column=0, columnspan=4,
                                                                                    sticky="w", pady=(6, 0))
        ttk.Radiobutton(lf, text="Ort selbst wählen:", variable=self.v_mode, value="manual",
                        command=self._mode).grid(row=2, column=0, sticky="w", pady=4)
        self.v_q = tk.StringVar(value=c.get("name", "") if c.get("mode") == "manual" else "")
        self.e_q = ttk.Entry(lf, textvariable=self.v_q, width=28)
        self.e_q.grid(row=2, column=1, sticky="w", padx=6)
        self.e_q.bind("<Return>", lambda e: self.search())
        self.b_s = ttk.Button(lf, text="Suchen", command=self.search)
        self.b_s.grid(row=2, column=2, sticky="w")
        self.lb = tk.Listbox(lf, height=4, bg=CARD2, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.lb.grid(row=3, column=0, columnspan=4, sticky="we", pady=(4, 0))
        self.lb.bind("<<ListboxSelect>>", lambda e: self._pick())
        self.results = []
        self.l_loc = ttk.Label(lf, text="", style="CardSub.TLabel")
        self.l_loc.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        lf.columnconfigure(3, weight=1)
        af = ttk.Frame(f)
        af.pack(fill="x", pady=(8, 0))
        self.v_al = tk.BooleanVar(value=c.get("alerts", True))
        ttk.Checkbutton(af, text="⛈ Unwetterwarnungen vom DWD", variable=self.v_al).pack(side="left")
        ttk.Label(af, text="  Push ab:").pack(side="left")
        self._al_choices = [(1, "Gelb – jede Warnung"), (2, "Orange – markantes Wetter"), (3, "Rot – Unwetter"),
                            (4, "Violett – nur extrem")]
        self.v_alp = tk.StringVar(value=dict(self._al_choices).get(int(c.get("alert_push", 2)), self._al_choices[1][1]))
        ttk.Combobox(af, textvariable=self.v_alp, values=[n for _, n in self._al_choices], state="readonly",
                     width=26).pack(side="left", padx=4)
        self.l_al = ttk.Label(f, text="", style="Muted.TLabel", justify="left", wraplength=760)
        self.l_al.pack(anchor="w")
        ttk.Label(f, text="Nächste 48 Stunden", style="Muted.TLabel").pack(anchor="w", pady=(12, 2))
        self.chart = Chart(f, height=180)
        self.chart.pack(fill="x")
        self.tree = ttk.Treeview(f, columns=("day", "wx", "temp", "rain", "sun"), show="headings", height=7)
        for col, t, w in (("day", "Tag", 110), ("wx", "Wetter", 220), ("temp", "Temperatur", 130),
                          ("rain", "Regen", 150), ("sun", "Sonne", 150)):
            self.tree.heading(col, text=t)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(10, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Label(b, text="Wetter: Open-Meteo · Warnungen: Deutscher Wetterdienst", style="Muted.TLabel").pack(
            side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Übernehmen", style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(b, text="Jetzt aktualisieren", command=self.refresh_now).pack(side="right", padx=6)
        self._mode()
        self.render()
        _center(self, master)

    def _mode(self):
        st = "normal" if self.v_mode.get() == "manual" else "disabled"
        for w in (self.e_q, self.b_s):
            w.configure(state=st)
        self._loc_text()

    def _loc_text(self):
        if self.v_mode.get() == "auto" and not self.loc.get("lat"):
            self.l_loc.configure(text="Wird beim Übernehmen ermittelt.")
        elif self.loc.get("lat") is not None:
            self.l_loc.configure(text=f"Gewählt: {self.loc.get('name') or '?'} "
                                      f"({self.loc['lat']:.2f}, {self.loc['lon']:.2f})".replace(".", ","))

    def search(self):
        q = self.v_q.get().strip()
        if len(q) < 2:
            return
        self.l_loc.configure(text="Suche …")

        def run():
            import weather as wx
            try:
                res, err = wx.search(q), ""
            except wx.WeatherError as e:
                res, err = [], str(e)
            ui_later(self, lambda: self._show_results(res, err))
        threading.Thread(target=run, daemon=True).start()

    def _show_results(self, res, err):
        self.results = res
        self.lb.delete(0, "end")
        for r in res:
            self.lb.insert("end", f"{r['name']}  ·  {r['region']}")
        self.l_loc.configure(text=("⚠ " + err) if err else ("Bitte einen Ort aus der Liste wählen" if res
                                                             else "Nichts gefunden – anders schreiben?"))

    def _pick(self):
        s = self.lb.curselection()
        if s:
            r = self.results[s[0]]
            self.loc = {"name": r["name"], "lat": r["lat"], "lon": r["lon"]}
            self._loc_text()

    def apply(self):
        mode = self.v_mode.get()
        if mode == "manual" and self.loc.get("lat") is None:
            messagebox.showwarning(APP, "Bitte erst einen Ort suchen und auswählen.", parent=self)
            return
        c = self.app.weather_cfg()
        relocate = mode == "auto" and c.get("mode") != "auto"
        c.update(enabled=self.v_en.get(), mode=mode, alerts=self.v_al.get(),
                 alert_push={n: k for k, n in self._al_choices}.get(self.v_alp.get(), 2))
        if mode == "manual":
            c.update(self.loc)
        elif relocate:
            c.update(name="", lat=None, lon=None)
        self.app.save_weather_cfg(c)
        self.l_now.configure(text="Lade Wetter …")

    def refresh_now(self):
        self.app._weather_busy = False
        self.app.refresh_weather(relocate=self.v_mode.get() == "auto")
        self.l_now.configure(text="Lade Wetter …")

    def render(self):
        import weather as wx
        v = self.app.weather_view()
        w = self.app.weather
        if not v:
            err = self.app._weather_err
            self.l_head.configure(text="Wetter")
            self.l_now.configure(text=("⚠ " + err) if err else "Noch keine Daten – wird geladen …")
            return
        cur = v["current"]
        al = v.get("alerts") or []
        self.l_al.configure(text="\n".join(
            f"{a['icon']} {a['headline']}  ({a['level_name']}{', ' + a['when'] if a['when'] else ''})"
            + (f"\n     {a['instr'][:220]}" if a.get("instr") else "") for a in al)
            or ("Keine Warnungen für deinen Ort." if self.app.weather_cfg().get("alerts", True) else ""),
            foreground=al[0]["color"] if al else MUTED)
        self.l_head.configure(text=f"{v['place'] or 'Wetter'}  ·  {round(cur['temp'])} °C  ·  {v['text']}")
        self.l_now.configure(text=(v["sub"] + (f"   {v['warn']}" if v["warn"] else "") +
                                   f"\nAktualisiert {v['updated']} Uhr"))
        now = time.time()
        hrs = [h for h in w["hourly"] if now - 3600 <= h["ts"] <= now + 48 * 3600]
        if hrs:
            self.chart.show([{"name": "Temperatur", "color": "#ff7849", "pts": [(h["ts"], h["temp"]) for h in hrs]}],
                            hrs[0]["ts"], hrs[-1]["ts"] + 3600, unit="°C", bucket=3600)
        self.tree.delete(*self.tree.get_children())
        for i, d in enumerate(w["daily"]):
            txt, _ = wx.describe(d["code"])
            rain = (f"{d['prob']} %" if d.get("prob") is not None else "–") + \
                   (f" · {d['rain']:.1f} mm".replace(".", ",") if d.get("rain") else "")
            self.tree.insert("", "end", iid=str(i), values=(
                "Heute" if i == 0 else "Morgen" if i == 1 else dstr(d["ts"], False), txt,
                f"{round(d['max'])}° / {round(d['min'])}°", rain, f"↑ {d['sunrise']}  ↓ {d['sunset']}"))


# =========================================================================== #
# Gerätesuche
# =========================================================================== #
class DiscoveryPage(ttk.Frame):
    GROUPS = [("lokal", "🟢 Lokal steuerbar – kann in Hauskern eingebaut werden"),
              ("cloud", "🟠 Nur über die Hersteller-Cloud"),
              ("erkannt", "🔵 Erkannt"), ("drin", "✅ Schon in Hauskern"), ("", "⚪ Unbekannt")]

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.b_scan = ttk.Button(bar, text="Jetzt suchen", style="Accent.TButton", command=self.scan)
        self.b_scan.pack(side="left")
        self.l = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l.pack(side="left", padx=12)
        self.v_unknown = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Unbekannte zeigen", variable=self.v_unknown, command=self.render).pack(side="right")
        ttk.Label(self, text="Sucht im WLAN/LAN nach Smart-Home-Geräten (Bonjour/mDNS, UPnP und die Geräteliste der "
                             "FRITZ!Box) und sagt dir, was sie sind\nund ob Hauskern sie steuern könnte. Es wird nichts "
                             "verändert und nichts ins Internet geschickt.", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(0, 6))
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("cat", "vendor", "ip"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Gerät")
        for c, t, w in (("cat", "Art", 180), ("vendor", "Hersteller / Modell", 220), ("ip", "IP-Adresse", 120)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=330)
        self.tree.tag_configure("group", foreground=FG, font=("Segoe UI Semibold", 10))
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sel_changed())
        det = ttk.Frame(self, style="Card.TFrame", padding=12)
        det.pack(fill="x", pady=(8, 0))
        self.l_det = ttk.Label(det, text="Gerät auswählen, um Details zu sehen.", style="CardSub.TLabel",
                               justify="left", wraplength=900)
        self.l_det.pack(anchor="w")
        bb = ttk.Frame(det, style="Card.TFrame")
        bb.pack(anchor="w", pady=(8, 0))
        self.b_item = ttk.Button(bb, text="＋ Zu Hauskern hinzufügen …", style="Accent.TButton", command=self.as_item,
                                 state="disabled")
        self.b_item.pack(side="left")
        self.b_ping = ttk.Button(bb, text="Zum Ping-Monitor", command=self.to_ping, state="disabled")
        self.b_ping.pack(side="left", padx=6)
        self.b_wol = ttk.Button(bb, text="⏻ Aufweck-Kachel", command=self.to_wol, state="disabled")
        self.b_wol.pack(side="left")
        self.items = {}

    def scan(self):
        self.app.run_discovery()
        self.refresh()

    def refresh(self):
        a = self.app
        self.b_scan.configure(state="disabled" if a.disc_running else "normal",
                              text="Suche läuft …" if a.disc_running else "Jetzt suchen")
        if a.disc_running:
            self.l.configure(text=a.disc_progress)
        elif a.disc_results:
            c = {k: sum(1 for d in a.disc_results if d["status"] == k) for k, _ in self.GROUPS}
            self.l.configure(text=f"{len(a.disc_results)} Geräte · {c['lokal']} lokal steuerbar · {c['cloud']} nur Cloud · "
                                  f"zuletzt {time.strftime('%d.%m. %H:%M', time.localtime(a.disc_time))}")
        else:
            self.l.configure(text="Noch nicht gesucht")
        self.render()

    def render(self):
        t = self.tree
        sel = t.selection()
        t.delete(*t.get_children())
        self.items = {}
        for key, label in self.GROUPS:
            rows = [d for d in self.app.disc_results if d["status"] == key]
            if not rows or (key == "" and not self.v_unknown.get()):
                continue
            g = t.insert("", "end", text=f"{label}  ({len(rows)})", open=True, tags=("group",))
            for d in rows:
                iid = "d" + d["ip"]
                vend = " · ".join(x for x in (d.get("vendor"), d.get("model")) if x)
                t.insert(g, "end", iid=iid, text=d["name"] or d["ip"], values=(d["category"], vend, d["ip"]))
                self.items[iid] = d
        if sel and t.exists(sel[0]):
            t.selection_set(sel[0])

    def _cur(self):
        s = self.tree.selection()
        return self.items.get(s[0]) if s else None

    def _sel_changed(self):
        d = self._cur()
        for b in (self.b_item, self.b_ping, self.b_wol):
            b.configure(state="disabled")
        if not d:
            return
        import discover
        lines = [f"{d['name']}  ·  {d['ip']}" + (f"  ·  {d['mac']}" if d.get("mac") else ""),
                 f"Status: {discover.STATUS_TEXT.get(d['status'], 'unbekannt')}"]
        if d.get("hint"):
            lines.append(d["hint"])
        info = []
        if d.get("services"):
            info.append("Dienste: " + ", ".join(d["services"]))
        if d.get("server"):
            info.append("UPnP: " + d["server"])
        info.append("gefunden über: " + ", ".join(d.get("via", [])))
        try:
            use = self.app.device_usage(d.get("mac"))
            if use:
                info.append("Schon in Hauskern: " + ", ".join(use))
            if d.get("mac"):
                tgt, allh = self.app.describe_host(d["mac"], d["ip"])
                rel = discover.related(tgt or {}, allh) if tgt else []
                if rel:
                    info.append("Gehört vermutlich dazu: " + ", ".join(f"{o['name']} ({w})" for o, w in rel[:6]))
                if tgt and tgt.get("hub"):
                    info.append("Basisstation: Geräte, die per Funk daran hängen, sind im Netz nicht sichtbar – du kannst "
                                "sie beim Hinzufügen eintragen.")
        except Exception:  # noqa: BLE001 – nur Zusatzinfo
            pass
        self.l_det.configure(text="\n".join(lines + info))
        if d.get("mac"):
            self.b_item.configure(state="normal")
            self.b_wol.configure(state="normal")
        self.b_ping.configure(state="normal")

    def as_item(self):
        d = self._cur()
        if not d or not d.get("mac"):
            return
        self.app.add_device_dialog(d["mac"], d.get("ip", ""), parent=self.winfo_toplevel())
        self.render()

    def to_ping(self):
        d = self._cur()
        if d:
            self.app.add_ping_target(d["name"] or d["ip"], d["ip"])
            messagebox.showinfo(APP, f"„{d['name']}“ steht jetzt im Ping-Monitor.", parent=self)

    def to_wol(self):
        d = self._cur()
        if d and d.get("mac"):
            self.app.add_wol(d["mac"], d["name"] or d["ip"])
            messagebox.showinfo(APP, f"Aufweck-Kachel für „{d['name']}“ angelegt.", parent=self)


# =========================================================================== #
# Druckkosten
# =========================================================================== #
def parse_hours(txt):
    t = str(txt or "").strip().replace(",", ".")
    if not t:
        return 0.0
    if ":" in t:
        h, _, m = t.partition(":")
        return float(h or 0) + float(m or 0) / 60
    return float(t)


def fmt_hours(h):
    h = max(0.0, float(h or 0))
    hh = int(h)
    mm = round((h - hh) * 60)
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{hh}:{mm:02d}"


class CostPage(ttk.Frame):
    ROWS = 4

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        import pricing
        self.P = pricing
        self.app = app
        self._after = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Neu", command=self.new).pack(side="left")
        ttk.Button(bar, text="Aus .3mf-Datei (Bambu Studio) …", command=self.from_3mf).pack(side="left", padx=6)
        ttk.Button(bar, text="Aus fertigem Druck …", command=self.from_print).pack(side="left")
        ttk.Button(bar, text="⚙ Kosten & Filamente …", command=self.settings).pack(side="right")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="c")
        body.columnconfigure(1, weight=1, uniform="c")
        body.rowconfigure(1, weight=1)

        # ---- Auftrag
        f = ttk.Frame(body, style="Card.TFrame", padding=14)
        f.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(f, text="AUFTRAG", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        self.v = {k: tk.StringVar(value=v) for k, v in (("name", ""), ("qty", "1"), ("time", "0:00"), ("kwh", ""),
                                                          ("labor", ""), ("extras", ""), ("margin", ""))}

        def row(r, label, key, w=10, hint=""):
            ttk.Label(f, text=label, style="CardSub.TLabel").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            ttk.Entry(f, textvariable=self.v[key], width=w).grid(row=r, column=1, columnspan=2 if w > 20 else 1,
                                                                 sticky="w", pady=3)
            if hint:
                ttk.Label(f, text=hint, style="CardSub.TLabel").grid(row=r, column=2 if w <= 20 else 3, columnspan=2,
                                                                     sticky="w", padx=6)
        row(1, "Bezeichnung", "name", 34)
        row(2, "Stückzahl", "qty", 6, "gleiche Teile in diesem Druck")
        row(3, "Druckzeit", "time", 8, "Std:Min (z. B. 3:25)")
        ttk.Label(f, text="Filament", style="CardSub.TLabel").grid(row=4, column=0, sticky="nw", pady=(8, 3))
        self.fil_rows = []
        for i in range(self.ROWS):
            vf, vg = tk.StringVar(), tk.StringVar()
            cb = ttk.Combobox(f, textvariable=vf, state="readonly", width=26)
            cb.grid(row=4 + i, column=1, columnspan=2, sticky="w", pady=(8 if i == 0 else 2, 2))
            ttk.Entry(f, textvariable=vg, width=8).grid(row=4 + i, column=3, sticky="w", padx=6, pady=(8 if i == 0 else 2, 2))
            self.fil_rows.append((cb, vf, vg))
        ttk.Label(f, text="Gramm", style="CardSub.TLabel").grid(row=3, column=3, sticky="sw", padx=6)
        r0 = 4 + self.ROWS
        row(r0, "Strom gemessen", "kwh", 8, "kWh – leer = geschätzt")
        row(r0 + 1, "Arbeitszeit", "labor", 8, "Minuten (Vorbereiten, Entstützen …)")
        row(r0 + 2, "Sonstiges", "extras", 8, "€ (Magnete, Schrauben, Einsätze …)")
        row(r0 + 3, "Gewinn", "margin", 8, "% – leer = Standard")

        # ---- Ergebnis
        rf = ttk.Frame(body, style="Card.TFrame", padding=14)
        rf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(rf, text="VERKAUFSPREIS", style="CardTitle.TLabel").pack(anchor="w")
        self.l_price = tk.Label(rf, text="–", bg=CARD, fg=FG, font=("Segoe UI Semibold", 26), anchor="w")
        self.l_price.pack(anchor="w")
        self.l_each = ttk.Label(rf, text="", style="CardSub.TLabel")
        self.l_each.pack(anchor="w")
        self.t = tk.Text(rf, height=17, bg=CARD, fg=FG, relief="flat", font=("Segoe UI", 10), wrap="word",
                         highlightthickness=0)
        self.t.pack(fill="both", expand=True, pady=(8, 6))
        self.t.tag_configure("muted", foreground=MUTED)
        self.t.tag_configure("bold", font=("Segoe UI Semibold", 10))
        self.t.tag_configure("good", foreground=OK, font=("Segoe UI Semibold", 10))
        bb = ttk.Frame(rf, style="Card.TFrame")
        bb.pack(fill="x")
        ttk.Button(bb, text="Als Text kopieren", command=self.copy).pack(side="left")
        ttk.Button(bb, text="Kalkulation speichern", style="Accent.TButton", command=self.save).pack(side="left", padx=6)

        # ---- gespeicherte Kalkulationen
        lf = ttk.Frame(body)
        lf.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        top = ttk.Frame(lf)
        top.pack(fill="x")
        ttk.Label(top, text="Gespeicherte Kalkulationen", style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="Löschen", command=self.delete).pack(side="right")
        ttk.Button(top, text="Laden", command=self.load).pack(side="right", padx=6)
        self.tree = ttk.Treeview(lf, columns=("date", "name", "qty", "cost", "price"), show="headings", height=6)
        for c, t, w, a in (("date", "Datum", 120, "w"), ("name", "Bezeichnung", 320, "w"), ("qty", "Stück", 60, "e"),
                           ("cost", "Selbstkosten", 110, "e"), ("price", "Verkaufspreis", 120, "e")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, pady=(4, 0))
        self.tree.bind("<Double-1>", lambda e: self.load())
        for var in list(self.v.values()) + [x for _, vf, vg in self.fil_rows for x in (vf, vg)]:
            var.trace_add("write", lambda *a: self._schedule())
        self.last = None
        self.refresh()

    # ------------------------------------------------------------ #
    def _fil_names(self):
        return ["– kein –"] + [f["name"] for f in self.app.filaments()]

    def refresh(self):
        names = self._fil_names()
        for i, (cb, vf, vg) in enumerate(self.fil_rows):
            cb.configure(values=names)
            if vf.get() not in names:
                vf.set(names[1] if i == 0 and len(names) > 1 else names[0])
        self.tree.delete(*self.tree.get_children())
        for cid, ts, name, d in self.app.store.calcs():
            r = d.get("result", {})
            self.tree.insert("", "end", iid=str(cid), values=(
                time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)), name, r.get("qty", 1),
                self.P.eur(r.get("cost", 0)), self.P.eur(r.get("price", 0))))
        self.recalc()

    def _schedule(self):
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:  # noqa: BLE001
                pass
        self._after = self.after(200, self.recalc)

    def job(self):
        fil = self.app.filaments()
        by_name = {f["name"]: f["id"] for f in fil}
        items = []
        for _, vf, vg in self.fil_rows:
            fid = by_name.get(vf.get())
            try:
                g = float(vg.get().replace(",", ".") or 0)
            except ValueError:
                g = 0
            if fid and g > 0:
                items.append({"filament": fid, "grams": g})
        try:
            hours = parse_hours(self.v["time"].get())
        except ValueError:
            hours = 0.0
        kwh = self.v["kwh"].get().strip().replace(",", ".")
        try:
            kwh = float(kwh) if kwh else None
        except ValueError:
            kwh = None
        return {"name": self.v["name"].get().strip(), "items": items, "hours": hours, "kwh": kwh,
                "qty": self.v["qty"].get() or 1, "labor_min": self.v["labor"].get(), "extras": self.v["extras"].get(),
                "margin": self.v["margin"].get()}

    def recalc(self):
        self._after = None
        job = self.job()
        r = self.app.calculate_job(job)
        self.last = (job, r)
        eur = self.P.eur
        self.l_price.configure(text=eur(r["price"]))
        self.l_each.configure(text=(f"{eur(r['price_each'])} je Stück · " if r["qty"] > 1 else "") +
                              ("ohne MwSt. (Kleinunternehmer)" if r["small_business"] else f"inkl. {self.P.pct(r['vat_pct'])} % MwSt."))
        t = self.t
        t.configure(state="normal")
        t.delete("1.0", "end")

        def line(label, val, tag=None):
            t.insert("end", f"{label}\t{eur(val)}\n", tag or ())
        t.configure(tabs=(330, "right"))
        for ln in r["material_lines"]:
            line("Material " + ln["label"], ln["eur"])
        if not r["material_lines"]:
            t.insert("end", "Noch kein Filament eingetragen\n", "muted")
        line(f"Strom ({fnum_de(r['kwh'], 2)} kWh {'gemessen' if r['kwh_measured'] else 'geschätzt'})", r["power"])
        line("Abschreibung Drucker", r["depreciation"])
        line("Verschleiß (Düse, Platte …)", r["wear"])
        line("Fehldruck-Risiko", r["risk"])
        if r["labor"]:
            line(f"Arbeitszeit ({r['labor_min']:.0f} Min.)", r["labor"])
        if r["extras"]:
            line("Verpackung / Sonstiges", r["extras"])
        t.insert("end", "\n")
        line("Selbstkosten" + (f"  ({eur(r['cost_each'])} je Stück)" if r["qty"] > 1 else ""), r["cost"], "bold")
        line(f"+ Gewinn (Ziel {self.P.pct(r['margin_pct'])} %)", r["profit"])
        line("= Netto", r["net"], "bold")
        if not r["small_business"]:
            line(f"+ MwSt. {self.P.pct(r['vat_pct'])} %", r["vat"])
        if r["fee"]:
            line(f"+ Plattformgebühr {self.P.pct(r['fee_pct'])} %", r["fee"])
        line("Verkaufspreis", r["price"], "bold")
        t.insert("end", "\n")
        line("Bleibt dir nach Kosten, Steuer und Gebühren", r["earn"], "good")
        if not self.app.cost_settings().get("printer_price"):
            t.insert("end", "\nTipp: Unter „⚙ Kosten & Filamente“ den Kaufpreis des Druckers eintragen – dann rechnet "
                            "Hauskern die Abschreibung mit ein.", "muted")
        t.configure(state="disabled")

    def set_job(self, job):
        self.v["name"].set(job.get("name", ""))
        self.v["qty"].set(str(job.get("qty", 1)))
        self.v["time"].set(fmt_hours(job.get("hours", 0)))
        self.v["kwh"].set("" if job.get("kwh") is None else fnum_de(job["kwh"], 3))
        self.v["labor"].set(str(job.get("labor_min") or ""))
        self.v["extras"].set(str(job.get("extras") or ""))
        self.v["margin"].set(str(job.get("margin") or ""))
        fil = self.app.filaments()
        names = self._fil_names()
        items = job.get("items", [])[:self.ROWS]
        for i, (cb, vf, vg) in enumerate(self.fil_rows):
            if i < len(items):
                it = items[i]
                f = self.P.find_filament(fil, it.get("filament", ""), it.get("type", ""), it.get("color", ""))
                vf.set(f["name"] if f["name"] in names else names[0])
                vg.set(fnum_de(it.get("grams", 0), 1))
            else:
                vf.set(names[0])
                vg.set("")
        self.recalc()

    def new(self):
        self.set_job({"items": [{"filament": (self.app.filaments() or [{"id": ""}])[0]["id"], "grams": 0}]})

    def from_3mf(self):
        from tkinter import filedialog
        import bambu_files
        path = filedialog.askopenfilename(parent=self, title="Geslicte .3mf aus Bambu Studio wählen",
                                          filetypes=[("Bambu-Projekt", "*.3mf"), ("Alle Dateien", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                u = bambu_files.parse_3mf(fh.read(), "all")
        except (OSError, bambu_files.FilesError) as e:
            messagebox.showwarning(APP, f"Die Datei enthält keine Slicer-Daten:\n{e}\n\nIn Bambu Studio erst "
                                        "„Slicen“ und dann „Exportieren → Geslictes Projekt (.3mf)“ wählen.", parent=self)
            return
        name = os.path.basename(path).replace(".gcode.3mf", "").replace(".3mf", "")
        self.set_job({"name": name, "items": u["filaments"], "hours": u["seconds"] / 3600, "qty": 1})
        if u.get("plates", 1) > 1:
            messagebox.showinfo(APP, f"Die Datei hat {u['plates']} Druckplatten – alle zusammen sind eingerechnet.",
                                parent=self)

    def from_print(self):
        rows = self.app.store.prints_between(time.time() - 120 * 86400, time.time() + 60)
        if not rows:
            messagebox.showinfo(APP, "Noch keine fertigen Drucke aufgezeichnet.", parent=self)
            return
        d = PickPrintDialog(self, rows)
        self.wait_window(d)
        if d.result is not None:
            self.set_job(self.app.job_from_print(rows[d.result]))

    def settings(self):
        d = CostSettingsDialog(self, self.app)
        self.wait_window(d)
        self.refresh()

    def copy(self):
        if self.last:
            self.clipboard_clear()
            self.clipboard_append(self.P.as_text(*self.last))

    def save(self):
        job, r = self.last
        name = job.get("name") or simpledialog.askstring(APP, "Bezeichnung der Kalkulation:", parent=self)
        if not name:
            return
        job["name"] = name
        self.v["name"].set(name)
        self.app.store.save_calc(name, {"job": job, "result": {k: v for k, v in r.items() if k != "material_lines"}})
        self.refresh()

    def _sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def load(self):
        cid = self._sel()
        d = next((d for i, _, _, d in self.app.store.calcs() if i == cid), None)
        if d:
            self.set_job(d["job"])

    def delete(self):
        cid = self._sel()
        if cid and messagebox.askyesno(APP, "Kalkulation löschen?", parent=self):
            self.app.store.delete_calc(cid)
            self.refresh()


def fnum_de(v, d=1):
    return f"{float(v or 0):.{d}f}".replace(".", ",")


class PickPrintDialog(tk.Toplevel):
    def __init__(self, master, rows):
        super().__init__(master)
        self.title("Druck übernehmen")
        self.configure(bg=BG)
        self.transient(master)
        self.result = None
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Welchen Druck willst du kalkulieren?", style="H1.TLabel").pack(anchor="w")
        self.lb = tk.Listbox(f, height=14, width=80, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.lb.pack(fill="both", expand=True, pady=8)
        for r in rows:
            dur = max(0, (r[3] or r[2]) - r[2]) / 3600
            g = f" · {fnum_de(r[8], 0)} g" if r[8] else " · Gewicht unbekannt"
            self.lb.insert("end", f"{time.strftime('%d.%m. %H:%M', time.localtime(r[2]))}  ·  {r[1] or 'Druck'}  ·  "
                                  f"{fmt_hours(dur)} h{g}  ·  {r[6]}")
        self.lb.bind("<Double-1>", lambda e: self.ok())
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Übernehmen", style="Accent.TButton", command=self.ok).pack(side="right")
        self.grab_set()
        _center(self, master)

    def ok(self):
        s = self.lb.curselection()
        if s:
            self.result = s[0]
            self.destroy()


class CostSettingsDialog(tk.Toplevel):
    FIELDS = [("printer_price", "Kaufpreis des Druckers", "€"), ("lifetime_h", "Erwartete Lebensdauer", "Druckstunden"),
              ("wear_h", "Verschleißteile", "€ pro Druckstunde (Düse, Platte, Riemen …)"),
              ("avg_watt", "Verbrauch ohne Messsteckdose", "Watt im Schnitt"),
              ("fail_pct", "Fehldruck-Risiko", "% Aufschlag"), ("waste_pct", "Verschnitt", "% (Spülen, Spulenreste)"),
              ("hourly_rate", "Dein Stundenlohn", "€ für Arbeitszeit"), ("labor_min", "Standard-Arbeitszeit", "Min. pro Auftrag"),
              ("packaging", "Verpackung", "€ pro Auftrag"), ("margin_pct", "Gewinnaufschlag", "%"),
              ("vat_pct", "MwSt.", "%"), ("fee_pct", "Plattformgebühr", "% (z. B. Etsy/eBay, 0 = keine)"),
              ("round_to", "Preis aufrunden auf", "€ (z. B. 0,10 / 0,50 – 0 = nicht runden)")]

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Druckkosten – Einstellungen")
        self.configure(bg=BG)
        self.geometry("820x760")
        self.transient(master)
        s = app.cost_settings()
        self.fil = [clean_color(x) for x in app.filaments()]
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Kosten & Filamente", style="H1.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text=f"Strompreis: {fnum_de(app.cfg.get('price_ct', 35), 1)} ct/kWh (unter Einstellungen). Gemessen wird "
                          "über die Steckdose des Druckers, sonst mit dem Durchschnittsverbrauch geschätzt.",
                  style="Muted.TLabel", wraplength=760, justify="left").grid(row=1, column=0, columnspan=3, sticky="w",
                                                                             pady=(2, 8))
        self.v = {}
        for i, (k, lbl, unit) in enumerate(self.FIELDS, start=2):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            self.v[k] = tk.StringVar(value=fnum_de(s[k], 2).rstrip("0").rstrip(",") if s[k] else "0")
            ttk.Entry(f, textvariable=self.v[k], width=10).grid(row=i, column=1, sticky="w", pady=2)
            ttk.Label(f, text=unit, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=8)
        r = 2 + len(self.FIELDS)
        self.v_small = tk.BooleanVar(value=bool(s.get("small_business")))
        ttk.Checkbutton(f, text="Kleinunternehmer (§ 19 UStG) – keine MwSt. berechnen", variable=self.v_small).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Kalkulationshilfe, keine Steuerberatung – ob du MwSt. ausweisen musst, klärst du am besten mit "
                          "dem Finanzamt bzw. einem Steuerberater.", style="Muted.TLabel", wraplength=760,
                  justify="left").grid(row=r + 1, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Filamente", style="H1.TLabel").grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=(12, 2))
        tf = ttk.Frame(f)
        tf.grid(row=r + 3, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(r + 3, weight=1)
        f.columnconfigure(2, weight=1)
        self.tree = ttk.Treeview(tf, columns=("name", "type", "color", "price", "spool"), show="headings", height=7)
        for c, t, w in (("name", "Name", 220), ("type", "Typ", 90), ("color", "Farbe", 90), ("price", "€ pro Rolle", 100),
                        ("spool", "Rolle", 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_fil())
        fb = ttk.Frame(tf, padding=(8, 0, 0, 0))
        fb.pack(side="right", fill="y")
        for txt, cmd in (("+ Filament", self.add_fil), ("Bearbeiten", self.edit_fil), ("Entfernen", self.del_fil)):
            ttk.Button(fb, text=txt, command=cmd).pack(fill="x", pady=2)
        ttk.Label(f, text="Tipp: Typ wie in Bambu Studio (PLA, PETG, PLA-CF …). Hast du eine Farbe eingetragen, nimmt Hauskern "
                          "bei automatisch erkannten Drucken genau diese Rolle, sonst die erste passende des Typs.",
                  style="Muted.TLabel", wraplength=760, justify="left").grid(row=r + 4, column=0, columnspan=3, sticky="w",
                                                                             pady=(4, 0))
        b = ttk.Frame(f)
        b.grid(row=r + 5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self.ok).pack(side="right")
        self.render()
        self.grab_set()
        _center(self, master)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        for f in self.fil:
            per_roll = f["price_kg"] * f.get("spool_g", 1000) / 1000
            f = clean_color(f)
            col = " ".join(x for x in (f.get("color_name"), f.get("color")) if x) or "alle"
            self.tree.insert("", "end", iid=f["id"], values=(f["name"], f.get("type", ""), col,
                                                            f"{fnum_de(per_roll, 2)} €", f"{int(f.get('spool_g', 1000))} g"))

    def _sel(self):
        s = self.tree.selection()
        return next((f for f in self.fil if s and f["id"] == s[0]), None)

    def _edit(self, cur=None):
        d = FilamentDialog(self, cur, self.app.ams_trays())
        self.wait_window(d)
        if d.result:
            self.fil = [f for f in self.fil if f["id"] != d.result["id"]] + [d.result]
            self.render()

    def add_fil(self):
        self._edit()

    def edit_fil(self):
        f = self._sel()
        if f:
            self._edit(f)

    def del_fil(self):
        f = self._sel()
        if f and len(self.fil) > 1:
            self.fil = [x for x in self.fil if x["id"] != f["id"]]
            self.render()

    def ok(self):
        out = {}
        for k, _, _ in self.FIELDS:
            try:
                out[k] = float(self.v[k].get().replace(",", ".") or 0)
            except ValueError:
                messagebox.showwarning(APP, f"Bitte eine Zahl eingeben: {dict((a, b) for a, b, _ in self.FIELDS)[k]}",
                                       parent=self)
                return
        out["small_business"] = self.v_small.get()
        self.app.save_cost_settings(out, self.fil)
        self.destroy()


HEX_RX = __import__("re").compile(r"^#?[0-9a-fA-F]{6}$")


def clean_color(f):
    """Ältere Einträge: Farbnamen, die versehentlich als Code gespeichert wurden (#kakaobraun) reparieren."""
    c = (f.get("color") or "").strip()
    if c and not HEX_RX.match(c):
        f["color_name"] = f.get("color_name") or c.lstrip("#").capitalize()
        f["color"] = ""
    elif c and not c.startswith("#"):
        f["color"] = "#" + c.lower()
    return f


class FilamentDialog(tk.Toplevel):
    def __init__(self, master, cur=None, trays=None):
        super().__init__(master)
        cur = clean_color(dict(cur or {}))
        self.cur, self.result, self.trays = cur, None, trays or []
        self.title("Filament")
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        spool = cur.get("spool_g", 1000)
        per_roll = cur.get("price_kg", 20.0) * spool / 1000
        self.hex = cur.get("color", "")
        self.v = {k: tk.StringVar(value=v) for k, v in (
            ("name", cur.get("name", "")), ("type", cur.get("type", "PLA")), ("cname", cur.get("color_name", "")),
            ("price", fnum_de(per_roll, 2)), ("spool", str(int(spool))))}
        if self.trays:
            ttk.Button(f, text="🧵 Aus AMS übernehmen …", style="Accent.TButton", command=self.from_ams).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        rows = (("name", "Name", "z. B. Bambu PLA Basic Kakaobraun"), ("type", "Typ", "PLA, PETG, ABS, ASA, TPU, PLA-CF …"))
        for i, (k, lbl, hint) in enumerate(rows, start=1):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            ttk.Entry(f, textvariable=self.v[k], width=28 if k == "name" else 12).grid(row=i, column=1, sticky="w", pady=3)
            ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=8)
        ttk.Label(f, text="Farbe (optional)").grid(row=3, column=0, sticky="w", pady=3, padx=(0, 10))
        cf = ttk.Frame(f)
        cf.grid(row=3, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Entry(cf, textvariable=self.v["cname"], width=16).pack(side="left")
        self.sw = tk.Canvas(cf, width=26, height=26, bg=BG, highlightthickness=0)
        self.sw.pack(side="left", padx=6)
        ttk.Button(cf, text="Farbe wählen …", command=self.pick_color).pack(side="left")
        ttk.Button(cf, text="keine", command=lambda: self._set_hex("")).pack(side="left", padx=(4, 0))
        self.l_hex = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_hex.grid(row=4, column=1, columnspan=2, sticky="w")
        for i, (k, lbl, hint) in enumerate((("price", "Preis pro Rolle", "€"), ("spool", "Rollengewicht", "g (meist 1000)")),
                                           start=5):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            ttk.Entry(f, textvariable=self.v[k], width=12).grid(row=i, column=1, sticky="w", pady=3)
            ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=8)
        b = ttk.Frame(f)
        b.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Übernehmen", style="Accent.TButton", command=self.ok).pack(side="right")
        self._set_hex(self.hex)
        self.grab_set()
        _center(self, master)

    def _set_hex(self, h):
        self.hex = (h or "").lower()
        self.sw.delete("all")
        if self.hex:
            self.sw.create_oval(2, 2, 24, 24, fill=self.hex, outline="#6a6f7b")
            self.l_hex.configure(text=f"Farbcode {self.hex} – Hauskern erkennt die Rolle bei Drucken an dieser Farbe")
        else:
            self.sw.create_oval(2, 2, 24, 24, fill="", outline=MUTED, dash=(2, 2))
            self.l_hex.configure(text="Ohne Farbcode gilt der Preis für alle Farben dieses Typs")

    def pick_color(self):
        from tkinter import colorchooser
        rgb, hx = colorchooser.askcolor(color=self.hex or "#6b4423", parent=self, title="Filamentfarbe")
        if hx:
            self._set_hex(hx)

    def from_ams(self):
        d = tk.Toplevel(self)
        d.title("Aus AMS übernehmen")
        d.configure(bg=BG)
        d.transient(self)
        ttk.Label(d, text="Welche Rolle?", style="H1.TLabel").pack(anchor="w", padx=14, pady=(12, 4))
        lb = tk.Listbox(d, height=min(12, len(self.trays)), width=60, bg=CARD, fg=FG, selectbackground=ACCENT,
                        borderwidth=0, highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        lb.pack(fill="both", expand=True, padx=14)
        for t in self.trays:
            rest = f" · noch {t['remain']} %" if t.get("remain") is not None else ""
            lb.insert("end", f"{t['unit']} · Fach {t['slot']}:  {t['brand'] or t['type']}  {t['color'] or ''}{rest}")

        def ok():
            sel = lb.curselection()
            if not sel:
                return
            t = self.trays[sel[0]]
            self.v["type"].set(t["type"])
            if t.get("color"):
                self._set_hex(t["color"])
            if not self.v["name"].get().strip():
                self.v["name"].set(" ".join(x for x in ("Bambu" if t.get("rfid") else "", t["brand"] or t["type"],
                                                         self.v["cname"].get().strip()) if x))
            if t.get("weight"):
                self.v["spool"].set(str(int(t["weight"])))
            d.destroy()
        lb.bind("<Double-1>", lambda e: ok())
        ttk.Button(d, text="Übernehmen", style="Accent.TButton", command=ok).pack(anchor="e", padx=14, pady=10)
        _center(d, self)

    def ok(self):
        try:
            price = float(self.v["price"].get().replace(",", "."))
            spool = float(self.v["spool"].get().replace(",", ".") or 1000)
        except ValueError:
            messagebox.showwarning(APP, "Preis und Rollengewicht bitte als Zahl eingeben.", parent=self)
            return
        name = self.v["name"].get().strip() or self.v["type"].get().strip() or "Filament"
        self.result = {"id": self.cur.get("id") or f"f{int(time.time() * 1000) % 10**9}", "name": name,
                       "type": self.v["type"].get().strip().upper(), "color": self.hex,
                       "color_name": self.v["cname"].get().strip(),
                       "price_kg": round(price / max(1.0, spool) * 1000, 4), "spool_g": spool}
        self.destroy()


# =========================================================================== #
# Updates
# =========================================================================== #
class UpdateDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import updater
        self.U, self.app = updater, app
        self.title("Updates")
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Updates", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"Installiert: Hauskern {app.version}", font=("Segoe UI Semibold", 11)).pack(anchor="w",
                                                                                                    pady=(4, 2))
        ttk.Label(f, text="Neue Versionen bekommst du als Hauskern.zip. Einfach in den Download-Ordner legen – beim "
                          "nächsten Start fragt Hauskern,\nob es sie installieren soll. Einstellungen, Verlauf und ffmpeg "
                          "bleiben dabei erhalten, die alte Version wird gesichert.", style="Muted.TLabel",
                  justify="left").pack(anchor="w")
        b = ttk.Frame(f)
        b.pack(anchor="w", pady=(10, 4))
        ttk.Button(b, text="Im Download-Ordner suchen", style="Accent.TButton",
                   command=lambda: app.check_downloads(interactive=True)).pack(side="left")
        ttk.Button(b, text="Update-Datei wählen …", command=self.pick).pack(side="left", padx=6)
        self.v_scan = tk.BooleanVar(value=app.cfg.get("update_scan", True))
        ttk.Checkbutton(f, text="Beim Start automatisch nach neuen Versionen suchen", variable=self.v_scan,
                        command=self._scan).pack(anchor="w", pady=(4, 10))

        ttk.Label(f, text="Zurück zu einer früheren Version", style="H1.TLabel").pack(anchor="w", pady=(6, 2))
        self.lb = tk.Listbox(f, height=4, width=64, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.lb.pack(anchor="w", fill="x")
        self.backups = self.U.list_backups(app.backup_dir)
        for p, v, ts in self.backups:
            self.lb.insert("end", f"Version {v}  ·  gesichert am {time.strftime('%d.%m.%Y %H:%M', time.localtime(ts))}")
        if not self.backups:
            self.lb.insert("end", "Noch keine Sicherung – entsteht beim ersten Update")
        ttk.Button(f, text="Diese Version wiederherstellen", command=self.restore).pack(anchor="w", pady=(6, 10))

        ttk.Label(f, text="Heimserver (Pi/Mini-PC)", style="H1.TLabel").pack(anchor="w", pady=(6, 2))
        self.l_pi = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.l_pi.pack(anchor="w")
        self.b_pi = ttk.Button(f, text="Heimserver jetzt auf diesen Stand bringen", command=lambda: self.pi(True))
        if app.cfg.get("pi_host"):
            self.b_pi.pack(anchor="w", pady=(6, 0))
            self.l_pi.configure(text=f"{app.cfg['pi_host']} – frage Version ab …")
            self.pi(False)
        else:
            self.l_pi.configure(text="Noch kein Heimserver eingerichtet. Sobald er läuft, bekommt er jedes Update automatisch vom PC.")
        ttk.Button(f, text="Schließen", command=self.destroy).pack(anchor="e", pady=(14, 0))
        _center(self, master)

    def _scan(self):
        self.app.cfg["update_scan"] = self.v_scan.get()
        self.app.save()

    def pick(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self, title="Hauskern-Update wählen",
                                       filetypes=[("Hauskern-Paket", "*.zip")])
        if p:
            self.app.install_update_file(p)

    def restore(self):
        s = self.lb.curselection()
        if not s or not self.backups:
            return
        p, v, _ = self.backups[s[0]]
        if messagebox.askyesno(APP, f"Zurück zu Version {v}?\nDie jetzige Version {self.app.version} wird vorher ebenfalls "
                                    "gesichert.", parent=self):
            self.app.install_update_file(p, allow_older=True)

    def pi(self, force):
        self.l_pi.configure(text=f"{self.app.cfg['pi_host']} – {'übertrage …' if force else 'frage Version ab …'}")

        def done(ok, msg, pv):
            ui_later(self, lambda: self.l_pi.configure(text=f"{self.app.cfg['pi_host']}: {msg}",
                                                       foreground=OK if ok else BAD))
        self.app.pi_sync(force=force, done=done)


# =========================================================================== #
# Gerät aus dem WLAN/LAN hinzufügen – mit Erkennung und verbundenen Geräten
# =========================================================================== #
DEVICE_CATEGORIES = ["TV-Box", "Fernseher", "Lautsprecher", "Rauchmelder", "Rauchmelder-Basisstation",
                     "Smart-Home-Zentrale", "Zigbee-Gateway", "Kamera", "Kamera-Basisstation", "Saugroboter",
                     "Spielkonsole", "Computer/NAS", "Drucker (Papier)", "Steckdose/Licht", "Smartwatch", "Auto",
                     "Handy/Tablet", "Router/Repeater", "Sonstiges"]
USE_MODES = [("device", "Gerät – eigene Kachel mit Online-Status (und den verbundenen Geräten)"),
             ("item", "Gegenstand – für die Anwesenheit (z. B. „Auto ist da“, „Uhr ist da“)"),
             ("person", "Person – Handy einer Person für die Anwesenheit")]


def default_mode(desc):
    if desc.get("role") == "person":
        return "person"
    if desc.get("category") in ("Smartwatch", "Auto"):
        return "item"
    return "device"


class AddDeviceDialog(tk.Toplevel):
    def __init__(self, master, app, target, allh, cur=None):
        super().__init__(master)
        import discover
        self.app, self.t, self.cur, self.result = app, target, cur or {}, None
        self.title("Gerät bearbeiten" if cur else "Zu Hauskern hinzufügen")
        self.configure(bg=BG)
        self.geometry("700x720")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=self.title(), style="H1.TLabel").pack(anchor="w")

        # --- Was ist das? ---
        card = ttk.Frame(f, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(8, 8))
        cat = target.get("category") or "nicht sicher erkannt"
        head = f"🔎 Erkannt: {cat}" + (f" · {target['vendor']}" if target.get("vendor") else "")
        ttk.Label(card, text=head, style="CardSub.TLabel", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 2))
        lines = [f"{target.get('name')}  ·  {target.get('ip') or '–'}  ·  {target.get('mac')}"]
        if target.get("hint"):
            lines.append(target["hint"])
        lines += [f"Hinweis: {w}" for w in target.get("why", [])]
        use = [] if cur else app.device_usage(target.get("mac"))
        if use:
            lines.append("Schon in Hauskern: " + ", ".join(use))
        ttk.Label(card, text="\n".join(lines), style="CardSub.TLabel", justify="left", wraplength=620).pack(anchor="w")

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.v_name = tk.StringVar(value=self.cur.get("name") or self._nice_name(target))
        ttk.Entry(top, textvariable=self.v_name, width=34).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(top, text="Art").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        self.v_cat = tk.StringVar(value=self.cur.get("category") or (target.get("category") or "Sonstiges"))
        ttk.Combobox(top, textvariable=self.v_cat, values=DEVICE_CATEGORIES, width=32).grid(row=1, column=1, sticky="w",
                                                                                          pady=3)
        self.v_mode = tk.StringVar(value="device" if cur else default_mode(target))
        if not cur:
            ttk.Label(f, text="Wie soll Hauskern es nutzen?", font=("Segoe UI Semibold", 10)).pack(anchor="w",
                                                                                                  pady=(10, 2))
            for val, txt in USE_MODES:
                ttk.Radiobutton(f, text=txt + ("   ← vorgeschlagen" if val == default_mode(target) else ""),
                                value=val, variable=self.v_mode, command=self._mode).pack(anchor="w")

        # --- Verbundene Geräte im Netz ---
        self.l_rel = ttk.Label(f, text="", font=("Segoe UI Semibold", 10))
        self.l_rel.pack(anchor="w", pady=(12, 0))
        self.l_rel_sub = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.l_rel_sub.pack(anchor="w")
        self.lb = tk.Listbox(f, selectmode="multiple", bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0,
                             highlightthickness=0, font=("Segoe UI", 10), activestyle="none", height=8)
        self.lb.pack(fill="both", expand=True, pady=(4, 0))
        tmac = (target.get("mac") or "").upper()
        others = [h for h in allh if h["mac"] != tmac]
        sugg = discover.related(dict(target, category=target.get("category", "")), others)
        chosen = {m.upper() for m in self.cur.get("children", [])}
        self.rows = []  # (mac, name) je Listbox-Zeile, None = Trenner
        sugg_macs = set()
        for o, why in sugg:
            sugg_macs.add(o["mac"])
            self._row(o, f"   ← {why}", select=(o["mac"] in chosen) if cur else True)
        rest = [o for o in others if o["mac"] not in sugg_macs]
        rest.sort(key=lambda o: (o["mac"] not in chosen, not o["active"], o["name"].lower()))
        if rest:
            self.lb.insert("end", "──────── weitere Geräte im Netz ────────")
            self.lb.itemconfigure("end", foreground=MUTED)
            self.rows.append(None)
            for o in rest:
                self._row(o, "", select=o["mac"] in chosen)
        self.n_sugg = len(sugg)

        # --- per Funk (nicht im WLAN) ---
        self.radio_f = ttk.Frame(f)
        ttk.Label(self.radio_f, text="Per Funk an diesem Gerät (nicht im WLAN sichtbar)",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(10, 0))
        ttk.Label(self.radio_f, text=("Die Melder/Sensoren hinter einer Station funken direkt mit ihr – Hauskern sieht "
                                      "sie nicht im Netz.\nTrag sie hier für die Übersicht ein, durch Komma getrennt "
                                      "(z. B. Flur, Küche, Schlafzimmer)." if target.get("hub") or self.cur.get("radio")
                                      else "Optional – nur falls an diesem Gerät etwas per Funk hängt, durch Komma "
                                           "getrennt."), style="Muted.TLabel", justify="left").pack(anchor="w")
        self.v_radio = tk.StringVar(value=", ".join(self.cur.get("radio", [])))
        ttk.Entry(self.radio_f, textvariable=self.v_radio, width=70).pack(anchor="w", pady=(4, 0))
        self.v_wol = tk.BooleanVar(value=False)
        self.wol_cb = ttk.Checkbutton(f, text="Zusätzlich Aufweck-Kachel (Wake-on-LAN)", variable=self.v_wol)
        if (target.get("category") or "").startswith("Computer") and not cur:
            self.wol_cb.pack(anchor="w", pady=(8, 0))

        b = ttk.Frame(f)
        b.pack(fill="x", side="bottom", pady=(12, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern" if cur else "Hinzufügen", style="Accent.TButton",
                   command=self._ok).pack(side="right")
        self._mode()
        self.grab_set()
        _center(self, master)

    @staticmethod
    def _nice_name(t):
        n = t.get("name") or ""
        if not n or n == t.get("mac") or re.fullmatch(r"[0-9A-Fa-f:.\-]+", n):
            return "" if t.get("role") == "person" else (t.get("category") or "Gerät")
        return n

    def _row(self, o, extra, select):
        dot = "●" if o["active"] else "○"
        cat = f"  ·  {o['category']}" if o.get("category") and o["category"] != "Unbekannt" else ""
        self.lb.insert("end", f"{dot}  {o['name']}{cat}{extra}")
        self.rows.append((o["mac"], o["name"]))
        if select:
            self.lb.selection_set("end")

    def _mode(self):
        m = self.v_mode.get()
        if m == "device":
            self.l_rel.configure(text="Verbundene Geräte im Netz")
            self.l_rel_sub.configure(text=("Hauskern hat passende Geräte gefunden und vorausgewählt – auf der Kachel "
                                           "siehst du, ob sie online sind." if self.n_sugg else
                                           "Nichts Passendes gefunden. Gehört noch etwas dazu (z. B. eine Zweitbox)? "
                                           "Dann einfach unten auswählen."))
            self.radio_f.pack(anchor="w", fill="x")
        else:
            self.l_rel.configure(text="Gehören weitere Geräte dazu?")
            self.l_rel_sub.configure(text="Z. B. Handy + Uhr einer Person – dann gilt sie als da, sobald eins davon im "
                                          "WLAN ist.")
            self.radio_f.pack_forget()

    def _ok(self):
        name = self.v_name.get().strip()
        if not name:
            messagebox.showwarning(APP, "Bitte einen Namen eingeben.", parent=self)
            return
        sel = [self.rows[i] for i in self.lb.curselection() if self.rows[i]]
        mode = self.v_mode.get()
        tmac = (self.t.get("mac") or "").upper()
        if mode == "device":
            radio = [x.strip() for x in re.split(r"[,;\n]", self.v_radio.get()) if x.strip()]
            self.app.save_device({"id": self.cur.get("id") or f"d{int(time.time() * 1000) % 10**9}", "name": name,
                                  "category": self.v_cat.get().strip(), "mac": tmac,
                                  "children": [m for m, _ in sel], "child_names": {m: n for m, n in sel},
                                  "radio": radio})
            msg = f"„{name}“ hat jetzt eine eigene Kachel unter Zuhause → Geräte 🙂"
        else:
            if not tmac:
                messagebox.showwarning(APP, "Für die Anwesenheit braucht das Gerät eine MAC-Adresse.", parent=self)
                return
            self.app.save_person({"id": f"p{int(time.time() * 1000) % 10**9}", "name": name,
                                  "macs": [tmac] + [m for m, _ in sel if m != tmac], "delay": 10, "kind": mode})
            msg = (f"„{name}“ ist jetzt als {'Person' if mode == 'person' else 'Gegenstand'} eingerichtet – siehst du "
                   "unter Zuhause → Personen 🙂")
        if self.v_wol.get() and tmac:
            self.app.add_wol(tmac, name)
        self.result = mode
        self.destroy()
        if not self.cur:
            messagebox.showinfo(APP, msg, parent=self.master)


# =========================================================================== #
# Gast-WLAN
# =========================================================================== #
GUEST_HOURS = [("Nicht automatisch ausschalten", 0), ("Nach 1 Stunde aus", 1), ("Nach 3 Stunden aus", 3),
               ("Nach 6 Stunden aus", 6), ("Nach 12 Stunden aus", 12), ("Nach 24 Stunden aus", 24),
               ("Nach 3 Tagen aus", 72)]


def draw_qr(canvas, matrix, size, x0=0, y0=0):
    n = len(matrix) + 8
    s = size / n
    canvas.create_rectangle(x0, y0, x0 + size, y0 + size, fill="#ffffff", outline="")
    for y, row in enumerate(matrix):
        for x, c in enumerate(row):
            if c:
                canvas.create_rectangle(x0 + (x + 4) * s, y0 + (y + 4) * s, x0 + (x + 5) * s, y0 + (y + 5) * s,
                                        fill="#000000", outline="")


class GuestDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Gast-WLAN")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        left = ttk.Frame(f)
        left.pack(side="left", fill="y", anchor="n")
        ttk.Label(left, text="Gast-WLAN", style="H1.TLabel").pack(anchor="w")
        self.l_state = ttk.Label(left, text="", font=("Segoe UI Semibold", 12))
        self.l_state.pack(anchor="w", pady=(6, 0))
        self.l_sub = ttk.Label(left, text="", style="Muted.TLabel", justify="left")
        self.l_sub.pack(anchor="w")
        self.b_toggle = ttk.Button(left, text="", style="Accent.TButton", command=self.toggle)
        self.b_toggle.pack(anchor="w", pady=(10, 4))
        cur = (app.cfg.get("guest") or {}).get("hours", 0)
        self.v_hours = tk.StringVar(value=next((t for t, h in GUEST_HOURS if h == cur), GUEST_HOURS[0][0]))
        ttk.Combobox(left, textvariable=self.v_hours, values=[t for t, _ in GUEST_HOURS], state="readonly",
                     width=30).pack(anchor="w")
        ttk.Label(left, text="Gilt beim nächsten Einschalten – auch über das Handy.", style="Muted.TLabel").pack(
            anchor="w", pady=(2, 12))
        ttk.Label(left, text="WLAN-Name", style="Muted.TLabel").pack(anchor="w")
        self.l_ssid = ttk.Label(left, text="…", font=("Segoe UI Semibold", 11))
        self.l_ssid.pack(anchor="w")
        ttk.Label(left, text="Passwort", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        self.show_pw = False
        self.l_pw = ttk.Label(left, text="…", font=("Consolas", 11), cursor="hand2")
        self.l_pw.pack(anchor="w")
        self.l_pw.bind("<Button-1>", lambda e: self._flip_pw())
        bb = ttk.Frame(left)
        bb.pack(anchor="w", pady=(12, 0))
        ttk.Button(bb, text="🖨 Druckversion", command=app.guest_print).pack(side="left")
        ttk.Button(bb, text="Neues Passwort …", command=self.new_pw).pack(side="left", padx=6)
        # Welches WLAN?
        main = [w for w in app.data.get("wlan", []) if w.get("box", "main") == "main"]
        self.choices = [("Automatisch erkennen", 0)] + [(f"{w['ssid']} ({w['band']}, Nr. {w['index']})", w["index"])
                                                        for w in main]
        ci = int((app.cfg.get("guest") or {}).get("index") or 0)
        self.v_which = tk.StringVar(value=next((t for t, i in self.choices if i == ci), self.choices[0][0]))
        ttk.Label(left, text="Welches WLAN ist das Gast-WLAN?", style="Muted.TLabel").pack(anchor="w", pady=(16, 0))
        cb = ttk.Combobox(left, textvariable=self.v_which, values=[t for t, _ in self.choices], state="readonly",
                          width=30)
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self._which())
        right = ttk.Frame(f, style="Card.TFrame", padding=12)
        right.pack(side="left", padx=(18, 0), anchor="n")
        self.qr = tk.Canvas(right, width=260, height=260, bg="#ffffff", highlightthickness=0)
        self.qr.pack()
        ttk.Label(right, text="Mit der Handykamera scannen\nund „Verbinden“ tippen", style="CardSub.TLabel",
                  justify="center").pack(pady=(8, 0))
        ttk.Button(f, text="Schließen", command=self.destroy).place(relx=1.0, rely=1.0, anchor="se")
        self.refresh()
        _center(self, master)

    def _hours(self):
        return next((h for t, h in GUEST_HOURS if t == self.v_hours.get()), 0)

    def refresh(self):
        a = self.app
        w, g = a.guest_wlan(), a.guest or {}
        on = bool(w and w["enabled"])
        off = (a.cfg.get("guest") or {}).get("off_at", 0)
        self.l_state.configure(text="● eingeschaltet" if on else "○ ausgeschaltet", foreground=AMBER if on else MUTED)
        sub = []
        if on and w.get("clients") is not None:
            sub.append(f"{w['clients']} Gerät(e) verbunden")
        if on and off:
            sub.append(f"schaltet um {time.strftime('%H:%M', time.localtime(off))} Uhr ab"
                       + ("" if time.localtime(off).tm_yday == time.localtime().tm_yday
                          else f" ({time.strftime('%d.%m.', time.localtime(off))})"))
        if g.get("error"):
            sub.append("⚠ " + g["error"][:80])
        self.l_sub.configure(text="\n".join(sub))
        self.b_toggle.configure(text="Ausschalten" if on else "Einschalten")
        self.l_ssid.configure(text=g.get("ssid") or (w or {}).get("ssid", "…"))
        pw = g.get("password")
        self.l_pw.configure(text=(pw if self.show_pw else "•" * min(len(pw), 16) + "   (zum Zeigen klicken)")
                            if pw else ("(ohne Passwort)" if g.get("ssid") else "…"))
        self.qr.delete("all")
        t = a.guest_qr_text()
        if t:
            import qr
            draw_qr(self.qr, qr.encode(t), 260)
        else:
            self.qr.create_text(130, 130, text="wird geladen …", fill="#666666", font=("Segoe UI", 10))

    def _flip_pw(self):
        self.show_pw = not self.show_pw
        self.refresh()

    def toggle(self):
        w = self.app.guest_wlan()
        try:
            self.app.set_guest(not (w and w["enabled"]), self._hours(), "PC")
        except RuntimeError as e:
            messagebox.showwarning(APP, str(e), parent=self)
        self.refresh()

    def new_pw(self):
        if not messagebox.askyesno(APP, "Neues Passwort für das Gast-WLAN vergeben?\n\nAlle Gäste, die gerade "
                                        "verbunden sind, müssen sich danach neu verbinden (einfach den neuen QR-Code "
                                        "scannen).", parent=self):
            return
        try:
            pw = self.app.new_guest_password()
        except RuntimeError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        self.show_pw = True
        self.refresh()
        messagebox.showinfo(APP, f"Neues Passwort: {pw}", parent=self)

    def _which(self):
        idx = next((i for t, i in self.choices if t == self.v_which.get()), 0)
        self.app.cfg.setdefault("guest", {})["index"] = idx
        self.app.save()
        self.app.guest = None
        self.app._on_wlan(self.app.data.get("wlan", []), rebuild=False)
        self.app.fetch_guest(force=True)
        self.refresh()


# =========================================================================== #
# Stromkosten – Einstellungen
# =========================================================================== #
class PowerSettingsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import power as P
        self.app, self.result = app, None
        ps = dict(P.DEFAULTS, **(app.cfg.get("power") or {}))
        self.title("Strompreis & Warnungen")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Strompreis & Warnungen", style="H1.TLabel").pack(anchor="w")
        g = ttk.Frame(f)
        g.pack(anchor="w", pady=(10, 0))
        self.v_price = tk.StringVar(value=fnum_de(app.cfg.get("price_ct", 35.0), 2))
        self.v_budget = tk.StringVar(value=fnum_de(ps["budget_eur"], 2) if ps["budget_eur"] else "")
        self.v_sb = tk.StringVar(value=fnum_de(ps["standby_w"], 1))
        for r, (lbl, var, unit) in enumerate((("Arbeitspreis", self.v_price, "ct/kWh  (steht auf der Stromrechnung)"),
                                              ("Monatsbudget", self.v_budget, "€ für alle Messsteckdosen (leer = keins)"),
                                              ("Grundlast ab", self.v_sb, "W anzeigen"))):
            ttk.Label(g, text=lbl).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(g, textvariable=var, width=10).grid(row=r, column=1, sticky="w", pady=3)
            ttk.Label(g, text=unit, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=(6, 0))
        ttk.Label(f, text="Benachrichtigungen (per Push und am PC)", font=("Segoe UI Semibold", 10)).pack(
            anchor="w", pady=(14, 2))
        self.v_report = tk.BooleanVar(value=ps["report"])
        self.v_out = tk.BooleanVar(value=ps["outliers"])
        self.v_bw = tk.BooleanVar(value=ps["budget_warn"])
        ttk.Checkbutton(f, text="Monatsbericht am 1. des Monats (Kosten, Vergleich, größte Verbraucher)",
                        variable=self.v_report).pack(anchor="w")
        ttk.Checkbutton(f, text="Warnen, wenn ein Gerät deutlich mehr verbraucht als sonst (ab dem 7. des Monats)",
                        variable=self.v_out).pack(anchor="w")
        ttk.Checkbutton(f, text="Warnen, wenn die Hochrechnung über dem Monatsbudget liegt", variable=self.v_bw).pack(
            anchor="w")
        ttk.Label(f, text="Gemessen werden nur Geräte an FRITZ!DECT-Messsteckdosen. Die Monatswerte kommen von der "
                          "FRITZ!Box\n(auch aus der Zeit vor Hauskern), der Rest aus Hauskerns eigener Aufzeichnung.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(12, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.grab_set()
        _center(self, master)

    def _ok(self):
        def num(v, default=0.0):
            t = str(v.get()).strip().replace(",", ".")
            return float(t) if t else default
        try:
            price, budget, sb = num(self.v_price, 35.0), num(self.v_budget), num(self.v_sb, 1.0)
        except ValueError:
            messagebox.showwarning(APP, "Bitte nur Zahlen eingeben (z. B. 32,5).", parent=self)
            return
        if not 1 <= price <= 300:
            messagebox.showwarning(APP, "Der Strompreis sieht ungewöhnlich aus – bitte in Cent pro kWh angeben "
                                        "(z. B. 32,5).", parent=self)
            return
        self.result = (price, {"budget_eur": max(0.0, budget), "report": self.v_report.get(),
                               "outliers": self.v_out.get(), "budget_warn": self.v_bw.get(), "standby_w": max(0.1, sb)})
        self.destroy()


# =========================================================================== #
# Handy-Nutzer & Rechte
# =========================================================================== #
class UsersDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import users as U
        self.U, self.app = U, app
        self.title("Handy-Nutzer & Rechte")
        self.configure(bg=BG)
        self.geometry("740x420")
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Handy-Nutzer & Rechte", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Jeder meldet sich am Handy mit seiner eigenen PIN an und sieht nur, was er darf. "
                          "Im Logbuch steht,\nwer was geschaltet hat. Admins dürfen alles – auch die Einstellungen "
                          "an den Heimserver senden.", style="Muted.TLabel", justify="left").pack(anchor="w",
                                                                                                    pady=(2, 8))
        self.tree = ttk.Treeview(f, columns=("name", "rights", "phones"), show="headings", height=8,
                                 selectmode="browse")
        for c, t, w in (("name", "Name", 150), ("rights", "Rechte", 300), ("phones", "Angemeldet", 110)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="+ Nutzer", style="Accent.TButton", command=self.add).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Überall abmelden", command=self.kick).pack(side="left")
        ttk.Button(b, text="2FA zurücksetzen", command=self.reset2fa).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Entfernen", command=self.remove).pack(side="left", padx=6)
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        self.render()
        self.grab_set()
        _center(self, master)

    def _users(self):
        return [dict(u) for u in self.app.cfg.get("web_users") or []]

    def render(self):
        self.tree.delete(*self.tree.get_children())
        for u in self._users():
            n = self.U.count_sessions(self.app.cfg, u)
            self.tree.insert("", "end", iid=u["id"], values=(("👑 " if u.get("admin") else "") + u["name"]
                                                              + (" 🔐" if u.get("totp") else ""),
                                                              self.U.describe(u), f"{n} Handy(s)" if n else "–"))

    def _sel(self):
        s = self.tree.selection()
        return next((u for u in self._users() if s and u["id"] == s[0]), None)

    def _store(self, lst):
        self.app.save_users(lst)
        self.render()

    def add(self):
        d = UserDialog(self, self.app, None)
        self.wait_window(d)
        if d.result:
            first = not self._users()
            if first:
                d.result["admin"] = True  # der erste Nutzer ist immer Admin
            self._store(self._users() + [d.result])

    def edit(self):
        u = self._sel()
        if not u:
            return
        d = UserDialog(self, self.app, u)
        self.wait_window(d)
        if d.result:
            lst = [d.result if x["id"] == u["id"] else x for x in self._users()]
            if not any(x.get("admin") for x in lst):
                messagebox.showwarning(APP, "Mindestens ein Nutzer muss Admin bleiben.", parent=self)
                return
            self._store(lst)

    def kick(self):
        u = self._sel()
        if not u:
            return
        if messagebox.askyesno(APP, f"„{u['name']}“ auf allen Handys abmelden?\nDie PIN bleibt gleich – einfach neu "
                                    "anmelden.", parent=self):
            import secrets
            self._store([dict(x, rev=secrets.token_hex(4)) if x["id"] == u["id"] else x for x in self._users()])

    def reset2fa(self):
        import totp
        u = self._sel()
        if not u:
            return
        if not totp.enabled(u):
            messagebox.showinfo(APP, f"Bei „{u['name']}“ ist Zwei-Faktor gar nicht an.\nEinschalten geht am Handy "
                                     "unter ⚙ → 🔐 Zwei-Faktor-Anmeldung.", parent=self)
            return
        if messagebox.askyesno(APP, f"Zwei-Faktor von „{u['name']}“ ausschalten?\nDann reicht wieder die PIN. "
                                    "Danach am Handy neu einrichten (z. B. wenn das Handy weg ist).", parent=self):
            totp.disable(u)
            self._store([u if x["id"] == u["id"] else x for x in self._users()])
            self.app.log("App", APP, f"Zwei-Faktor von „{u['name']}“ zurückgesetzt")

    def remove(self):
        u = self._sel()
        if not u:
            return
        rest = [x for x in self._users() if x["id"] != u["id"]]
        if rest and not any(x.get("admin") for x in rest):
            messagebox.showwarning(APP, "Das ist der letzte Admin – mach vorher jemand anderen zum Admin.", parent=self)
            return
        msg = f"„{u['name']}“ entfernen? Seine Handys werden abgemeldet."
        if not rest:
            msg += "\n\nDanach ist niemand mehr eingerichtet – die Handy-Oberfläche bleibt aus, bis du neu anlegst."
        if messagebox.askyesno(APP, msg, parent=self):
            self._store(rest)


class UserDialog(tk.Toplevel):
    def __init__(self, master, app, cur):
        super().__init__(master)
        import users as U
        self.U, self.app, self.cur, self.result = U, app, cur or {}, None
        self.title("Nutzer bearbeiten" if cur else "Neuer Handy-Nutzer")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=self.title(), style="H1.TLabel").pack(anchor="w")
        g = ttk.Frame(f)
        g.pack(anchor="w", pady=(10, 0))
        self.v_name = tk.StringVar(value=self.cur.get("name", ""))
        self.v_pin = tk.StringVar()
        ttk.Label(g, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Entry(g, textvariable=self.v_name, width=24).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(g, text="PIN (4–12 Ziffern)").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Entry(g, textvariable=self.v_pin, width=14, show="•").grid(row=1, column=1, sticky="w", pady=3)
        if cur:
            ttk.Label(g, text="leer lassen = PIN bleibt", style="Muted.TLabel").grid(row=1, column=2, sticky="w",
                                                                                   padx=6)
        self.v_admin = tk.BooleanVar(value=bool(self.cur.get("admin")))
        ttk.Checkbutton(f, text="👑 Admin – darf alles (auch künftige Bereiche und Einstellungen an den Heimserver senden)",
                        variable=self.v_admin, command=self._admin).pack(anchor="w", pady=(10, 4))
        pf = ttk.Frame(f)
        pf.pack(anchor="w", pady=(4, 6))
        ttk.Label(pf, text="Vorlage:").pack(side="left")
        self.v_preset = tk.StringVar(value="")
        cb = ttk.Combobox(pf, textvariable=self.v_preset, values=[n for n, _ in U.PRESETS], state="readonly", width=32)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._preset())
        self.grid_f = ttk.Frame(f, style="Card.TFrame", padding=10)
        self.grid_f.pack(fill="x")
        rights = self.cur.get("rights") or dict(U.PRESETS[1][1])
        self.v_r, self.cbs = {}, []
        for i, (k, label, mx, hint) in enumerate(U.AREAS):
            ttk.Label(self.grid_f, text=label, style="CardSub.TLabel", font=("Segoe UI Semibold", 10)).grid(
                row=i, column=0, sticky="w", pady=2)
            vals = [U.LEVEL_TEXT[x] for x in range(mx + 1)]
            v = tk.StringVar(value=U.LEVEL_TEXT[min(int(rights.get(k, 0)), mx)])
            c = ttk.Combobox(self.grid_f, textvariable=v, values=vals, state="readonly", width=10)
            c.grid(row=i, column=1, padx=10, pady=2)
            ttk.Label(self.grid_f, text=hint, style="CardSub.TLabel").grid(row=i, column=2, sticky="w")
            self.v_r[k] = v
            self.cbs.append(c)
        ttk.Label(f, text="„ansehen“ = sieht den Bereich, kann aber nichts schalten. „aus“ = Bereich ist auf seinem "
                          "Handy gar nicht da.", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self._admin()
        self.grab_set()
        _center(self, master)

    def _admin(self):
        st = "disabled" if self.v_admin.get() else "readonly"
        for c in self.cbs:
            c.configure(state=st)
        if self.v_admin.get():
            for k, v in self.v_r.items():
                v.set(self.U.LEVEL_TEXT[self.U.MAX[k]])

    def _preset(self):
        p = dict(self.U.PRESETS).get(self.v_preset.get())
        if p and not self.v_admin.get():
            for k, v in self.v_r.items():
                v.set(self.U.LEVEL_TEXT[p[k]])

    def _ok(self):
        from webui import check_pin, hash_pin
        name = self.v_name.get().strip()
        pin = self.v_pin.get().strip()
        if not name:
            messagebox.showwarning(APP, "Bitte einen Namen eingeben.", parent=self)
            return
        if not self.cur and not pin:
            messagebox.showwarning(APP, "Bitte eine PIN festlegen.", parent=self)
            return
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 12):
            messagebox.showwarning(APP, "Die PIN muss aus 4 bis 12 Ziffern bestehen.", parent=self)
            return
        others = [u for u in self.app.cfg.get("web_users") or [] if u["id"] != self.cur.get("id")]
        if pin and any(check_pin(pin, u.get("pin", "")) for u in others):
            messagebox.showwarning(APP, "Diese PIN benutzt schon jemand anderes – jeder braucht eine eigene, "
                                        "daran erkennt Hauskern, wer sich anmeldet.", parent=self)
            return
        inv = {t: n for n, t in self.U.LEVEL_TEXT.items()}
        rights = {k: inv.get(v.get(), 0) for k, v in self.v_r.items()}
        if self.cur:
            import secrets
            r = dict(self.cur, name=name, admin=self.v_admin.get(), rights=rights)
            if pin:
                r["pin"], r["rev"] = hash_pin(pin), secrets.token_hex(4)  # neue PIN → neu anmelden
        else:
            r = self.U.new_user(name, hash_pin(pin), rights, self.v_admin.get())
            r["rights"] = rights if not r["admin"] else r["rights"]
        self.result = r
        self.destroy()


# =========================================================================== #
# Alexa & Google Home
# =========================================================================== #
class VoiceDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import voice as V
        self.app, self.V = app, V
        self.title("Alexa & Google Home")
        self.configure(bg=BG)
        self.geometry("720x640")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🎙 Alexa & Google Home", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Wähle, was Alexa und Google Home sehen dürfen und wie es dort heißt. Die Verbindung läuft "
                          "lokal über Matter –\ndafür kommt auf den Heimserver ein kleines Zusatzprogramm "
                          "(Matter-Bridge). Nur Freigegebenes ist sichtbar.", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(2, 8))
        v = app.cfg.get("voice") or {}
        self.v_en = tk.BooleanVar(value=bool(v.get("enabled")))
        ttk.Checkbutton(f, text="Für Alexa & Google Home freigeben", variable=self.v_en).pack(anchor="w")
        seen = getattr(app, "voice_seen", 0)
        st = (f"✅ Matter-Bridge verbunden – zuletzt {time.strftime('%H:%M', time.localtime(seen))} Uhr"
              if seen and time.time() - seen < 300 else "Matter-Bridge noch nicht verbunden – kommt mit dem Heimserver")
        ttk.Label(f, text=st, style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        outer = ttk.Frame(f)
        outer.pack(fill="both", expand=True)
        cv = tk.Canvas(outer, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        self.inner = ttk.Frame(cv)
        self.inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self.inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        sel = V.selection(app)
        self.rows = {}
        cands = V.candidates(app)
        r = 0
        for typ, title in V.GROUPS:
            group = [c for c in cands if c["type"] == typ]
            if not group:
                continue
            ttk.Label(self.inner, text=title, font=("Segoe UI Semibold", 10)).grid(row=r, column=0, columnspan=3,
                                                                                  sticky="w", pady=(10, 2))
            r += 1
            for c in group:
                s = sel.get(c["id"], {})
                on = tk.BooleanVar(value=bool(s.get("on")))
                name = tk.StringVar(value=s.get("name") or c["default"])
                ttk.Checkbutton(self.inner, text=c["label"], variable=on, width=34).grid(row=r, column=0, sticky="w")
                ttk.Label(self.inner, text="heißt dort:", style="Muted.TLabel").grid(row=r, column=1, padx=6)
                ttk.Entry(self.inner, textvariable=name, width=26).grid(row=r, column=2, sticky="w", pady=1)
                self.rows[c["id"]] = (on, name)
                r += 1
        if not cands:
            ttk.Label(self.inner, text="Noch nichts da – sobald FRITZ!DECT-Geräte, Szenen, Personen oder Drucker "
                                       "eingerichtet sind, erscheinen sie hier.", style="Muted.TLabel").grid(row=0, column=0)
        ttk.Label(f, text="Tipp: kurze, eindeutige Namen ohne Raumnamen-Dopplung – z. B. „Schreibtisch“ statt "
                          "„Steckdose Schreibtisch Büro“. Szenen sagst du mit „… an“.", style="Muted.TLabel",
                  wraplength=660, justify="left").pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self.grab_set()
        _center(self, master)

    def _ok(self):
        items = {cid: {"on": on.get(), "name": name.get()} for cid, (on, name) in self.rows.items()}
        try:
            n = self.V.save_selection(self.app, self.v_en.get(), items)
        except ValueError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        self.app.log("App", "Sprachassistenten", f"{n} Geräte freigegeben" if self.v_en.get() else "aus")
        self.destroy()


# =========================================================================== #
# Sicherung
# =========================================================================== #
class BackupDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import backup as B
        self.app, self.B = app, B
        self.title("Sicherung")
        self.configure(bg=BG)
        self.geometry("720x700")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="💾 Sicherung", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Jede Nacht sichert Hauskern Einstellungen, Verlauf, Druck- und Stromdaten. Behalten werden "
                          "die letzten 7 Tage,\n4 Wochen und 5 manuelle Sicherungen. Wiederherstellen legt die jetzigen "
                          "Daten vorher beiseite.", style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 8))
        self.l_st = ttk.Label(f, text="", font=("Segoe UI Semibold", 10))
        self.l_st.pack(anchor="w")
        c = app.backup_cfg()
        g = ttk.Frame(f)
        g.pack(anchor="w", pady=(8, 4))
        self.v_en = tk.BooleanVar(value=c.get("enabled", True))
        ttk.Checkbutton(g, text="Jede Nacht automatisch um", variable=self.v_en).grid(row=0, column=0, sticky="w")
        self.v_time = tk.StringVar(value=c.get("time", "03:30"))
        ttk.Entry(g, textvariable=self.v_time, width=7).grid(row=0, column=1, padx=6)
        ttk.Label(g, text="Uhr").grid(row=0, column=2, sticky="w")
        ttk.Label(g, text="Zusätzlich kopieren nach").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_extra = tk.StringVar(value=c.get("extra_dir", ""))
        ttk.Entry(g, textvariable=self.v_extra, width=36).grid(row=1, column=1, columnspan=2, sticky="w", padx=6,
                                                              pady=(6, 0))
        ttk.Button(g, text="Ordner …", command=self._pick).grid(row=1, column=3, pady=(6, 0))
        self.v_pull = tk.BooleanVar(value=c.get("pi_pull", True))
        if app.cfg.get("pi_host"):
            ttk.Checkbutton(g, text="Jeden Tag eine Kopie vom Heimserver auf diesen PC holen",
                            variable=self.v_pull).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        n = app.nas_cfg()
        nf = ttk.Frame(f, style="Card.TFrame", padding=10)
        nf.pack(fill="x", pady=(10, 0))
        self.v_nas = tk.BooleanVar(value=bool(n.get("enabled")))
        ttk.Checkbutton(nf, text="Zusätzlich auf Netzwerkspeicher (z. B. SSD an der FRITZ!Box)",
                        variable=self.v_nas).grid(row=0, column=0, columnspan=4, sticky="w")
        self.nv = {k: tk.StringVar(value=str(n.get(k) or d)) for k, d in
                   (("host", "fritz.box"), ("user", ""), ("dir", ""), ("keep", 14))}
        self.nv["pw"] = tk.StringVar(value="")
        for i, (k, lbl, show, w) in enumerate((("host", "Adresse", "", 16), ("user", "Benutzer", "", 16),
                                                ("pw", "Passwort", "•", 16), ("dir", "Ordner", "", 30))):
            ttk.Label(nf, text=lbl, style="CardSub.TLabel").grid(row=1 + i // 2, column=(i % 2) * 2, sticky="w",
                                                                 pady=2, padx=(0, 6))
            ttk.Entry(nf, textvariable=self.nv[k], width=w, show=show).grid(row=1 + i // 2, column=(i % 2) * 2 + 1,
                                                                           sticky="w", pady=2, padx=(0, 12))
        ttk.Button(nf, text="Testen", command=self.nas_test).grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.l_nas = ttk.Label(nf, text="Benutzer braucht in der FRITZ!Box das Recht „Zugang zu NAS-Inhalten“; "
                                        "FTP-Zugriff einschalten. Passwort leer = bleibt.", style="CardSub.TLabel",
                               wraplength=480, justify="left")
        self.l_nas.grid(row=3, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.tree = ttk.Treeview(f, columns=("when", "size", "ver", "where"), show="headings", height=9,
                                 selectmode="browse")
        for col, txt, w in (("when", "Sicherung", 180), ("size", "Größe", 80), ("ver", "Version", 70),
                            ("where", "Von", 220)):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w)
        self.tree.pack(fill="both", expand=True, pady=(10, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="💾 Jetzt sichern", style="Accent.TButton", command=self.now).pack(side="left")
        ttk.Button(b, text="↩ Wiederherstellen …", command=self.restore).pack(side="left", padx=6)
        ttk.Button(b, text="Aus Datei …", command=self.restore_file).pack(side="left")
        ttk.Button(b, text="Ordner öffnen", command=self._open).pack(side="left", padx=6)
        ttk.Button(b, text="Speichern & schließen", command=self._ok).pack(side="right")
        self.render()
        _center(self, master)

    def render(self):
        B, a = self.B, self.app
        c = a.backup_cfg()
        last = c.get("last")
        self.l_st.configure(text=(("✅ " if c.get("last_ok", True) else "⚠ ") + f"Letzte Sicherung: {B.fmt_ts(last)}"
                                  + ("" if c.get("last_ok", True) else f" – {c.get('last_err', '')}"))
                            if last else "Noch keine Sicherung – die erste kommt heute Nacht (oder jetzt).",
                            foreground=OK if c.get("last_ok", True) else BAD)
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        pi_dir = os.path.join(os.path.dirname(a.backups_dir), "pi-sicherungen")
        for src, lst in (("dieser Rechner", a.list_backups()), ("Raspberry Pi", B.list_backups(pi_dir))):
            for x in lst:
                iid = x["path"]
                self.items[iid] = x
                self.tree.insert("", "end", iid=iid, values=(B.fmt_ts(x["ts"]) + (" · manuell" if x["manual"] else ""),
                                                             B.fmt_size(x["size"]), x["meta"].get("version", "?"),
                                                             f"{src} ({x['meta'].get('host', '')})"))

    def _nas_vals(self):
        n = self.app.nas_cfg()
        n.update(enabled=self.v_nas.get(), host=self.nv["host"].get().strip() or "fritz.box",
                 user=self.nv["user"].get().strip(), dir=self.nv["dir"].get().strip())
        try:
            n["keep"] = max(1, int(self.nv["keep"].get()))
        except ValueError:
            n["keep"] = 14
        if self.nv["pw"].get():
            n["pw"] = self.nv["pw"].get()
        return n

    def nas_test(self):
        n = self._nas_vals()
        self.l_nas.configure(text="prüfe …", foreground=MUTED)

        def run():
            try:
                tls, cnt, roots = self.B.nas_test(n["host"], n["user"], n["pw"], n["dir"])
                msg, col = (f"✅ Verbunden ({'verschlüsselt' if tls else 'unverschlüsselt im Heimnetz'}) · {cnt} "
                            f"Sicherungen · Laufwerke: {', '.join(roots[:4]) or '–'}"), OK
            except self.B.BackupError as e:
                msg, col = f"⚠ {e}", BAD
            ui_later(self, lambda: self.l_nas.configure(text=msg, foreground=col))
        threading.Thread(target=run, daemon=True).start()

    def _pick(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=self, title="Zusätzlicher Sicherungsordner (z. B. USB-Stick, NAS)")
        if d:
            self.v_extra.set(d)

    def _open(self):
        import subprocess
        os.makedirs(self.app.backups_dir, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.app.backups_dir)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", self.app.backups_dir])

    def now(self):
        def done(res):
            ui_later(self, self.render)
        if not self.app.run_backup("manual", done=done):
            messagebox.showinfo(APP, "Es läuft gerade schon eine Sicherung.", parent=self)
        self.l_st.configure(text="Sichert …", foreground=MUTED)

    def _restore(self, path):
        try:
            meta = self.B.check(path)
        except (self.B.BackupError, OSError) as e:
            messagebox.showwarning(APP, f"Diese Datei lässt sich nicht einspielen:\n{e}", parent=self)
            return
        other = meta.get("os") and meta.get("os") != os.name
        if not messagebox.askyesno(APP, f"Sicherung vom {self.B.fmt_ts(meta.get('created'))} ({meta.get('host', '')}) "
                                        "einspielen?\n\nDie jetzigen Daten werden vorher in einen Ordner daneben gelegt. "
                                        "Hauskern startet dabei neu."
                                        + ("\n\nAchtung: Die Sicherung stammt von einem anderen Betriebssystem – "
                                           "gespeicherte Passwörter musst du danach neu eingeben." if other else ""),
                                   parent=self):
            return
        self.app.restore_backup(path)
        self.destroy()

    def restore(self):
        s = self.tree.selection()
        if s:
            self._restore(s[0])

    def restore_file(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self, title="Hauskern-Sicherung wählen", filetypes=[("Sicherung", "*.zip")])
        if p:
            self._restore(p)

    def _ok(self):
        import automations as A
        if A._hm(self.v_time.get()) is None:
            messagebox.showwarning(APP, "Uhrzeit bitte als HH:MM, z. B. 03:30.", parent=self)
            return
        extra = self.v_extra.get().strip()
        if extra and not os.path.isdir(extra):
            messagebox.showwarning(APP, f"Den Ordner „{extra}“ gibt es nicht.", parent=self)
            return
        c = dict(self.app.cfg.get("backup") or {})
        n = self._nas_vals()
        if n["enabled"] and not (n["user"] and n["pw"]):
            messagebox.showwarning(APP, "Für den Netzwerkspeicher bitte Benutzer und Passwort eintragen.", parent=self)
            return
        c.update(enabled=self.v_en.get(), time=self.v_time.get().strip(), extra_dir=extra, pi_pull=self.v_pull.get())
        self.app.cfg["backup"] = c
        self.app.save_nas(n)
        self.destroy()


# =========================================================================== #
# Zeitraffer
# =========================================================================== #
def _de_date(iso):
    try:
        y, m, d = str(iso).split("-")
        return f"{int(d):02d}.{int(m):02d}.{y}"
    except ValueError:
        return ""


def _iso_date(de):
    de = str(de or "").strip()
    if not de:
        return ""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", de)
    if not m:
        raise ValueError("Datum bitte als TT.MM.JJJJ, z. B. 10.01.2027")
    d, mo, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 else y
    try:
        time.strptime(f"{y}-{mo}-{d}", "%Y-%m-%d")
    except ValueError as e:
        raise ValueError("Dieses Datum gibt es nicht") from e
    return f"{y:04d}-{mo:02d}-{d:02d}"


class VacationDialog(tk.Toplevel):
    """Urlaubsmodus: Licht wie zu Hause, Heizung sparen, Kamera-Push."""

    def __init__(self, master, app):
        super().__init__(master)
        import vacation as VA
        self.app, self.VA = app, VA
        self.title("Urlaubsmodus")
        self.configure(bg=BG)
        self.geometry("640x720")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🏖 Urlaubsmodus", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Lampen gehen abends zu leicht wechselnden Zeiten an und aus, die Heizung spart, und bei "
                          "Personen an der Kamera kommt eine Push-Nachricht mit Bild. Kommt jemand nach Hause, "
                          "endet er von selbst.", style="Muted.TLabel", wraplength=590, justify="left").pack(
            anchor="w", pady=(2, 8))
        self.l_state = ttk.Label(f, text="", font=("Segoe UI Semibold", 11))
        self.l_state.pack(anchor="w", pady=(0, 6))
        if app.cfg.get("satellite"):
            ttk.Label(f, text="⚠ Hauskern läuft auf dem Heimserver – Urlaubsmodus bitte am Handy schalten "
                              "(⚙ → 🏖 Urlaubsmodus), sonst greift er nicht.", foreground=WARN,
                      wraplength=590, justify="left").pack(anchor="w", pady=(0, 6))
        c = app.vacation_cfg()
        g = ttk.Frame(f)
        g.pack(anchor="w", fill="x")
        self.v_until = tk.StringVar(value=_de_date(c["until"]))
        self.v_back = tk.StringVar(value=c["back"])
        self.v_end = tk.StringVar(value=c["end"])
        self.v_spread = tk.StringVar(value=str(int(c["spread"])))
        self.v_start = tk.StringVar(value=dict(VA.START_CHOICES).get(c["start"], VA.START_CHOICES[0][1]))
        rows = [("Rückkehr am (TT.MM.JJJJ, leer = offen)", ttk.Entry(g, textvariable=self.v_until, width=14)),
                ("Ankunft ungefähr um", ttk.Entry(g, textvariable=self.v_back, width=8)),
                ("Licht an", ttk.Combobox(g, textvariable=self.v_start, values=[n for _, n in VA.START_CHOICES],
                                          state="readonly", width=20)),
                ("Licht aus um", ttk.Entry(g, textvariable=self.v_end, width=8)),
                ("Zufall (± Minuten)", ttk.Entry(g, textvariable=self.v_spread, width=8))]
        for i, (lbl, w) in enumerate(rows):
            ttk.Label(g, text=lbl).grid(row=i, column=0, sticky="w", pady=2)
            w.grid(row=i, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Lampen (Steckdosen), die abends angehen:", font=("Segoe UI Semibold", 10)).pack(
            anchor="w", pady=(10, 2))
        pf = ttk.Frame(f)
        pf.pack(anchor="w", fill="x")
        self.v_plugs = {}
        import webadmin
        opts = webadmin._switch_opts(app)
        for i, (ain, name) in enumerate(opts):
            v = tk.BooleanVar(value=ain in c["plugs"])
            self.v_plugs[ain] = v
            ttk.Checkbutton(pf, text=name, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18))
        if not opts:
            ttk.Label(pf, text="Noch keine schaltbare FRITZ!DECT-Steckdose gefunden.", style="Muted.TLabel").grid(
                row=0, column=0)
        self.has_heat = any(d.get("hkr") for d in app.data.get("smart", []))
        hf = ttk.Frame(f)
        hf.pack(anchor="w", fill="x", pady=(10, 0))
        self.v_heat = tk.BooleanVar(value=c["heat"])
        self.v_temp = tk.StringVar(value=fnum_de(c["heat_temp"], 1))
        self.v_pre = tk.StringVar(value=fnum_de(c["preheat_h"], 1))
        if self.has_heat:
            ttk.Checkbutton(hf, text="Heizung absenken auf höchstens", variable=self.v_heat).grid(row=0, column=0,
                                                                                                  sticky="w")
            ttk.Entry(hf, textvariable=self.v_temp, width=5).grid(row=0, column=1, padx=4)
            ttk.Label(hf, text="°C").grid(row=0, column=2, sticky="w")
            ttk.Label(hf, text="Vor der Rückkehr wieder aufheizen (Stunden)").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Entry(hf, textvariable=self.v_pre, width=5).grid(row=1, column=1, padx=4)
        self.v_cam = tk.BooleanVar(value=c["cam_push"])
        self.v_auto = tk.BooleanVar(value=c["auto_end"])
        ttk.Checkbutton(f, text="Push mit Bild, wenn jemand an der Kamera ist", variable=self.v_cam).pack(
            anchor="w", pady=(10, 0))
        ttk.Checkbutton(f, text="Automatisch aus, wenn jemand nach Hause kommt", variable=self.v_auto).pack(anchor="w")
        b = ttk.Frame(f)
        b.pack(fill="x", side="bottom", pady=(12, 0))
        self.b_toggle = ttk.Button(b, text="", style="Accent.TButton", command=self.toggle)
        self.b_toggle.pack(side="left")
        ttk.Button(b, text="Speichern & schließen", command=self._ok).pack(side="right")
        self.render()
        _center(self, master)

    def render(self):
        c = self.app.vacation_cfg()
        self.l_state.configure(text=f"Status: {self.VA.describe(c)}", foreground=AMBER if c["active"] else MUTED)
        self.b_toggle.configure(text="🏖 Urlaubsmodus ausschalten" if c["active"] else "🏖 Urlaubsmodus einschalten")

    def _vals(self, active):
        return {"active": active, "until": _iso_date(self.v_until.get()), "back": self.v_back.get().strip(),
                "end": self.v_end.get().strip(), "spread": self.v_spread.get().strip(),
                "start": {n: k for k, n in self.VA.START_CHOICES}.get(self.v_start.get(), "sunset"),
                "plugs": [a for a, v in self.v_plugs.items() if v.get()],
                "cam_push": self.v_cam.get(), "auto_end": self.v_auto.get(),
                **({"heat": self.v_heat.get(), "heat_temp": self.v_temp.get().replace(",", "."),
                    "preheat_h": self.v_pre.get().replace(",", ".")} if self.has_heat else {})}

    def _post(self, active):
        import webadmin
        try:
            ok, msg, _ = webadmin.post(self.app, "vacation", "save", None, self._vals(active), {"name": ""})
        except (ValueError, webadmin.Bad) as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return None
        return msg

    def toggle(self):
        msg = self._post(not self.app.vacation_cfg()["active"])
        if msg:
            self.render()
            self.app._on_status(msg)

    def _ok(self):
        if self._post(self.app.vacation_cfg()["active"]) is not None:
            self.destroy()


class AppliancesDialog(tk.Toplevel):
    """Waschmaschine & Co.: melden, wenn das Programm fertig ist."""

    def __init__(self, master, app):
        super().__init__(master)
        import appliances as AP
        self.app, self.AP = app, AP
        self.title("Waschmaschine & Co.")
        self.configure(bg=BG)
        self.geometry("640x520")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🧺 Waschmaschine & Co.", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Hängt Waschmaschine, Trockner oder Spülmaschine an einer FRITZ!DECT-Messsteckdose, erkennt "
                          "Hauskern am Stromverbrauch, wann das Programm fertig ist, und schickt eine Push-Nachricht.",
                  style="Muted.TLabel", wraplength=590, justify="left").pack(anchor="w", pady=(2, 8))
        self.lb = tk.Listbox(f, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                             font=("Segoe UI", 10), activestyle="none", height=8)
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda e: self.edit())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="＋ Gerät", style="Accent.TButton", command=lambda: self.edit(new=True)).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.delete).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="🐞 Klappt nicht? Melden", command=self.report).pack(side="right", padx=6)
        self.render()
        _center(self, master)

    def render(self):
        import webadmin
        self.items = self.app.appliances()
        names = dict(webadmin._power_opts(self.app))
        self.lb.delete(0, "end")
        now = time.time()
        for a in self.items:
            state, sub = self.AP.status(a, self.app.appl_state.get(a["id"]), now)
            self.lb.insert("end", f"{self.AP.icon(a)} {a['name']}   ·   {names.get(a.get('ain'), 'Steckdose fehlt')}"
                                  f"   ·   {state}{' ' + sub if sub else ''}")
        if not self.items:
            self.lb.insert("end", "Noch kein Gerät – „＋ Gerät“ anklicken.")

    def _sel(self):
        s = self.lb.curselection()
        return self.items[s[0]] if s and s[0] < len(self.items) else None

    def edit(self, new=False):
        a = None if new else self._sel()
        if not new and not a:
            return
        d = ApplianceEdit(self, self.app, a)
        self.wait_window(d)
        self.render()

    def delete(self):
        a = self._sel()
        if a and messagebox.askyesno(APP, f"„{a['name']}“ entfernen?", parent=self):
            self.app.remove_appliance(a["id"])
            self.render()


class ApplianceEdit(tk.Toplevel):
    def __init__(self, master, app, a):
        super().__init__(master)
        import appliances as AP
        import webadmin
        self.app, self.AP, self.a = app, AP, a
        self.title("Gerät")
        self.configure(bg=BG)
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        a = AP.norm(a or {"kind": "wash", "name": ""})
        self.plugs = webadmin._power_opts(app)
        self.v = {"name": tk.StringVar(value=a.get("name", "")),
                  "kind": tk.StringVar(value=dict(AP.KIND_CHOICES)[a.get("kind", "wash")]),
                  "ain": tk.StringVar(value=dict(self.plugs).get(a.get("ain"), "")),
                  "remind_min": tk.StringVar(value=str(int(a.get("remind_min") or 0))),
                  "start_w": tk.StringVar(value=fnum_de(a["start_w"], 1)),
                  "end_w": tk.StringVar(value=fnum_de(a["end_w"], 1)),
                  "end_min": tk.StringVar(value=fnum_de(a["end_min"], 1))}
        rows = [("Name", ttk.Entry(f, textvariable=self.v["name"], width=26)),
                ("Art", ttk.Combobox(f, textvariable=self.v["kind"], values=[n for _, n in AP.KIND_CHOICES],
                                     state="readonly", width=24)),
                ("Messsteckdose", ttk.Combobox(f, textvariable=self.v["ain"], values=[n for _, n in self.plugs],
                                               state="readonly", width=24)),
                ("Erinnern nach (Min., 0 = nie)", ttk.Entry(f, textvariable=self.v["remind_min"], width=8)),
                ("Läuft ab (Watt)", ttk.Entry(f, textvariable=self.v["start_w"], width=8)),
                ("Fertig unter (Watt)", ttk.Entry(f, textvariable=self.v["end_w"], width=8)),
                ("… für mindestens (Min.)", ttk.Entry(f, textvariable=self.v["end_min"], width=8))]
        for i, (lbl, w) in enumerate(rows):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=3)
            w.grid(row=i, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Die Watt-Werte nur ändern, wenn es zu früh oder zu spät meldet.", style="Muted.TLabel").grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 0))
        if not self.plugs:
            ttk.Label(f, text="Noch keine FRITZ!DECT-Messsteckdose gefunden.", foreground=WARN).grid(
                row=len(rows) + 1, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Speichern", style="Accent.TButton", command=self._ok).grid(
            row=len(rows) + 2, column=1, sticky="e", pady=(12, 0))
        _center(self, master)

    def _ok(self):
        import webadmin
        inv_k = {n: k for k, n in self.AP.KIND_CHOICES}
        inv_p = {n: k for k, n in self.plugs}
        vals = {"name": self.v["name"].get(), "kind": inv_k.get(self.v["kind"].get(), "wash"),
                "ain": inv_p.get(self.v["ain"].get(), (self.a or {}).get("ain", "")),
                **{k: self.v[k].get().replace(",", ".") for k in ("remind_min", "start_w", "end_w", "end_min")}}
        try:
            webadmin.post(self.app, "appliances", "save", (self.a or {}).get("id"), vals, {"name": ""})
        except webadmin.Bad as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        self.destroy()


class IntegrationsDialog(tk.Toplevel):
    """Weitere Hersteller: Shelly, Philips Hue, Tasmota, ESPHome, Zigbee2MQTT, Sky Q, Vestel-Fernseher."""

    def __init__(self, master, app):
        super().__init__(master)
        import providers as PR
        self.app, self.PR = app, PR
        self.title("Weitere Hersteller")
        self.configure(bg=BG)
        self.geometry("720x540")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🔌 Weitere Hersteller", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Geräte anderer Marken einbinden – alles lokal im Heimnetz, ohne Hersteller-Cloud. Danach "
                          "funktionieren Kacheln, Abläufe, Urlaubsmodus, Waschmaschine & Co. und Siri auch mit ihnen.",
                  style="Muted.TLabel", wraplength=660, justify="left").pack(anchor="w", pady=(2, 8))
        self.lb = tk.Listbox(f, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                             font=("Segoe UI", 10), activestyle="none", height=9)
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda e: self.edit())
        self.l_sug = ttk.Label(f, text="", style="Muted.TLabel", wraplength=660, justify="left")
        self.l_sug.pack(anchor="w", pady=(6, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="＋ Hinzufügen", style="Accent.TButton", command=lambda: self.edit(new=True)).pack(side="left")
        ttk.Button(b, text="Bearbeiten", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.delete).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="🐞 Klappt nicht? Melden", command=self.report).pack(side="right", padx=6)
        self.render()
        self._tick()
        _center(self, master)

    def _tick(self):
        try:
            if self.winfo_exists():
                self.render()
                self.after(3000, self._tick)
        except tk.TclError:
            pass

    def render(self):
        self.items = list(self.app.cfg.get("integrations", []))
        sel = self.lb.curselection()
        self.lb.delete(0, "end")
        for c in self.items:
            self.lb.insert("end", f"{self.PR.TYPES.get(c['type'], (c['type'],))[0]}  ·  {self.app.integration_title(c)}"
                                  f"  ·  {c.get('host')}   –   {self.app.integration_status(c['id'])}")
        if not self.items:
            self.lb.insert("end", "Noch nichts eingebunden – „＋ Hinzufügen“ anklicken.")
        if sel and sel[0] < len(self.items):
            self.lb.selection_set(sel[0])
        sug = self.app.integration_suggestions()
        self.l_sug.configure(text=("🔎 In deinem Netz gefunden (noch nicht eingebunden): " + ", ".join(
            f"{n} ({self.PR.TYPES[t][0]}, {ip})" for ip, n, t in sug[:6])) if sug else
            "Tipp: Unter „Gerätesuche“ findet Hauskern Shelly, Hue, Tasmota und ESPHome im Netz von selbst.")

    def _sel(self):
        s = self.lb.curselection()
        return self.items[s[0]] if s and s[0] < len(self.items) else None

    def edit(self, new=False):
        c = None if new else self._sel()
        if not new and not c:
            return
        d = IntegrationEdit(self, self.app, c)
        self.wait_window(d)
        self.render()

    def report(self):
        c = self._sel()
        txt = ""
        if c:
            txt = (f"Ich wollte {self.PR.TYPES.get(c['type'], (c['type'],))[0]} unter {c.get('host')} einbinden.\n"
                   f"Hauskern meldet: {self.app.integration_status(c['id'])}\n\nWas ich gemacht habe: ")
        self.app.bug_dialog("vendors", txt)

    def delete(self):
        c = self._sel()
        if c and messagebox.askyesno(APP, f"„{c.get('name') or c.get('host')}“ entfernen? Die Geräte verschwinden "
                                          "dann aus Hauskern.", parent=self):
            self.app.remove_integration(c["id"])
            self.render()


class IntegrationEdit(tk.Toplevel):
    def __init__(self, master, app, c):
        super().__init__(master)
        import providers as PR
        self.app, self.PR, self.c = app, PR, c or {}
        self.title("Hersteller einbinden")
        self.configure(bg=BG)
        self.transient(master)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        c = c or {"type": "shelly", "enabled": True}
        self.types = [(k, v[0]) for k, v in PR.TYPES.items()]
        sug = app.integration_suggestions()
        self.v = {"type": tk.StringVar(value=dict(self.types)[c.get("type", "shelly")]),
                  "host": tk.StringVar(value=c.get("host", "") or (sug[0][0] if sug and not self.c else "")),
                  "name": tk.StringVar(value=c.get("name", "")), "user": tk.StringVar(value=c.get("user", "")),
                  "pw": tk.StringVar(value=""), "port": tk.StringVar(value=str(c.get("port", 1883))),
                  "base": tk.StringVar(value=c.get("base", "zigbee2mqtt")), "mac": tk.StringVar(value=c.get("mac", ""))}
        self.v_en = tk.BooleanVar(value=c.get("enabled", True))
        rows = [("Hersteller", ttk.Combobox(f, textvariable=self.v["type"], values=[n for _, n in self.types],
                                            state="readonly", width=28)),
                ("Adresse (IP)", ttk.Combobox(f, textvariable=self.v["host"], values=[ip for ip, _, _ in sug], width=28)),
                ("Name (optional)", ttk.Entry(f, textvariable=self.v["name"], width=30)),
                ("Benutzer (falls Passwort)", ttk.Entry(f, textvariable=self.v["user"], width=30)),
                ("Passwort (falls gesetzt)", ttk.Entry(f, textvariable=self.v["pw"], width=30, show="•")),
                ("MQTT-Port (nur Zigbee)", ttk.Entry(f, textvariable=self.v["port"], width=8)),
                ("MQTT-Thema (nur Zigbee)", ttk.Entry(f, textvariable=self.v["base"], width=30)),
                ("MAC (nur Fernseher, optional)", ttk.Entry(f, textvariable=self.v["mac"], width=20))]
        for i, (lbl, w) in enumerate(rows):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=3)
            w.grid(row=i, column=1, sticky="w", padx=8)
        ttk.Checkbutton(f, text="Eingeschaltet", variable=self.v_en).grid(row=len(rows), column=1, sticky="w")
        self.l_msg = ttk.Label(f, text="Tipp: Adresse eintragen und „Erkennen“ klicken.\nPhilips Hue: erst den Knopf auf "
                                       "der Bridge drücken, dann „Hue koppeln“.\nFernseher: im TV-Menü „Virtuelle Fernbedienung“ einschalten." + (
                                           "  ✔ Bereits gekoppelt." if self.c.get("key") else ""),
                               style="Muted.TLabel", justify="left")
        self.l_msg.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="we", pady=(12, 0))
        ttk.Button(b, text="🔎 Erkennen", command=lambda: self._op("detect")).pack(side="left")
        ttk.Button(b, text="🔗 Hue koppeln", command=lambda: self._op("pair")).pack(side="left", padx=6)
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=lambda: self._op("save")).pack(side="right")
        _center(self, master)

    def _vals(self):
        inv = {n: k for k, n in self.types}
        return {"type": inv.get(self.v["type"].get(), "shelly"), "host": self.v["host"].get().strip(),
                "name": self.v["name"].get().strip(), "user": self.v["user"].get().strip(), "pw": self.v["pw"].get(),
                "port": self.v["port"].get().strip(), "base": self.v["base"].get().strip(), "mac": self.v["mac"].get().strip(),
                "enabled": self.v_en.get()}

    def _op(self, op):
        import webadmin
        self.l_msg.configure(text="Einen Moment …", foreground=MUTED)
        self.update_idletasks()
        try:
            ok, msg, extra = webadmin.post(self.app, "integrations", op, self.c.get("id"), self._vals(), {"name": ""})
        except webadmin.Bad as e:
            self.l_msg.configure(text="⚠ " + str(e), foreground=WARN)
            if op == "save" or "Gespeichert" in str(e):
                self.c = next((x for x in self.app.cfg.get("integrations", []) if x.get("host") == self._vals()["host"]),
                              self.c)
            return
        if op == "detect":
            st = (extra or {}).get("set") or {}
            if st.get("type"):
                self.v["type"].set(dict(self.types)[st["type"]])
            if st.get("name") and not self.v["name"].get():
                self.v["name"].set(st["name"])
            self.l_msg.configure(text="✔ " + msg, foreground=OK)
            return
        self.c = next((x for x in self.app.cfg.get("integrations", []) if x.get("host") == self._vals()["host"]), self.c)
        if op == "pair":
            self.l_msg.configure(text=msg, foreground=OK)
            return
        self.destroy()


class ClipsDialog(tk.Toplevel):
    """Kamera-Aufnahmen: ansehen, löschen, Speichergrenze."""

    def __init__(self, master, app):
        super().__init__(master)
        import clips as C
        import timelapse as T
        self.app, self.C, self.T = app, C, T
        self.title("Kamera-Aufnahmen")
        self.configure(bg=BG)
        self.geometry("720x560")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🎥 Kamera-Aufnahmen", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Aufnahmen aus dem Kamerafenster (● Aufnahme) und aus Abläufen („Kamera-Aufnahme“).\n"
                          "Auch auf dem Handy: Kameras → 🎥 Aufnahmen. Ältere werden automatisch gelöscht.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 8))
        c = app.clips_cfg()
        h = ttk.Frame(f)
        h.pack(anchor="w")
        self.v = {"max_gb": tk.StringVar(value=fnum_de(c["max_gb"], 1)), "keep_days": tk.StringVar(value=str(int(c["keep_days"])))}
        for i, (k, lbl) in enumerate((("max_gb", "Höchstens Speicher (GB)"), ("keep_days", "Löschen nach (Tagen, 0 = nie)"))):
            ttk.Label(h, text=lbl).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(h, textvariable=self.v[k], width=8).grid(row=i, column=1, sticky="w", padx=6)
        self.l_use = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_use.pack(anchor="w", pady=(10, 2))
        self.lb = tk.Listbox(f, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                             font=("Segoe UI", 10), activestyle="none", height=10)
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda e: self.play())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="▶ Abspielen", command=self.play).pack(side="left")
        ttk.Button(b, text="Löschen", command=self.delete).pack(side="left", padx=6)
        ttk.Button(b, text="Ordner öffnen", command=lambda: open_folder(app.clips_dir())).pack(side="left")
        ttk.Button(b, text="Speichern & schließen", style="Accent.TButton", command=self._ok).pack(side="right")
        self.render()
        _center(self, master)

    def render(self):
        vids = self.C.list_clips(self.app.clips_dir(), self.app._clips_active())
        self.vids = vids
        self.lb.delete(0, "end")
        for v in vids:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(v["ts"]))
            self.lb.insert("end", f"{v['cam'] or 'Kamera'}   ·   {when}   ·   {self.T.fmt_size(v['size'])}")
        if not vids:
            self.lb.insert("end", "Noch keine Aufnahmen.")
        self.l_use.configure(text=f"Aufnahmen: {len(vids)} · belegt {self.T.fmt_size(sum(v['size'] for v in vids))} · "
                                  f"Ordner: {self.app.clips_dir()}")

    def _sel(self):
        s = self.lb.curselection()
        return self.vids[s[0]] if s and s[0] < len(self.vids) else None

    def play(self):
        v = self._sel()
        if v:
            if os.name == "nt":
                os.startfile(v["path"])  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", v["path"]])

    def delete(self):
        v = self._sel()
        if v and messagebox.askyesno(APP, f"Aufnahme „{v['name']}“ löschen?", parent=self):
            self.app.clip_delete(v["name"])
            self.render()

    def _ok(self):
        try:
            c = {"max_gb": max(0.2, float(self.v["max_gb"].get().replace(",", "."))),
                 "keep_days": max(0, int(self.v["keep_days"].get()))}
        except ValueError:
            messagebox.showwarning(APP, "Bitte nur Zahlen eingeben.", parent=self)
            return
        self.app.save_clips_cfg(c)
        self.destroy()


class TimelapseDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import timelapse as T
        self.app, self.T = app, T
        self.title("Zeitraffer")
        self.configure(bg=BG)
        self.geometry("720x620")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🎬 Zeitraffer", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Ein Bild pro Schicht, am Ende macht Hauskern ein Video daraus und löscht die Einzelbilder. "
                          "Standardmäßig aus.\n„Nur wenn ich ihn einschalte“: am Handy beim Drucker „🎬 Zeitraffer“ "
                          "tippen – gilt für diesen einen Druck.", style="Muted.TLabel", justify="left").pack(
            anchor="w", pady=(2, 8))
        c = app.tl_cfg()
        g = ttk.Frame(f)
        g.pack(anchor="w")
        self.cams = [("printer", "Kamera im Drucker")] + [(x["id"], f"Kamera „{x['name']}“")
                                                          for x in app.cfg.get("cameras", [])]
        self.pv = {}
        r = 0
        for serial, pr in app.printers.items():
            m = tk.StringVar(value=dict(T.MODES)[app.tl_mode(serial)])
            src0 = ((app.cfg.get("timelapse") or {}).get("sources") or {}).get(serial, "printer")
            sv = tk.StringVar(value=dict(self.cams).get(src0, self.cams[0][1]))
            ttk.Label(g, text=pr.name, font=("Segoe UI Semibold", 10)).grid(row=r, column=0, sticky="w", padx=(0, 10))
            ttk.Combobox(g, textvariable=m, values=[n for _, n in T.MODES], state="readonly", width=26).grid(
                row=r, column=1, pady=3)
            ttk.Combobox(g, textvariable=sv, values=[n for _, n in self.cams], state="readonly", width=24).grid(
                row=r, column=2, padx=6, pady=3)
            self.pv[serial] = (m, sv)
            r += 1
        if not app.printers:
            ttk.Label(g, text="Noch kein 3D-Drucker eingerichtet.", style="Muted.TLabel").grid(row=0, column=0)
        h = ttk.Frame(f)
        h.pack(anchor="w", pady=(10, 0))
        self.v = {k: tk.StringVar(value=fnum_de(c[k], 1) if k == "max_gb" else str(int(c[k])))
                  for k in ("interval", "max_gb", "keep_days")}
        self.v_w = tk.StringVar(value="1920 px (Full HD)" if int(c.get("width", 1280)) == 1920 else "1280 px (spart Platz)")
        for i, (k, lbl) in enumerate((("max_gb", "Höchstens Speicher (GB)"), ("keep_days", "Löschen nach (Tagen, 0 = nie)"),
                                      ("interval", "Ohne Schichten: Bild alle (s)"))):
            ttk.Label(h, text=lbl).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(h, textvariable=self.v[k], width=8).grid(row=i, column=1, sticky="w", padx=6)
        ttk.Label(h, text="Videobreite").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Combobox(h, textvariable=self.v_w, values=["1280 px (spart Platz)", "1920 px (Full HD)"], state="readonly",
                     width=22).grid(row=3, column=1, sticky="w", padx=6)
        self.l_use = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_use.pack(anchor="w", pady=(10, 2))
        self.lb = tk.Listbox(f, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                             font=("Segoe UI", 10), activestyle="none", height=9)
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda e: self.play())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="▶ Abspielen", command=self.play).pack(side="left")
        ttk.Button(b, text="Löschen", command=self.delete).pack(side="left", padx=6)
        ttk.Button(b, text="Ordner öffnen", command=lambda: open_folder(app.tl.out_dir)).pack(side="left")
        ttk.Button(b, text="Speichern & schließen", style="Accent.TButton", command=self._ok).pack(side="right")
        self.render()
        _center(self, master)

    def render(self):
        vids = self.app.tl.videos()
        self.vids = vids
        self.lb.delete(0, "end")
        for v in vids:
            self.lb.insert("end", f"{v['name'][:-4]}   ·   {self.T.fmt_size(v['size'])}")
        if not vids:
            self.lb.insert("end", "Noch keine Zeitraffer-Videos.")
        self.l_use.configure(text=f"Videos: {len(vids)} · belegt {self.T.fmt_size(sum(v['size'] for v in vids))} · "
                                  f"Ordner: {self.app.tl.out_dir}")

    def _sel(self):
        s = self.lb.curselection()
        return self.vids[s[0]] if s and s[0] < len(self.vids) else None

    def play(self):
        v = self._sel()
        if v:
            if os.name == "nt":
                os.startfile(v["path"])  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", v["path"]])

    def delete(self):
        v = self._sel()
        if v and messagebox.askyesno(APP, f"„{v['name']}“ löschen?", parent=self):
            self.app.tl.delete(v["name"])
            self.render()

    def _ok(self):
        T = self.T
        c = dict(self.app.cfg.get("timelapse") or {})
        inv_m = {n: k for k, n in T.MODES}
        inv_c = {n: k for k, n in self.cams}
        c["modes"] = dict(c.get("modes") or {}, **{s: inv_m[m.get()] for s, (m, _) in self.pv.items()})
        c["sources"] = dict(c.get("sources") or {}, **{s: inv_c.get(sv.get(), "printer") for s, (_, sv) in self.pv.items()})
        try:
            c.update(max_gb=max(0.2, float(self.v["max_gb"].get().replace(",", "."))),
                     keep_days=max(0, int(self.v["keep_days"].get())), interval=max(5, int(self.v["interval"].get())))
        except ValueError:
            messagebox.showwarning(APP, "Bitte nur Zahlen eingeben.", parent=self)
            return
        c["width"] = 1920 if self.v_w.get().startswith("1920") else 1280
        self.app.save_tl_cfg(c)
        self.destroy()


# =========================================================================== #
# Handy-Adresse mit QR-Code
# =========================================================================== #
class PhoneAccessDialog(tk.Toplevel):
    """Zeigt die Adresse der Handy-Oberfläche – zum Abscannen mit der iPhone-Kamera."""

    def __init__(self, master, app):
        super().__init__(master)
        import qr as Q
        self.app, self.Q = app, Q
        self.title("Handy-Zugriff")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="📱 Hauskern aufs Handy", style="H1.TLabel").pack(anchor="w")
        self.urls = app.web_urls()
        on = bool(app.cfg.get("web_enabled")) and bool(getattr(app, "web", None))
        if not self.urls:
            ttk.Label(f, text="Keine Netzwerkadresse gefunden – ist der PC mit dem Heimnetz verbunden?",
                      style="Muted.TLabel", wraplength=340).pack(anchor="w", pady=8)
            ttk.Button(f, text="Schließen", command=self.destroy).pack(anchor="e")
            return
        if not on:
            ttk.Label(f, text="⚠ Der Handy-Zugriff ist gerade aus – in den Einstellungen einschalten.",
                      foreground=WARN, wraplength=340).pack(anchor="w", pady=(4, 0))
        ttk.Label(f, text="Mit der iPhone-Kamera abscannen – im Heim-WLAN oder unterwegs mit VPN.",
                  style="Muted.TLabel", wraplength=340).pack(anchor="w", pady=(4, 10))
        self.qr = tk.Canvas(f, width=280, height=280, bg="#ffffff", highlightthickness=0)
        self.qr.pack()
        self.v = tk.StringVar(value=self.urls[0])
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        if len(self.urls) > 1:  # z. B. LAN + WLAN oder ein virtueller Adapter
            cb = ttk.Combobox(row, textvariable=self.v, values=self.urls, state="readonly", width=30)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: self._draw())
        else:
            ttk.Label(row, textvariable=self.v, font=("Segoe UI Semibold", 12)).pack(side="left")
        ttk.Button(row, text="Kopieren", command=self._copy).pack(side="right")
        ttk.Label(f, text=f"🖼 Wand-Display fürs Tablet: {self.urls[0]}/wand", foreground=ACCENT).pack(anchor="w", pady=(8, 0))
        ttk.Label(f, text="Tipp: In Safari öffnen → Teilen → „Zum Home-Bildschirm“.\n"
                          "Unterwegs: WireGuard an – zu Hause im WLAN aus (in der WireGuard-App\n"
                          "„Bei Bedarf aktivieren“ mit dem Heim-WLAN als Ausnahme).",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Button(f, text="Schließen", style="Accent.TButton", command=self.destroy).pack(anchor="e", pady=(12, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self._draw()
        _center(self, master)

    def _draw(self):
        self.qr.delete("all")
        draw_qr(self.qr, self.Q.encode(self.v.get()), 280)

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.v.get())
        self.app._on_status("Adresse kopiert 📋")


# =========================================================================== #
# Batterien
# =========================================================================== #
class BatteryDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Batterien")
        self.configure(bg=BG)
        self.geometry("720x460")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🔋 Batterien", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Sortiert nach „wird zuerst leer“. Hauskern merkt sich jeden Tag den Stand – die Schätzung "
                          "wird mit der Zeit genauer.\nEtwa drei Wochen vorher kommt eine Erinnerung aufs Handy.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 10))
        cols = ("pct", "left", "changed", "product")
        self.t = ttk.Treeview(f, columns=cols, show="tree headings", height=12)
        self.t.heading("#0", text="Gerät")
        self.t.column("#0", width=200)
        for c, txt, w in (("pct", "Batterie", 80), ("left", "Reicht noch", 210), ("changed", "Gewechselt", 100),
                          ("product", "Modell", 140)):
            self.t.heading(c, text=txt)
            self.t.column(c, width=w, anchor="w")
        self.t.tag_configure("low", foreground=BAD)
        self.t.tag_configure("mid", foreground=WARN)
        self.t.tag_configure("off", foreground=MUTED)
        self.t.pack(fill="both", expand=True)
        for b in app.battery_items():
            tag = "off" if not b["present"] else ("low" if b["low"] or b["pct"] <= 20 else ("mid" if b["pct"] <= 40 else ""))
            self.t.insert("", "end", text=b["name"], tags=(tag,),
                          values=(f"{b['pct']} %", b["left_text"] + ("" if b["present"] else " · nicht erreichbar"),
                                  time.strftime("%d.%m.%Y", time.localtime(b["changed"])) if b["changed"] else "–",
                                  b["product"]))
        if not self.t.get_children():
            self.t.insert("", "end", text="Keine Geräte mit Batterie gefunden.")
        ttk.Button(f, text="Schließen", style="Accent.TButton", command=self.destroy).pack(anchor="e", pady=(10, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        _center(self, master)


# =========================================================================== #
# Filament-Lager
# =========================================================================== #
class FilamentStockDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import filament as FL
        self.app, self.FL = app, FL
        self.title("Filament-Lager")
        self.configure(bg=BG)
        self.geometry("860x560")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🧵 Filament-Lager", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Nach jedem Druck zieht Hauskern den Verbrauch ab. Bambu-Rollen mit Chip kommen direkt aus "
                          "dem AMS. ● = steckt gerade im Drucker.", style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        self.l_new = ttk.Label(f, text="", foreground=ACCENT, cursor="hand2")
        self.l_new.pack(anchor="w")
        self.l_new.bind("<Button-1>", lambda e: self.take_tray())
        cols = ("left", "type", "where")
        self.t = ttk.Treeview(f, columns=cols, show="tree headings", height=14)
        self.t.heading("#0", text="Spule")
        self.t.column("#0", width=260)
        for c, txt, w in (("left", "Noch drauf", 130), ("type", "Material", 90), ("where", "Wo", 300)):
            self.t.heading(c, text=txt)
            self.t.column(c, width=w, anchor="w")
        self.t.tag_configure("low", foreground=WARN)
        self.t.pack(fill="both", expand=True, pady=(6, 0))
        self.t.bind("<Double-1>", lambda e: self.edit())
        self.imgs = {}
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="＋ Spule", command=lambda: self.edit(new=True)).pack(side="left")
        ttk.Button(b, text="Bearbeiten / Nachwiegen", command=self.edit).pack(side="left", padx=6)
        ttk.Button(b, text="Entfernen", command=self.delete).pack(side="left")
        lim = tk.StringVar(value=f"{app.fil_low_limit():g}")
        ttk.Label(b, text="   Warnen unter").pack(side="left")
        e = ttk.Entry(b, textvariable=lim, width=6)
        e.pack(side="left", padx=4)
        ttk.Label(b, text="g  ·  AMS unter").pack(side="left")
        self.v_ams = tk.StringVar(value=str(app.ams_low_pct()))
        ttk.Entry(b, textvariable=self.v_ams, width=4).pack(side="left", padx=4)
        ttk.Label(b, text="% (0 = aus)").pack(side="left")
        self.v_lim = lim
        ttk.Button(b, text="Schließen", style="Accent.TButton", command=self._close).pack(side="right")
        self.bind("<Escape>", lambda e: self._close())
        self.render()
        _center(self, master)

    def _img(self, color):
        color = color or "#555a66"
        if color not in self.imgs:
            im = tk.PhotoImage(width=14, height=14)
            im.put(color, to=(1, 1, 13, 13))
            self.imgs[color] = im
        return self.imgs[color]

    def render(self):
        FL, app = self.FL, self.app
        self.t.delete(*self.t.get_children())
        lim = app.fil_low_limit()
        for s in sorted(app.spools(), key=lambda q: (not q.get("slot"), float(q.get("left") or 0))):
            where = app.fil_slot_label(s.get("slot")) or s.get("loc") or "im Lager"
            low = float(s.get("left") or 0) < lim
            try:
                img = self._img(s.get("color"))
            except Exception:  # noqa: BLE001 – ungültige Farbe o. Ä.: dann eben ohne Farbfeld
                img = ""
            self.t.insert("", "end", iid=s["id"], text=("● " if s.get("slot") else "  ") + s["name"], image=img,
                          values=(FL.fmt_left(s) + (" ⚠" if low else ""), s["type"], where), tags=("low",) if low else ())
        unk = app.fil_unknown()
        self.l_new.configure(text=f"➕ {len(unk)} neue Rolle(n) im AMS erkannt – ins Lager übernehmen" if unk else "")

    def _sel(self):
        s = self.t.selection()
        return next((q for q in self.app.spools() if s and q["id"] == s[0]), None)

    def take_tray(self):
        unk = self.app.fil_unknown()
        if unk:
            k, _txt, t = unk[0]
            SpoolDialog(self, self.app, self.FL.from_tray(k, t), new=True)
            self.render()

    def edit(self, new=False):
        sp = None if new else self._sel()
        if not new and not sp:
            return
        SpoolDialog(self, self.app, sp or self.FL.new_spool(), new=new or sp is None)
        self.render()

    def delete(self):
        sp = self._sel()
        if sp and messagebox.askyesno(APP, f"„{sp['name']}“ aus dem Lager entfernen?", parent=self):
            self.app.fil_delete(sp["id"])
            self.render()

    def _close(self):
        try:
            v = float(self.v_lim.get().replace(",", "."))
            if v != self.app.fil_low_limit() and 0 <= v <= 5000:
                self.app.cfg["filament_low"] = v
                self.app._fil_after_change()
        except ValueError:
            pass
        try:
            a = int(float(self.v_ams.get().replace(",", ".")))
            if 0 <= a <= 50 and a != self.app.ams_low_pct():
                self.app.cfg["ams_low_pct"] = a
                self.app.save()
        except ValueError:
            pass
        self.destroy()


class SpoolDialog(tk.Toplevel):
    def __init__(self, master, app, sp, new=False):
        super().__init__(master)
        import filament as FL
        self.app, self.FL, self.sp, self.new = app, FL, dict(sp), new
        self.title("Neue Spule" if new else sp["name"])
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        self.v = {k: tk.StringVar(value=str(sp.get(k) if sp.get(k) not in (None,) else ""))
                  for k in ("name", "type", "brand", "loc")}
        self.v["total"] = tk.StringVar(value=f"{float(sp.get('total') or 1000):g}")
        self.v["left"] = tk.StringVar(value=f"{round(float(sp.get('left') or 0))}")
        self.v["tare"] = tk.StringVar(value=f"{float(sp.get('tare') or 0):g}" if sp.get("tare") else "")
        self.v["gross"], self.v["minus"] = tk.StringVar(), tk.StringVar()
        self.color = sp.get("color") or ""
        self.slots = [("", "– nicht im Drucker –")] + app.fil_slots()
        self.v_slot = tk.StringVar(value=dict(self.slots).get(sp.get("slot") or "", self.slots[0][1]))
        r = 0

        def row(lbl, w):
            nonlocal r
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            w.grid(row=r, column=1, sticky="w", pady=3)
            r += 1
        row("Name", ttk.Entry(f, textvariable=self.v["name"], width=32))
        row("Material", ttk.Combobox(f, textvariable=self.v["type"], values=FL.TYPES, width=18))
        cf = ttk.Frame(f)
        self.sw = tk.Canvas(cf, width=26, height=20, highlightthickness=0, bg=BG)
        self.sw.pack(side="left")
        ttk.Button(cf, text="Farbe wählen …", command=self.pick).pack(side="left", padx=6)
        self.l_col = ttk.Label(cf, text="", style="Muted.TLabel")
        self.l_col.pack(side="left")
        row("Farbe", cf)
        row("Hersteller", ttk.Entry(f, textvariable=self.v["brand"], width=20))
        row("Volle Spule (g)", ttk.Entry(f, textvariable=self.v["total"], width=8))
        row("Noch drauf (g)", ttk.Entry(f, textvariable=self.v["left"], width=8))
        row("Steckt in", ttk.Combobox(f, textvariable=self.v_slot, values=[t for _, t in self.slots], state="readonly",
                                      width=46))
        row("Lagerort", ttk.Entry(f, textvariable=self.v["loc"], width=24))
        ttk.Separator(f).grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1
        row("Leere Spule wiegt (g)", ttk.Entry(f, textvariable=self.v["tare"], width=8))
        row("Nachgewogen mit Spule (g)", ttk.Entry(f, textvariable=self.v["gross"], width=8))
        row("Verbrauch abziehen (g)", ttk.Entry(f, textvariable=self.v["minus"], width=8))
        info = "Bambu-Rolle mit Chip – die Restmenge kommt automatisch vom AMS." if sp.get("uuid") else ""
        if sp.get("used"):
            info += ("\n" if info else "") + f"Bisher verbraucht: {round(float(sp['used']))} g"
        if info:
            ttk.Label(f, text=info, style="Muted.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))
            r += 1
        b = ttk.Frame(f)
        b.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        self._show_color()
        _center(self, master)
        self.grab_set()
        self.wait_window(self)

    def _show_color(self):
        self.sw.delete("all")
        self.sw.create_rectangle(2, 2, 24, 18, fill=self.color or BG, outline=MUTED)
        self.l_col.configure(text=self.FL.color_name(self.color) or "keine")

    def pick(self):
        from tkinter import colorchooser
        _rgb, hx = colorchooser.askcolor(color=self.color or "#888888", parent=self, title="Filamentfarbe")
        if hx:
            self.color = hx.lower()
            self._show_color()

    def _num(self, k, what, allow_empty=False):
        t = self.v[k].get().strip().replace(",", ".")
        if not t and allow_empty:
            return None
        try:
            v = float(t)
            if v < 0:
                raise ValueError
            return v
        except ValueError:
            raise ValueError(f"{what}: bitte eine Zahl eingeben") from None

    def _ok(self):
        FL = self.FL
        try:
            total, left = self._num("total", "Volle Spule"), self._num("left", "Noch drauf")
            tare = self._num("tare", "Leere Spule", True) or 0
            gross, minus = self._num("gross", "Nachgewogen", True), self._num("minus", "Verbrauch", True)
        except ValueError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        if gross is not None and not tare:
            messagebox.showwarning(APP, "Zum Nachwiegen bitte einmal das Gewicht der leeren Spule eintragen.", parent=self)
            return
        slot = next((k for k, t in self.slots if t == self.v_slot.get()), "")
        sp = {"type": self.v["type"].get().strip() or "PLA", "color": self.color, "brand": self.v["brand"].get().strip(),
              "total": total or 1000, "left": left, "tare": tare, "slot": slot, "loc": self.v["loc"].get().strip()}
        sp["name"] = self.v["name"].get().strip() or FL.suggest_name(sp)
        if not self.new and self.sp.get("id"):
            sp["id"] = self.sp["id"]
        elif self.sp.get("uuid"):
            sp["uuid"] = self.sp["uuid"]
        self.app.fil_save(sp, gross=gross, minus=minus)
        self.destroy()


# =========================================================================== #
# Siri-Kurzbefehle
# =========================================================================== #
class SiriDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import webadmin
        self.app = app
        self.title("Siri-Kurzbefehle")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🗣 Siri-Kurzbefehle", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="„Hey Siri, Hauskern“ → „Schreibtisch aus“. Jeder Handy-Nutzer hat seinen eigenen Schlüssel – "
                          "es gelten seine Rechte.\nAm einfachsten richtest du es direkt am iPhone ein: Hauskern öffnen → "
                          "⚙ → 🗣 Siri-Kurzbefehle (dort zum Kopieren).", style="Muted.TLabel", justify="left").pack(
            anchor="w", pady=(2, 10))
        r = ttk.Frame(f)
        r.pack(fill="x")
        ttk.Label(r, text="Für").pack(side="left")
        self.users = [(u["id"], u["name"]) for u in app.cfg.get("web_users") or []]
        self.v_user = tk.StringVar(value=self.users[0][1] if self.users else "")
        cb = ttk.Combobox(r, textvariable=self.v_user, values=[n for _, n in self.users], state="readonly", width=18)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._show())
        self.v_url = tk.StringVar()
        u = ttk.Frame(f)
        u.pack(fill="x", pady=(8, 0))
        ttk.Entry(u, textvariable=self.v_url, width=70, state="readonly").pack(side="left")
        ttk.Button(u, text="Kopieren", command=self._copy).pack(side="left", padx=6)
        ttk.Button(u, text="Neuer Schlüssel", command=self._renew).pack(side="left")
        ttk.Label(f, text=webadmin.SIRI_STEPS, style="Muted.TLabel", justify="left", wraplength=640).pack(
            anchor="w", pady=(12, 8))
        t = ttk.Frame(f)
        t.pack(fill="x")
        ttk.Label(t, text="Ausprobieren:").pack(side="left")
        self.v_test = tk.StringVar(value="wie weit ist der drucker")
        e = ttk.Entry(t, textvariable=self.v_test, width=36)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self._test())
        ttk.Button(t, text="▶", command=self._test, width=3).pack(side="left")
        self.l_ans = ttk.Label(f, text="", foreground=ACCENT, wraplength=640, justify="left")
        self.l_ans.pack(anchor="w", pady=(6, 0))
        ttk.Button(f, text="Schließen", style="Accent.TButton", command=self.destroy).pack(anchor="e", pady=(12, 0))
        self.bind("<Escape>", lambda ev: self.destroy())
        self._show()
        _center(self, master)

    def _uid(self):
        return next((i for i, n in self.users if n == self.v_user.get()), None)

    def _show(self):
        if self._uid():
            self.v_url.set(self.app.siri_url(self._uid()))

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.v_url.get())
        self.l_ans.configure(text="📋 Adresse kopiert")

    def _renew(self):
        if self._uid() and messagebox.askyesno(APP, "Neuen Schlüssel erzeugen?\nDer bisherige Kurzbefehl funktioniert "
                                                     "dann nicht mehr und muss die neue Adresse bekommen.", parent=self):
            self.app.siri_token(self._uid(), renew=True)
            self._show()

    def _test(self):
        u = next((x for x in self.app.cfg.get("web_users") or [] if x["id"] == self._uid()), None)
        if u:
            self.l_ans.configure(text="🗣 " + self.app.siri_command(u, self.v_test.get()))


# =========================================================================== #
# Heizungs-Check
# =========================================================================== #
class HeatingDialog(tk.Toplevel):
    ICON = {"warn": "⚠️", "tip": "💡", "ok": "✅", "info": "ℹ️"}

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Heizungs-Check")
        self.configure(bg=BG)
        self.geometry("760x620")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="🔥 Heizungs-Check", style="H1.TLabel").pack(side="left")
        self.v_days = tk.StringVar(value="14 Tage")
        cb = ttk.Combobox(top, textvariable=self.v_days, values=["7 Tage", "14 Tage", "30 Tage"], state="readonly",
                          width=9)
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self.render(force=True))
        self.l_sum = ttk.Label(f, text="", style="Muted.TLabel", wraplength=700, justify="left")
        self.l_sum.pack(anchor="w", pady=(4, 8))
        self.txt = tk.Text(f, bg=CARD, fg=FG, relief="flat", wrap="word", font=("Segoe UI", 10), padx=14, pady=10,
                           highlightthickness=0, cursor="arrow")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("room", font=("Segoe UI Semibold", 12), spacing1=10)
        self.txt.tag_configure("meta", foreground=MUTED, font=("Segoe UI", 9))
        self.txt.tag_configure("warn", foreground=BAD, lmargin1=8, lmargin2=30, spacing1=4)
        self.txt.tag_configure("tip", foreground=FG, lmargin1=8, lmargin2=30, spacing1=4)
        self.txt.tag_configure("ok", foreground=MUTED, lmargin1=8, lmargin2=30, spacing1=4)
        self.txt.tag_configure("info", foreground=MUTED, lmargin1=8, lmargin2=30, spacing1=4)
        ttk.Button(f, text="Schließen", style="Accent.TButton", command=self.destroy).pack(anchor="e", pady=(10, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.render()
        _center(self, master)

    def render(self, force=False):
        days = int((self.v_days.get() or "14").split()[0])
        r = self.app.heating_report(days, force=force)
        s = r["summary"]
        self.l_sum.configure(text=(f"{s['tips']} Hinweis(e) in {s['rooms']} Räumen" if s["tips"] else
                                   "Keine Auffälligkeiten 👍") + " – Hauskern stellt dabei nichts um, du entscheidest."
                             + ("" if r["persons"] else "\nMit Personen (Anwesenheit) sieht der Check auch, ob "
                                                        "geheizt wird, wenn niemand zu Hause ist."))
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        for room in r["rooms"]:
            t.insert("end", room["name"] + "\n", "room")
            meta = []
            if room["avg_ist"] is not None:
                meta.append(f"Ø {fnum_de(room['avg_ist'], 1)} °C")
            if room["heat_h_week"] is not None:
                meta.append(f"{room['heat_h_week']} Std./Woche geheizt")
            if room["heatup_min"]:
                meta.append(f"Aufheizen ca. {room['heatup_min']} Min.")
            if meta:
                t.insert("end", " · ".join(meta) + "\n", "meta")
            for lvl, text in room["findings"] or [("ok", "Nichts Auffälliges.")]:
                t.insert("end", f"{self.ICON.get(lvl, '•')}  {text}\n", lvl)
        if not r["rooms"]:
            t.insert("end", "Keine Heizkörperregler gefunden.", "meta")
        t.configure(state="disabled")


# =========================================================================== #
# Anrufliste
# =========================================================================== #
class CallsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import calls as CL
        self.app, self.CL = app, CL
        self.title("Anrufliste")
        self.configure(bg=BG)
        self.geometry("720x520")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="📞 Anrufliste", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Direkt von der FRITZ!Box, jede Minute aktualisiert. Bei verpassten Anrufen kommt eine "
                          "Push-Nachricht (abschaltbar unter Push-Nachrichten).\nNamen kommen aus dem Telefonbuch der "
                          "FRITZ!Box.", style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 8))
        self.l_err = ttk.Label(f, text="", foreground=WARN, wraplength=660, justify="left")
        self.l_err.pack(anchor="w")
        cols = ("when", "what", "number", "min")
        self.t = ttk.Treeview(f, columns=cols, show="tree headings", height=14)
        self.t.heading("#0", text="Wer")
        self.t.column("#0", width=200)
        for c, txt, w in (("when", "Wann", 120), ("what", "", 150), ("number", "Nummer", 150), ("min", "Dauer", 60)):
            self.t.heading(c, text=txt)
            self.t.column(c, width=w, anchor="w")
        self.t.tag_configure("missed", foreground=BAD)
        self.t.pack(fill="both", expand=True)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        self.v_on = tk.BooleanVar(value=bool(app.calls_cfg().get("enabled", True)))
        ttk.Checkbutton(b, text="Anrufliste abrufen", variable=self.v_on, command=self._toggle).pack(side="left")
        ttk.Button(b, text="Aktualisieren", command=self._refresh).pack(side="left", padx=8)
        ttk.Button(b, text="Schließen", style="Accent.TButton", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.render()
        _center(self, master)

    def render(self):
        CL = self.CL
        self.t.delete(*self.t.get_children())
        for c in self.app.call_data[:60]:
            self.t.insert("", "end", text=f"{CL.ICON.get(c['type'], '')}  {CL.who(c)}",
                          tags=("missed",) if CL.is_missed(c) else (),
                          values=(CL.when(c["ts"]), CL.LABEL.get(c["type"], "") + (" · Nachricht" if c["message"] else ""),
                                  CL.pretty_number(c["number"]) if c["number"] else "",
                                  f"{c['minutes']} Min." if c["minutes"] else ""))
        if not self.app.call_data:
            self.t.insert("", "end", text="Noch keine Anrufe geladen.")
        self.l_err.configure(text=("⚠ " + self.app.call_err) if self.app.call_err else "")

    def _toggle(self):
        self.app.calls_cfg()["enabled"] = bool(self.v_on.get())
        self.app.save()

    def _refresh(self):
        self.app._calls_busy = False
        self.app._calls_tick_once()
        self.after(2500, self.render)


# =========================================================================== #
# Was ist neu?
# =========================================================================== #
class WhatsNewDialog(tk.Toplevel):
    def __init__(self, master, sections):
        super().__init__(master)
        import changelog
        self.title("Was ist neu?")
        self.configure(bg=BG)
        self.geometry("680x560")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="📰 Was ist neu in Hauskern?", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Wird nach jedem Update einmal angezeigt – später wieder unter ⋯ Mehr → Was ist neu?",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        t = tk.Text(f, bg=CARD, fg=FG, relief="flat", wrap="word", font=("Segoe UI", 10), padx=14, pady=10,
                    highlightthickness=0, cursor="arrow", spacing1=2, spacing3=2)
        t.pack(fill="both", expand=True)
        t.tag_configure("h", font=("Segoe UI Semibold", 12), foreground=ACCENT, spacing1=10, spacing3=4)
        t.tag_configure("b", lmargin1=6, lmargin2=20)
        for v, title, lines in sections:
            t.insert("end", f"Version {v}: {title}\n", "h")
            t.insert("end", changelog.plain(lines) + "\n", "b")
        t.configure(state="disabled")
        ttk.Button(f, text="Alles klar 👍", style="Accent.TButton", command=self.destroy).pack(anchor="e", pady=(10, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        _center(self, master)


# =========================================================================== #
# Erweiterungen
# =========================================================================== #
class PluginsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import plugins as P
        import webadmin
        self.app, self.P = app, P
        self.title("Erweiterungen")
        self.configure(bg=BG)
        self.geometry("800x540")
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🧩 Erweiterungen", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text=webadmin.PLUGIN_WARN, style="Muted.TLabel", wraplength=740, justify="left").pack(
            anchor="w", pady=(2, 4))
        if app.cfg.get("satellite"):
            ttk.Label(f, text="Dieser PC überlässt die Automationen dem Heimserver – Erweiterungen laufen dort. "
                              "Hier kannst du sie nur ansehen.", foreground=WARN, wraplength=740).pack(anchor="w")
        cols = ("status", "desc")
        self.t = ttk.Treeview(f, columns=cols, show="tree headings", height=12)
        self.t.heading("#0", text="Erweiterung")
        self.t.column("#0", width=200)
        self.t.heading("status", text="Status")
        self.t.column("status", width=210)
        self.t.heading("desc", text="Was sie tut")
        self.t.column("desc", width=340)
        self.t.pack(fill="both", expand=True, pady=(8, 0))
        self.l_err = ttk.Label(f, text="", foreground=BAD, wraplength=740, justify="left")
        self.l_err.pack(anchor="w", pady=(4, 0))
        self.t.bind("<<TreeviewSelect>>", lambda e: self._show_err())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="✅ Freigeben & einschalten", command=self._enable).pack(side="left")
        ttk.Button(b, text="Ausschalten", command=self._disable).pack(side="left", padx=6)
        ttk.Button(b, text="Neu laden", command=self._reload).pack(side="left")
        ttk.Button(b, text="Ordner öffnen", command=lambda: open_folder(app.plugins.folder)).pack(side="left", padx=6)
        ttk.Button(b, text="Beispiele hinzufügen", command=self._examples).pack(side="left")
        ttk.Button(b, text="Schließen", style="Accent.TButton", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.render()
        _center(self, master)

    def render(self):
        self.infos = self.app.plugins.scan()
        self.t.delete(*self.t.get_children())
        for i in self.infos:
            self.t.insert("", "end", iid=i["file"], text=i["name"],
                          values=(self.P.STATUS_TEXT.get(i["status"], i["status"]), i["description"]))
        if not self.infos:
            self.t.insert("", "end", text="Noch keine Erweiterungen – „Beispiele hinzufügen“ probieren.")
        self._show_err()

    def _sel(self):
        s = self.t.selection()
        return next((i for i in self.infos if s and i["file"] == s[0]), None)

    def _show_err(self):
        i = self._sel()
        self.l_err.configure(text=("Fehler: " + i["error"]) if i and i["error"] else "")

    def _enable(self):
        i = self._sel()
        if not i:
            return
        if not messagebox.askyesno(APP, f"„{i['name']}“ freigeben und einschalten?\n\nEine Erweiterung ist ein kleines "
                                        "Programm und darf alles, was Hauskern darf. Nur freigeben, wenn du weißt, "
                                        "woher die Datei kommt.", parent=self):
            return
        ok, err = self.app.plugins.enable(i["file"])
        self.app.log("App", i["file"], "Erweiterung freigegeben")
        if not ok:
            messagebox.showwarning(APP, f"„{i['name']}“ startet nicht:\n{err}", parent=self)
        self.render()

    def _disable(self):
        i = self._sel()
        if i:
            self.app.plugins.disable(i["file"])
            self.render()

    def _reload(self):
        i = self._sel()
        if i:
            self.app.plugins.reload(i["file"])
            self.render()

    def _examples(self):
        try:
            added = self.app.plugins.add_examples()
        except ValueError as e:
            messagebox.showwarning(APP, str(e), parent=self)
            return
        self.render()
        messagebox.showinfo(APP, ("Hinzugefügt: " + ", ".join(added) + "\n\nSie sind noch aus – auswählen und "
                                  "„Freigeben & einschalten“.") if added else "Die Beispiele sind schon da.", parent=self)


# =========================================================================== #
# Kamera-Erkennung
# =========================================================================== #
class VisionDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        import vision as V
        import webadmin
        self.app, self.V = app, V
        self.title("Kamera-Erkennung")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🧠 Kamera-Erkennung", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text=webadmin.VISION_HINT, style="Muted.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(2, 10))
        self.l_st = ttk.Label(f, text="", font=("Segoe UI Semibold", 10))
        self.l_st.pack(anchor="w")
        c = app.vision_cfg()
        self.ok = V.available()[0]
        self.v_on = tk.BooleanVar(value=bool(c.get("enabled")))
        self.v_sens = tk.StringVar(value=c.get("sens", "mittel"))
        self.v_cams = {}
        g = ttk.Frame(f)
        g.pack(anchor="w", pady=(8, 0))
        if self.ok:
            ttk.Checkbutton(g, text="Kamera-Erkennung einschalten", variable=self.v_on).grid(row=0, column=0,
                                                                                         columnspan=2, sticky="w")
            ttk.Label(g, text="Empfindlichkeit").grid(row=1, column=0, sticky="w", pady=4)
            ttk.Combobox(g, textvariable=self.v_sens, values=list(V.SENS), state="readonly", width=10).grid(
                row=1, column=1, sticky="w", padx=6)
            for i, cam in enumerate(app.cfg.get("cameras", [])):
                v = tk.BooleanVar(value=bool((c.get("cams") or {}).get(cam["id"], True)))
                ttk.Checkbutton(g, text=f"📷 {cam['name']}", variable=v).grid(row=2 + i, column=0, columnspan=2,
                                                                              sticky="w")
                self.v_cams[cam["id"]] = v
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        self.b_dl = ttk.Button(b, text=f"⬇ Modell laden ({V.MODEL_MB} MB)", command=self._dl)
        if self.ok and not V.model_ok(app.vision.folder):
            self.b_dl.pack(side="left")
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right", padx=6)
        self._tick()
        _center(self, master)

    def _tick(self):
        try:
            self.l_st.configure(text="Status: " + self.app.vision_status())
            self.after(1000, self._tick)
        except tk.TclError:
            pass

    def _dl(self):
        try:
            self.app.vision_download()
        except RuntimeError as e:
            messagebox.showwarning(APP, str(e), parent=self)

    def _ok(self):
        c = dict(self.app.vision_cfg())
        c.update(enabled=bool(self.v_on.get()) and self.ok, sens=self.v_sens.get() or "mittel",
                 cams=dict(c.get("cams") or {}, **{k: bool(v.get()) for k, v in self.v_cams.items()}))
        self.app.cfg["vision"] = c
        self.app.save()
        if c["enabled"] and not self.V.model_ok(self.app.vision.folder) and not self.app.vision_dl:
            try:
                self.app.vision_download()
            except RuntimeError as e:
                messagebox.showwarning(APP, str(e), parent=self)
        self.destroy()


# =========================================================================== #
# Einkaufsliste & Aufgaben
# =========================================================================== #
class ListsDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Einkaufsliste & Aufgaben")
        self.configure(bg=BG)
        self.geometry("520x600")
        self.transient(master)
        self.me = {"name": ""}  # am PC eingetragen – ohne Namen
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.cur = tk.StringVar(value="einkauf")
        for k, txt in (("einkauf", "🛒 Einkaufsliste"), ("aufgaben", "✅ Aufgaben")):
            ttk.Radiobutton(top, text=txt, value=k, variable=self.cur, command=self.render).pack(side="left", padx=(0, 12))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(12, 6))
        self.v_in = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.v_in)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.add())
        ttk.Button(row, text="＋ Eintragen", style="Accent.TButton", command=self.add).pack(side="left", padx=(6, 0))
        self.lb = tk.Listbox(f, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                             font=("Segoe UI", 11), activestyle="none")
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<Double-1>", lambda ev: self.toggle())
        self.lb.bind("<space>", lambda ev: self.toggle())
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="Abhaken / zurück", command=self.toggle).pack(side="left")
        ttk.Button(b, text="Löschen", command=self.remove).pack(side="left", padx=6)
        ttk.Button(b, text="Erledigte löschen", command=lambda: self.op("clear")).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Label(f, text="Doppelklick = abhaken · mehrere auf einmal mit Komma · Siri: „Milch auf die Liste“",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.bind("<Escape>", lambda ev: self.destroy())
        self.items = []
        self.render()
        e.focus_set()
        _center(self, master)

    def render(self):
        lst = self.app.web_lists()["lists"][self.cur.get() or "einkauf"]
        self.items = lst["items"]
        self.lb.delete(0, "end")
        for i in self.items:
            self.lb.insert("end", ("  ✓  " if i["done"] else "  ○  ") + i["text"] + (f"   ({i['by']})" if i["by"] else ""))
            if i["done"]:
                self.lb.itemconfigure("end", fg=MUTED)
        if not self.items:
            self.lb.insert("end", "  Noch nichts eingetragen.")

    def _sel(self):
        s = self.lb.curselection()
        return self.items[s[0]] if s and s[0] < len(self.items) else None

    def op(self, op, **kw):
        ok, msg = self.app.lists_op(dict(kw, op=op, list=self.cur.get() or "einkauf"), self.me)
        if not ok:
            messagebox.showinfo(APP, msg, parent=self)
        self.render()

    def add(self):
        if self.v_in.get().strip():
            self.op("add", text=self.v_in.get())
            self.v_in.set("")

    def toggle(self):
        i = self._sel()
        if i:
            self.op("undone" if i["done"] else "done", id=i["id"])

    def remove(self):
        i = self._sel()
        if i:
            self.op("remove", id=i["id"])


class RemoteDialog(tk.Toplevel):
    """Fernbedienung für Fernseher und TV-Boxen (Sky Q, Vestel/Hanseatic …)."""
    LAYOUT = [["power", "home", "guide", "info"],
              ["ch_up", "up", "vol_up", "source"],
              ["left", "ok", "right", "mute"],
              ["ch_down", "down", "vol_down", "back"],
              ["rew", "play", "pause", "ffw"],
              ["stop", "record"]]
    SHORT = {"power": "⏻", "home": "⌂ Start", "guide": "Programm", "info": "ℹ Info", "ch_up": "Sender +",
             "ch_down": "Sender −", "up": "▲", "down": "▼", "left": "◀", "right": "▶", "ok": "OK", "vol_up": "🔊 +",
             "vol_down": "🔉 −", "mute": "🔇", "source": "Eingang", "back": "↩ Zurück", "rew": "⏪", "play": "▶ Play",
             "pause": "⏸ Pause", "ffw": "⏩", "stop": "⏹ Stopp", "record": "⏺ Aufn."}

    def __init__(self, master, app, ain):
        super().__init__(master)
        self.app, self.ain = app, ain
        d = app._dev(ain) or {}
        self.title(d.get("name", "Fernbedienung"))
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="📺 " + d.get("name", ""), style="H1.TLabel").pack(anchor="w")
        self.l_now = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_now.pack(anchor="w", pady=(0, 8))
        keys = set((d.get("media") or {}).get("keys", []))
        g = ttk.Frame(f)
        g.pack()
        for r, row in enumerate(self.LAYOUT):
            for c, k in enumerate(row):
                if k in keys:
                    ttk.Button(g, text=self.SHORT.get(k, k), width=10,
                               command=lambda k=k: self.press(k)).grid(row=r, column=c, padx=2, pady=2)
        cf = ttk.Frame(f)
        cf.pack(fill="x", pady=(10, 0))
        ttk.Label(cf, text="Sender-Nr.:").pack(side="left")
        self.v_ch = tk.StringVar()
        e = ttk.Entry(cf, textvariable=self.v_ch, width=6)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self.press("channel", self.v_ch.get()))
        ttk.Button(cf, text="Umschalten", command=lambda: self.press("channel", self.v_ch.get())).pack(side="left")
        ttk.Button(cf, text="Schließen", command=self.destroy).pack(side="right")
        self.l_msg = ttk.Label(f, text="", style="Muted.TLabel")
        self.l_msg.pack(anchor="w", pady=(8, 0))
        self._tick()
        _center(self, master)

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            d = self.app._dev(self.ain) or {}
            on = d.get("switch")
            now = (d.get("media") or {}).get("now")
            self.l_now.configure(text=("an" if on else "aus") + (f" · {now}" if now else ""))
            self.after(3000, self._tick)
        except tk.TclError:
            pass

    def press(self, key, value=None):
        d = self.app._dev(self.ain)
        if not d:
            self.l_msg.configure(text="⚠ Gerät gerade nicht da")
            return
        if key == "channel":
            value = "".join(ch for ch in str(value) if ch.isdigit())[:4]
            if not value:
                self.l_msg.configure(text="Bitte eine Sendernummer eintippen")
                return
        self.app.jobs.put(("action", lambda c, dev=dict(d): c.press(dev, key, value)))
        self.l_msg.configure(text=f"→ Sender {value}" if value else "→ " + self.SHORT.get(key, key))


class BugReportDialog(tk.Toplevel):
    """🐞 Problem melden – Familie: direkt als Nachricht an Pascal · Consumer: vorausgefüllte GitHub-Meldung."""

    def __init__(self, master, app, area="allgemein", text=""):
        super().__init__(master)
        import bugreport as BR
        self.app, self.BR = app, BR
        pub = BR.PUBLIC
        self.title("Problem melden")
        self.configure(bg=BG)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="🐞 Problem melden", style="H1.TLabel").pack(anchor="w")
        intro = ("Etwas klappt nicht? Beschreib kurz, was du gemacht hast und was passiert ist. Hauskern öffnet danach "
                 "eine vorausgefüllte Fehlermeldung auf der Projektseite (GitHub) – die ist öffentlich sichtbar. "
                 "IP-Adressen, Namen und Passwörter werden vorher entfernt, und abgeschickt wird erst, wenn du dort "
                 "auf „Create“ klickst." if pub else
                 "Etwas klappt nicht? Beschreib kurz, was du gemacht hast und was passiert ist – die Meldung geht "
                 "direkt an Pascal. Passwörter und Zugangscodes werden vorher geschwärzt.")
        ttk.Label(f, text=intro, style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(2, 10))
        g = ttk.Frame(f)
        g.pack(fill="x")
        self.v_who = tk.StringVar(value="" if pub else
                                  app.cfg.get("bug_name") or os.environ.get("USERNAME") or os.environ.get("USER") or "")
        if not pub:
            ttk.Label(g, text="Dein Name").grid(row=0, column=0, sticky="w", pady=3)
            ttk.Entry(g, textvariable=self.v_who, width=30).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(g, text="Worum geht es?").grid(row=1, column=0, sticky="w", pady=3)
        self.areas = BR.AREAS
        self.v_area = tk.StringVar(value=dict(self.areas).get(area, self.areas[0][1]))
        ttk.Combobox(g, textvariable=self.v_area, values=[n for _, n in self.areas], state="readonly",
                     width=44).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Was ist passiert?").pack(anchor="w", pady=(10, 2))
        self.t = tk.Text(f, height=8, width=70, bg=CARD, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                         font=("Segoe UI", 10), padx=8, pady=6)
        self.t.pack(fill="both", expand=True)
        if text:
            self.t.insert("1.0", text)
        self.v_att = tk.BooleanVar(value=not pub)
        if not pub:
            ttk.Checkbutton(f, text="Fehlerprotokoll mitschicken (hilft sehr beim Finden – ohne Passwörter)",
                            variable=self.v_att).pack(anchor="w", pady=(8, 0))
        self.l_msg = ttk.Label(f, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.l_msg.pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="👁 Vorschau", command=self.preview).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        self.b_send = ttk.Button(b, text="🌐 Auf GitHub melden" if pub else "📨 An Pascal senden",
                                 style="Accent.TButton", command=self.send)
        self.b_send.pack(side="right", padx=6)
        _center(self, master)

    def _vals(self):
        inv = {n: k for k, n in self.areas}
        return self.v_who.get(), inv.get(self.v_area.get(), "allgemein"), self.t.get("1.0", "end").strip()

    def preview(self):
        try:
            title, body = self.app.bug_report(*self._vals(), preview=True)
        except self.BR.ReportError as e:
            self.l_msg.configure(text="⚠ " + str(e), foreground=WARN)
            return
        d = tk.Toplevel(self)
        d.title("So sieht die Meldung aus")
        d.configure(bg=BG)
        t = tk.Text(d, width=90, height=28, bg=CARD, fg=FG, relief="flat", wrap="word", font=("Consolas", 9), padx=10,
                    pady=8)
        t.pack(fill="both", expand=True)
        t.insert("1.0", title + "\n\n" + body + ("\n\n📎 Dazu: Fehlerprotokoll als ZIP" if self.v_att.get() else ""))
        t.configure(state="disabled")

    def send(self):
        try:
            self.app.bug_report(*self._vals(), attach=self.v_att.get(),
                                done=lambda ok, msg: self.app.results.put(("bug_done", (ok, msg))))
        except self.BR.ReportError as e:
            self.l_msg.configure(text="⚠ " + str(e), foreground=WARN)
            return
        self.app._bug_dlg = self
        self.b_send.configure(state="disabled")
        self.l_msg.configure(text="🌐 Öffne die Projektseite …" if self.BR.PUBLIC else "📨 Wird verschickt …",
                             foreground=MUTED)

    def result(self, ok, msg):
        try:
            self.l_msg.configure(text=msg, foreground=OK if ok else WARN)
            self.b_send.configure(state="normal")
        except tk.TclError:
            pass


class BugReceiveDialog(tk.Toplevel):
    """Für Pascal: Fehlermeldungen der Familie per Telegram empfangen + Familien-Paket bauen."""

    def __init__(self, master, app):
        super().__init__(master)
        import push as P
        self.app, self.P = app, P
        self.title("Meldungen empfangen")
        self.configure(bg=BG)
        self.transient(master)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="📨 Fehlermeldungen empfangen", style="H1.TLabel").pack(anchor="w")
        ttk.Label(f, text="Nur für den, der Hauskern betreut (Pascal). Hier legst du fest, dass „🐞 Problem melden“ "
                          "bei dir in Telegram ankommt – auch die Meldungen aus Hauskern bei der Familie.\n\n"
                          "1. In Telegram bei @BotFather einen eigenen Bot nur für Meldungen anlegen (/newbot).\n"
                          "2. Dem neuen Bot „Hallo“ schreiben.\n"
                          "3. Token unten einfügen → „Chat-ID ermitteln“ → „Speichern“.\n"
                          "4. „📦 Paket für die Familie“ erstellen und diese ZIP statt der normalen weitergeben – "
                          "dann landen die Meldungen der Familie auch bei dir.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(4, 10))
        cur = (app.bug_target() or {}).get("telegram") or {}
        g = ttk.Frame(f)
        g.pack(fill="x")
        ttk.Label(g, text="Bot-Token").grid(row=0, column=0, sticky="w", pady=3)
        self.v_tok = tk.StringVar(value=cur.get("token", ""))
        ttk.Entry(g, textvariable=self.v_tok, width=52, show="•").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(g, text="Chat-ID").grid(row=1, column=0, sticky="w", pady=3)
        self.v_chat = tk.StringVar(value=cur.get("chat_id", ""))
        ttk.Entry(g, textvariable=self.v_chat, width=20).grid(row=1, column=1, sticky="w", padx=8)
        self.l = ttk.Label(f, text="✓ Eingerichtet – Meldungen kommen per Telegram" if cur else
                           "Noch nicht eingerichtet – Meldungen laufen bis dahin über ntfy",
                           style="Muted.TLabel", wraplength=560, justify="left")
        self.l.pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="Chat-ID ermitteln", command=self.find).pack(side="left")
        ttk.Button(b, text="Testmeldung", command=self.test).pack(side="left", padx=6)
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="📦 Paket für die Familie", command=self.package).pack(side="right", padx=6)
        ttk.Button(b, text="Speichern", style="Accent.TButton", command=self.save).pack(side="right")
        _center(self, master)

    def _msg(self, text, color=MUTED):
        ui_later(self, lambda: self.l.configure(text=text, foreground=color))

    def find(self):
        tok = self.v_tok.get()
        self._msg("Suche Chat …")

        def run():
            try:
                cid, name = self.P.telegram_find_chat(tok)
                ui_later(self, lambda: self.v_chat.set(cid))
                self._msg(f"✓ Chat gefunden: {name} ({cid}) – jetzt „Speichern“", OK)
            except Exception as e:  # noqa: BLE001
                self._msg(f"⚠ {e}", BAD)
        threading.Thread(target=run, daemon=True).start()

    def test(self):
        import bugreport as BR
        tok, cid = self.v_tok.get(), self.v_chat.get().strip()
        if not (tok.strip() and cid):
            self._msg("⚠ Erst Token eintragen und Chat-ID ermitteln", BAD)
            return
        self._msg("Sende …")

        def run():
            try:
                BR.send_telegram(tok, cid, "🐞 Hauskern: Testmeldung", "So sehen Meldungen aus „Problem melden“ aus. 👍")
                self._msg("✓ Testmeldung verschickt – schau in Telegram", OK)
            except BR.ReportError as e:
                self._msg(f"⚠ {e}", BAD)
        threading.Thread(target=run, daemon=True).start()

    def save(self):
        tok, cid = self.P._clean_token(self.v_tok.get()), self.v_chat.get().strip()
        if not (tok and cid):
            self._msg("⚠ Erst Token eintragen und Chat-ID ermitteln", BAD)
            return
        try:
            self.app.set_bug_target(tok, cid)
        except OSError as e:
            self._msg(f"⚠ Konnte nicht speichern: {e}", BAD)
            return
        self._msg("✓ Gespeichert – deine Meldungen kommen jetzt per Telegram. Für die Familie: „📦 Paket für die "
                  "Familie“ erstellen und weitergeben.", OK)

    def package(self):
        if not self.app.bug_target():
            self._msg("⚠ Erst einrichten und speichern – sonst fehlt im Paket das Meldeziel", BAD)
            return
        try:
            p = self.app.family_package()
        except OSError as e:
            self._msg(f"⚠ {e}", BAD)
            return
        self._msg(f"✓ Paket erstellt: {p}\nDiese Datei der Familie schicken (z. B. per WhatsApp) – sie legen sie in "
                  "Downloads, Hauskern installiert sie selbst. Hinweis: Wer das Paket hat, kann dir über den "
                  "Meldungs-Bot schreiben – also nur in der Familie weitergeben.", OK)
