# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
wizard.py – Einrichtungs-Assistent: führt Schritt für Schritt durch FRITZ!Box, Geräte, Personen, Handy-Zugang
und Extras. Beim ersten Start automatisch, danach jederzeit über die Seitenleiste („✨ Einrichtung“).

Der Assistent legt nichts von selbst an: Geräte, Personen und Nutzer entstehen nur, wenn du sie auswählst.
Automationen legt er nie an.
"""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from dashboard import ACCENT, BAD, BG, CARD, FG, MUTED, OK
from pages import APP, _center, draw_qr, ui_later

STEPS = [("welcome", "Willkommen"), ("router", "Router"), ("fritz", "FRITZ!Box"), ("devices", "Geräte im Netz"),
         ("persons", "Wer wohnt hier?"),
         ("phone", "Handy-Zugang"), ("extras", "Wetter & Push"), ("done", "Fertig")]


class WizardDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Hauskern einrichten")
        self.configure(bg=BG)
        self.geometry("920x620")
        self.minsize(860, 580)
        self.transient(master)
        self.i = 0
        self._dir = 1
        self.fritz_ok = bool(app.client) and app.is_fritz()
        self.detected = None
        self._alive = True
        side = tk.Frame(self, bg=CARD, width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="✨ Einrichtung", bg=CARD, fg=FG, font=("Segoe UI Semibold", 13), anchor="w",
                 padx=18, pady=16).pack(fill="x")
        self.step_lbls = []
        for _, name in STEPS:
            lb = tk.Label(side, text=name, bg=CARD, fg=MUTED, font=("Segoe UI", 11), anchor="w", padx=18, pady=6)
            lb.pack(fill="x")
            self.step_lbls.append(lb)
        main = ttk.Frame(self, padding=(24, 18, 24, 14))
        main.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(main)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(main)
        nav.pack(fill="x", pady=(12, 0))
        self.b_back = ttk.Button(nav, text="‹ Zurück", command=self.back)
        self.b_back.pack(side="left")
        self.b_next = ttk.Button(nav, text="Weiter ›", style="Accent.TButton", command=self.next)
        self.b_next.pack(side="right")
        self.b_skip = ttk.Button(nav, text="Überspringen", command=self.skip)
        self.b_skip.pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.show()
        self.grab_set()
        _center(self, master)

    # ------------------------------------------------------------------ #
    def close(self):
        self._alive = False
        self.app.cfg["wizard_done"] = True
        self.app.save()
        self.destroy()

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def show(self):
        key = STEPS[self.i][0]
        if key == "fritz" and self.app.router_kind() != "fritz":
            self.i = max(0, min(len(STEPS) - 1, self.i + self._dir))  # FRITZ!Box-Schritt nur bei FRITZ!Box
            return self.show()
        self._clear()
        no_fritz = self.app.router_kind() not in ("fritz", "")
        for n, lb in enumerate(self.step_lbls):
            if STEPS[n][0] == "fritz" and no_fritz:
                lb.configure(fg=MUTED, text="   " + STEPS[n][1] + " (–)")
                continue
            lb.configure(fg=FG if n == self.i else (OK if n < self.i else MUTED),
                         text=("● " if n == self.i else ("✓ " if n < self.i else "   ")) + STEPS[n][1])
        self.b_back.configure(state="normal" if self.i else "disabled")
        self.b_skip.configure(state="normal" if key not in ("welcome", "done") else "disabled")
        self.b_next.configure(text="Fertig 🎉" if key == "done" else ("Los geht's ›" if key == "welcome" else "Weiter ›"))
        getattr(self, "step_" + key)()

    def _h(self, title, sub=""):
        ttk.Label(self.body, text=title, style="H1.TLabel").pack(anchor="w")
        if sub:
            ttk.Label(self.body, text=sub, style="Muted.TLabel", justify="left", wraplength=640).pack(anchor="w",
                                                                                                      pady=(4, 12))

    def back(self):
        if self.i:
            self._dir = -1
            self.i -= 1
            self.show()

    def skip(self):
        self._dir = 1
        self.i = min(self.i + 1, len(STEPS) - 1)
        self.show()

    def next(self):
        key = STEPS[self.i][0]
        leave = getattr(self, "leave_" + key, None)
        if leave and leave() is False:
            return
        if key == "done":
            return self.close()
        self.skip()

    # ------------------------------------------------------------------ #
    def step_welcome(self):
        self._h("Hallo 👋", "In ein paar Minuten ist Hauskern eingerichtet. Alles läuft bei dir zu Hause – nichts geht "
                           "in eine fremde Cloud.")
        for icon, txt in (("📡", "Deinen Router erkennen – FRITZ!Box oder ein anderer"),
                          ("🔎", "Geräte im Netz finden – 3D-Drucker, Kameras und mehr"),
                          ("🏠", "Festlegen, wer hier wohnt (Anwesenheit über das Handy)"),
                          ("📱", "Handy-Zugang mit eigener PIN für jeden"),
                          ("🌦", "Wetter und Push-Nachrichten")):
            row = ttk.Frame(self.body)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text=icon, font=("Segoe UI Emoji", 16)).pack(side="left", padx=(0, 10))
            ttk.Label(row, text=txt, font=("Segoe UI", 11)).pack(side="left")
        ttk.Label(self.body, text="Jeden Schritt kannst du überspringen und später nachholen. Hauskern legt dabei nichts "
                                  "von selbst an – keine Geräte, keine Automationen. Du entscheidest.",
                  style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(18, 0))

    # ------------------------------------------------------------------ #
    ROUTER_CHOICES = [
        ("fritz", "FRITZ!Box", "Volle Unterstützung: Geräte & Anwesenheit, Internet, WLAN und Gast-WLAN schalten, "
                               "FRITZ!DECT-Steckdosen und -Heizung, Stromkosten."),
        ("generic", "Anderer Router", "Z. B. Speedport, Vodafone Station, o2 HomeBox, TP-Link, ASUS: Internet-Status, "
                                      "Datenrate, Geräte im Netz und Anwesenheit. WLAN schalten und FRITZ!DECT gehen "
                                      "nur mit FRITZ!Box."),
        ("none", "Erstmal ohne Router", "Nur 3D-Drucker, Kameras, Kalender, Wetter und Druckkosten – Router kannst du "
                                        "später jederzeit einrichten."),
    ]

    def step_router(self):
        a = self.app
        self._h("Dein Router", "Hauskern erkennt deinen Router selbst – oder du wählst aus.")
        self.l_det = ttk.Label(self.body, text="🔎 Erkenne deinen Router …", style="Muted.TLabel")
        self.l_det.pack(anchor="w", pady=(0, 10))
        cur = a.router_kind() or "fritz"
        self.v_rk = tk.StringVar(value=cur)
        for key, title, desc in self.ROUTER_CHOICES:
            box = ttk.Frame(self.body, style="Card.TFrame", padding=10)
            box.pack(fill="x", pady=3)
            ttk.Radiobutton(box, text=title, value=key, variable=self.v_rk).pack(anchor="w")
            ttk.Label(box, text=desc, style="CardSub.TLabel", wraplength=600, justify="left").pack(anchor="w",
                                                                                                padx=(22, 0))
        if self.detected:
            self._show_detect(self.detected, preselect=False)
            return

        def run():
            import router
            try:
                res = router.detect()
            except Exception as e:  # noqa: BLE001
                res = {"kind": "none", "host": "", "name": "", "error": str(e)}
            ui_later(self, lambda: self._show_detect(res, preselect=not a.router_kind()))
        threading.Thread(target=run, daemon=True, name="Router erkennen").start()

    def _show_detect(self, res, preselect=True):
        self.detected = res
        if not self._alive or STEPS[self.i][0] != "router" or not self.l_det.winfo_exists():
            return
        k, name, host = res.get("kind"), res.get("name") or "Router", res.get("host", "")
        if k == "fritz":
            txt = f"✅ Erkannt: {name} ({host}) – volle Unterstützung"
        elif k == "generic":
            txt = f"✅ Erkannt: {name} ({host})" + ("" if res.get("upnp") else
                                                    " · UPnP ist aus – im Router einschalten für Internet-Daten")
        else:
            txt = "Kein Router erkannt – bitte selbst wählen."
        self.l_det.configure(text=txt, foreground=OK if k in ("fritz", "generic") else MUTED)
        if preselect and k in ("fritz", "generic"):
            self.v_rk.set(k)

    def leave_router(self):
        a = self.app
        k = self.v_rk.get()
        res = self.detected or {}
        if k == "fritz":
            a.cfg["router"] = "fritz"
            if res.get("kind") == "fritz" and res.get("host") and not a.cfg.get("user"):
                a.cfg["host"] = res["host"]
            a.save()
        elif k == "generic":
            a.apply_generic_router(res.get("name", "") if res.get("kind") == "generic" else "")
        else:
            a.set_no_router()
        return True

    # ------------------------------------------------------------------ #
    def step_fritz(self):
        a = self.app
        self._h("Deine FRITZ!Box", "Hauskern braucht einen Benutzer der FRITZ!Box. Am besten legst du dafür einen eigenen "
                                   "an: in der FRITZ!Box unter System → FRITZ!Box-Benutzer → Benutzer hinzufügen, mit den "
                                   "Rechten „FRITZ!Box Einstellungen“ und „Smart Home“.")
        self.l_find = ttk.Label(self.body, text="🔎 Suche deine FRITZ!Box …", style="Muted.TLabel")
        self.l_find.pack(anchor="w", pady=(0, 8))
        g = ttk.Frame(self.body)
        g.pack(anchor="w")
        self.v_host = tk.StringVar(value=a.cfg.get("host") or "fritz.box")
        self.v_user = tk.StringVar(value=a.cfg.get("user", ""))
        self.v_pw = tk.StringVar(value=a.password or "")
        for r, (lbl, var, show) in enumerate((("Adresse", self.v_host, ""), ("Benutzername", self.v_user, ""),
                                              ("Passwort", self.v_pw, "•"))):
            ttk.Label(g, text=lbl).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(g, textvariable=var, width=30, show=show).grid(row=r, column=1, sticky="w", pady=4)
        bb = ttk.Frame(self.body)
        bb.pack(anchor="w", pady=(10, 0))
        ttk.Button(bb, text="Verbindung testen", command=self.test_fritz).pack(side="left")
        self.l_test = ttk.Label(bb, text="✅ verbunden" if self.fritz_ok else "", foreground=OK)
        self.l_test.pack(side="left", padx=12)

        def find():
            import fritz_api
            res = fritz_api.find_box()

            def show():
                if not self._alive or not self.l_find.winfo_exists():
                    return
                if res:
                    self.l_find.configure(text=f"✅ Gefunden: {res['model']} unter „{res['host']}“", foreground=OK)
                    if not a.cfg.get("host") or a.cfg.get("host") == "fritz.box":
                        self.v_host.set(res["host"])
                else:
                    self.l_find.configure(text="Keine FRITZ!Box automatisch gefunden – bitte die Adresse selbst "
                                               "eintragen (meist fritz.box oder 192.168.178.1).", foreground=MUTED)
            ui_later(self, show)
        threading.Thread(target=find, daemon=True, name="FRITZ!Box suchen").start()

    def test_fritz(self, then=None):
        host, user, pw = self.v_host.get().strip(), self.v_user.get().strip(), self.v_pw.get()
        if not (host and user and pw):
            self.l_test.configure(text="Bitte Adresse, Benutzer und Passwort eintragen.", foreground=BAD)
            return
        self.l_test.configure(text="prüfe …", foreground=MUTED)

        def run():
            import fritz_api
            try:
                model, err = fritz_api.test_login(host, user, pw), ""
            except Exception as e:  # noqa: BLE001
                model, err = "", str(e)

            def done():
                if not self._alive:
                    return
                if model:
                    self.fritz_ok = True
                    self.app.apply_fritz_login(host, user, pw)
                    if self.l_test.winfo_exists():
                        self.l_test.configure(text=f"✅ verbunden mit {model}", foreground=OK)
                    if then:
                        then()
                elif self.l_test.winfo_exists():
                    hint = " – Benutzer oder Passwort falsch?" if "401" in err or "Anmeld" in err else ""
                    self.l_test.configure(text=f"⚠ {err[:90]}{hint}", foreground=BAD)
            ui_later(self, done)
        threading.Thread(target=run, daemon=True, name="FRITZ!Box testen").start()

    def leave_fritz(self):
        a = self.app
        same = (self.v_host.get().strip(), self.v_user.get().strip(), self.v_pw.get()) == \
            (a.cfg.get("host"), a.cfg.get("user"), a.password)
        if self.fritz_ok and same:
            return True
        if not self.v_user.get().strip() and not self.v_pw.get():
            return True  # nichts eingegeben = überspringen
        self.test_fritz(then=self.skip)
        return False

    # ------------------------------------------------------------------ #
    def step_devices(self):
        a = self.app
        self._h("Geräte im Netz", "Hauskern schaut, was in deinem WLAN/LAN steckt. Wähle ein Gerät aus und richte es ein "
                                  "– oder lass es einfach weg.")
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        self.l_disc = ttk.Label(top, text="", style="Muted.TLabel")
        self.l_disc.pack(side="left")
        ttk.Button(top, text="Nochmal suchen", command=a.run_discovery).pack(side="right")
        self.dtree = ttk.Treeview(self.body, columns=("cat", "st"), show="tree headings", height=11, selectmode="browse")
        self.dtree.heading("#0", text="Gerät")
        self.dtree.heading("cat", text="Art")
        self.dtree.heading("st", text="")
        self.dtree.column("#0", width=280)
        self.dtree.column("cat", width=200)
        self.dtree.column("st", width=150)
        self.dtree.pack(fill="both", expand=True, pady=8)
        self.dtree.bind("<<TreeviewSelect>>", lambda e: self._dsel())
        self.dtree.bind("<Double-1>", lambda e: self.setup_device())
        bb = ttk.Frame(self.body)
        bb.pack(fill="x")
        self.b_dev = ttk.Button(bb, text="Einrichten …", style="Accent.TButton", command=self.setup_device,
                                state="disabled")
        self.b_dev.pack(side="left")
        self.l_dhint = ttk.Label(bb, text="", style="Muted.TLabel")
        self.l_dhint.pack(side="left", padx=10)
        self.ditems = {}
        self._disc_sig = None
        if not a.disc_results and not a.disc_running:
            a.run_discovery()
        self._disc_poll()

    def _kind(self, d):
        cat, vend = d.get("category", ""), (d.get("vendor") or "")
        if cat == "3D-Drucker" or "bambu" in vend.lower():
            return "printer"
        if cat.startswith("Kamera") and "Basisstation" not in cat or "_onvif._tcp" in " ".join(d.get("services", [])):
            return "camera"
        return "device"

    def _disc_poll(self):
        if not self._alive or STEPS[self.i][0] != "devices" or not self.dtree.winfo_exists():
            return
        a = self.app
        self.l_disc.configure(text=("Suche läuft … " + (a.disc_progress or "")) if a.disc_running else
                              f"{len(a.disc_results)} Geräte gefunden")
        known = a.known_ips()
        sig = (len(a.disc_results), a.disc_running, len(known))
        if sig != self._disc_sig:
            self._disc_sig = sig
            t = self.dtree
            t.delete(*t.get_children())
            self.ditems = {}
            groups = [("setup", "🟢 Kann Hauskern einbinden"), ("done", "✅ Schon in Hauskern"),
                      ("other", "⚪ Weitere Geräte")]
            rows = {g: [] for g, _ in groups}
            for d in a.disc_results:
                if d["ip"] in known or d.get("status") == "drin":
                    rows["done"].append(d)
                elif self._kind(d) in ("printer", "camera") or d.get("status") == "lokal":
                    rows["setup"].append(d)
                elif d.get("category") and d["category"] != "Unbekannt":
                    rows["other"].append(d)
            for g, label in groups:
                if not rows[g]:
                    continue
                gid = t.insert("", "end", text=f"{label}  ({len(rows[g])})", open=g != "other")
                for d in rows[g]:
                    iid = "w" + d["ip"]
                    st = known.get(d["ip"], "") if g == "done" else {"printer": "→ Drucker einrichten",
                                                                      "camera": "→ Kamera einrichten"}.get(
                        self._kind(d), "→ hinzufügen")
                    t.insert(gid, "end", iid=iid, text=d["name"] or d["ip"], values=(d.get("category", ""), st))
                    self.ditems[iid] = d
        self.after(1000, self._disc_poll)

    def _dsel(self):
        s = self.dtree.selection()
        d = self.ditems.get(s[0]) if s else None
        if not d:
            self.b_dev.configure(state="disabled")
            self.l_dhint.configure(text="")
            return
        k = self._kind(d)
        self.b_dev.configure(state="normal", text={"printer": "3D-Drucker einrichten …",
                                                   "camera": "Kamera einrichten …"}.get(k, "Zu Hauskern hinzufügen …"))
        self.l_dhint.configure(text={"printer": "Du brauchst den LAN-Zugangscode und die Seriennummer vom Drucker-Display.",
                                     "camera": "Du brauchst das Kamerakonto aus der Hersteller-App (bei Tapo: "
                                               "Kamera-Einstellungen → Erweitert → Kamerakonto)."}.get(k, d.get("hint", "")[:90]))

    def setup_device(self):
        s = self.dtree.selection()
        d = self.ditems.get(s[0]) if s else None
        if not d:
            return
        k = self._kind(d)
        if k == "printer":
            self.app.printer_dialog(prefill={"name": d.get("model") or "3D-Drucker", "ip": d["ip"], "serial": ""})
        elif k == "camera":
            tapo = "tapo" in (d.get("vendor") or "").lower() or "tapo" in (d.get("name") or "").lower()
            self.app.camera_dialog(prefill={"name": d.get("name") or "Kamera", "host": d["ip"], "port": 2020 if tapo else 80,
                                            "user": "", "brand": "tapo" if tapo else ""})
        elif d.get("mac"):
            self.app.add_device_dialog(d["mac"], d["ip"], parent=self)
        else:
            messagebox.showinfo(APP, "Für dieses Gerät kennt Hauskern keine MAC-Adresse – es lässt sich nur im "
                                     "Ping-Monitor beobachten.", parent=self)
        self._disc_sig = None

    # ------------------------------------------------------------------ #
    def step_persons(self):
        self._h("Wer wohnt hier?", "Hauskern erkennt, wer zu Hause ist, wenn das Handy im WLAN ist. Das ist die Grundlage "
                                   "für „Alle weg“ oder „Jemand kommt heim“ – was dann passieren soll, legst du später "
                                   "selbst unter Automationen fest.")
        self.l_pers = ttk.Label(self.body, text="", font=("Segoe UI Semibold", 11))
        self.l_pers.pack(anchor="w")
        ttk.Label(self.body, text="Handys im Netz:", style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
        self.plb = tk.Listbox(self.body, bg=CARD, fg=FG, selectbackground=ACCENT, borderwidth=0, highlightthickness=0,
                              font=("Segoe UI", 10), activestyle="none", height=9)
        self.plb.pack(fill="both", expand=True)
        bb = ttk.Frame(self.body)
        bb.pack(fill="x", pady=(8, 0))
        ttk.Button(bb, text="Als Person hinzufügen …", style="Accent.TButton", command=self.add_person).pack(side="left")
        ttk.Button(bb, text="Alle Personen & Gegenstände …", command=self._persons).pack(side="left", padx=6)
        ttk.Label(self.body, text="Wichtig beim iPhone: Einstellungen → WLAN → (i) neben deinem WLAN → „Private "
                                  "WLAN-Adresse“ auf „Fest“ stellen, sonst erkennt Hauskern es nicht zuverlässig.",
                  style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(10, 0))
        self._fill_persons()

    def _fill_persons(self):
        a = self.app
        ps = a.cfg.get("persons", [])
        names = [("🔑 " if p.get("kind") == "item" else "") + p["name"] for p in ps]
        self.l_pers.configure(text=("Schon eingerichtet: " + ", ".join(names)) if names else "Noch niemand eingerichtet.")
        used = {m.upper() for p in ps for m in p.get("macs", [])}
        try:
            hosts = a.describe_hosts()
        except Exception:  # noqa: BLE001
            hosts = []
        phones = [h for h in hosts if h.get("role") == "person" and h["mac"] not in used]
        phones.sort(key=lambda h: (not h["active"], h["name"].lower()))
        self.plb.delete(0, "end")
        self.pmacs = []
        for h in phones:
            self.plb.insert("end", f"{'●' if h['active'] else '○'}  {h['name']}   ·   {h['category']}")
            self.pmacs.append((h["mac"], h["ip"]))
        if not phones:
            self.plb.insert("end", "Keine weiteren Handys gefunden – die Liste füllt sich, sobald der Router "
                                   "verbunden ist, oder unter „Alle Personen“ selbst auswählen.")

    def add_person(self):
        s = self.plb.curselection()
        if not s or s[0] >= len(self.pmacs):
            return
        mac, ip = self.pmacs[s[0]]
        self.app.add_device_dialog(mac, ip, parent=self)
        self._fill_persons()

    def _persons(self):
        self.app.persons_dialog()
        self._fill_persons()

    # ------------------------------------------------------------------ #
    def step_phone(self):
        a = self.app
        self._h("Hauskern auf dem Handy", "Im Heimnetz (und unterwegs per VPN) bedienst du Hauskern auch vom Handy. Jeder "
                                          "bekommt eine eigene PIN – du legst fest, wer was darf.")
        row = ttk.Frame(self.body)
        row.pack(fill="both", expand=True)
        left = ttk.Frame(row)
        left.pack(side="left", fill="y", anchor="n")
        users = a.cfg.get("web_users") or []
        self.v_wname = tk.StringVar(value="")
        self.v_wpin = tk.StringVar()
        self.v_wpin2 = tk.StringVar()
        if users:
            ttk.Label(left, text="👥 " + ", ".join(("👑 " if u.get("admin") else "") + u["name"] for u in users),
                      font=("Segoe UI Semibold", 11)).pack(anchor="w")
        else:
            ttk.Label(left, text="Dein Zugang (Admin – darf alles):", font=("Segoe UI Semibold", 11)).pack(anchor="w")
            g = ttk.Frame(left)
            g.pack(anchor="w", pady=(6, 0))
            for r, (lbl, var, show) in enumerate((("Dein Name", self.v_wname, ""), ("PIN (4–12 Ziffern)", self.v_wpin, "•"),
                                                  ("PIN wiederholen", self.v_wpin2, "•"))):
                ttk.Label(g, text=lbl).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=3)
                ttk.Entry(g, textvariable=var, width=18, show=show).grid(row=r, column=1, sticky="w", pady=3)
        bb = ttk.Frame(left)
        bb.pack(anchor="w", pady=(12, 0))
        ttk.Button(bb, text="Einschalten & QR-Code zeigen" if not a.web else "QR-Code zeigen", style="Accent.TButton",
                   command=self.enable_phone).pack(side="left")
        ttk.Button(bb, text="👥 Weitere Nutzer & Rechte …", command=self._users).pack(side="left", padx=6)
        ttk.Label(left, text="Familie, Mitbewohner oder Kinder bekommen eigene PINs – z. B. „alles sehen, aber die "
                             "3D-Drucker nicht steuern“.", style="Muted.TLabel", wraplength=380,
                  justify="left").pack(anchor="w", pady=(10, 0))
        self.l_web = ttk.Label(left, text="", style="Muted.TLabel", wraplength=380, justify="left")
        self.l_web.pack(anchor="w", pady=(10, 0))
        right = ttk.Frame(row, style="Card.TFrame", padding=10)
        right.pack(side="right", anchor="n")
        self.wqr = tk.Canvas(right, width=200, height=200, bg="#ffffff", highlightthickness=0)
        self.wqr.pack()
        self.l_wqr = ttk.Label(right, text="", style="CardSub.TLabel", justify="center")
        self.l_wqr.pack(pady=(6, 0))
        self._show_web()

    def _users(self):
        self.app.users_dialog(parent=self)
        self.show()

    def _show_web(self):
        a = self.app
        self.wqr.delete("all")
        urls = a.web_urls() if a.web else []
        if a.web and urls:
            import qr
            draw_qr(self.wqr, qr.encode(urls[0]), 200)
            self.l_wqr.configure(text=f"Mit dem Handy scannen\n{urls[0]}")
            self.l_web.configure(text="Danach in Safari: Teilen → „Zum Home-Bildschirm“ – dann ist Hauskern wie eine "
                                      "App. Fragt Windows nach der Firewall: „Private Netzwerke“ erlauben.")
        else:
            self.wqr.create_text(100, 100, text="noch aus", fill="#777777", font=("Segoe UI", 11))
            self.l_wqr.configure(text="")

    def enable_phone(self):
        a = self.app
        if not (a.cfg.get("web_users") or []):
            name, pin, pin2 = self.v_wname.get().strip(), self.v_wpin.get().strip(), self.v_wpin2.get().strip()
            if not name or not pin:
                messagebox.showwarning(APP, "Bitte Name und PIN eintragen.", parent=self)
                return False
            if not (pin.isdigit() and 4 <= len(pin) <= 12):
                messagebox.showwarning(APP, "Die PIN muss aus 4 bis 12 Ziffern bestehen.", parent=self)
                return False
            if pin != pin2:
                messagebox.showwarning(APP, "Die beiden PINs sind nicht gleich.", parent=self)
                return False
            a.create_first_user(name, pin)
        a.enable_web()
        if not a.web:
            return False
        self.show()
        return True

    def leave_phone(self):
        a = self.app
        if a.web or (a.cfg.get("web_users") and a.cfg.get("web_enabled")):
            return True
        if self.v_wname.get().strip() or self.v_wpin.get().strip():
            return self.enable_phone() is not False
        return True

    # ------------------------------------------------------------------ #
    def step_extras(self):
        a = self.app
        self._h("Wetter & Benachrichtigungen")
        card = ttk.Frame(self.body, style="Card.TFrame", padding=12)
        card.pack(fill="x")
        v = a.weather_view() if hasattr(a, "weather_view") else None
        place = (v or {}).get("place") or ("wird automatisch ermittelt …" if a.cfg.get("weather", {}).get("enabled", True)
                                           else "aus")
        ttk.Label(card, text="🌦 Wetter", style="CardSub.TLabel", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(card, text=f"Ort: {place}" + (f" · {round(v['current']['temp'])}°" if v and v.get("current") else "")
                  + "\nDer Ort wird über deine Internetverbindung ungefähr bestimmt – du kannst ihn auch selbst festlegen.",
                  style="CardSub.TLabel", justify="left").pack(anchor="w", pady=(2, 6))
        ttk.Button(card, text="Ort ändern …", command=lambda: (a.weather_dialog(), self.show())).pack(anchor="w")
        card2 = ttk.Frame(self.body, style="Card.TFrame", padding=12)
        card2.pack(fill="x", pady=(12, 0))
        push = a.cfg.get("push", {}) or {}
        on = push.get("enabled")
        ttk.Label(card2, text="🔔 Push-Nachrichten aufs Handy", style="CardSub.TLabel",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(card2, text=("✅ eingerichtet" if on else "Noch aus. ") +
                  ("" if on else "Mit der kostenlosen App ntfy bekommst du z. B. „Druck fertig“ oder „Internet weg“ "
                                 "aufs Handy – auch unterwegs."), style="CardSub.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", pady=(2, 6))
        ttk.Button(card2, text="Push einrichten …" if not on else "Push-Einstellungen …",
                   command=lambda: (a.push_dialog(), self.show())).pack(anchor="w")
        self.v_upd = tk.BooleanVar(value=a.cfg.get("update_scan", True))
        ttk.Checkbutton(self.body, text="Beim Start nach neuen Hauskern-Versionen im Download-Ordner suchen",
                        variable=self.v_upd, command=lambda: (a.cfg.update(update_scan=self.v_upd.get()), a.save())
                        ).pack(anchor="w", pady=(14, 0))

    # ------------------------------------------------------------------ #
    def step_done(self):
        a = self.app
        self._h("Fertig 🎉", "Hier ist, was eingerichtet ist. Alles kannst du jederzeit ändern – und den Assistenten "
                            "findest du in der Seitenleiste unter „✨ Einrichtung“.")
        c = a.cfg
        users = c.get("web_users") or []
        rk = a.router_kind()
        rname = {"fritz": "FRITZ!Box", "generic": c.get("router_name") or "Router"}.get(rk, "")
        items = [
            (bool(a.client) or self.fritz_ok, "Router", (f"{rname} · verbunden" if (a.client or self.fritz_ok) else
                                                       f"{rname} · noch nicht verbunden") if rk in ("fritz", "generic")
             else "ohne Router – später unter Einstellungen"),
            (bool(c.get("printers")), "3D-Drucker", ", ".join(p["name"] for p in c.get("printers", [])) or "keiner"),
            (bool(c.get("cameras")), "Kameras", ", ".join(x["name"] for x in c.get("cameras", [])) or "keine"),
            (bool(c.get("devices")), "Geräte", f"{len(c.get('devices', []))} hinzugefügt" if c.get("devices") else "keine"),
            (bool(c.get("persons")), "Anwesenheit", ", ".join(p["name"] for p in c.get("persons", [])) or "niemand"),
            (bool(users and c.get("web_enabled")), "Handy-Zugang",
             (", ".join(u["name"] for u in users) if users else "aus") + ("" if c.get("web_enabled") else " (aus)")),
            (bool((c.get("push") or {}).get("enabled")), "Push", "an" if (c.get("push") or {}).get("enabled") else "aus"),
        ]
        g = ttk.Frame(self.body)
        g.pack(anchor="w")
        for r, (ok, name, txt) in enumerate(items):
            ttk.Label(g, text="✅" if ok else "–", foreground=OK if ok else MUTED).grid(row=r, column=0, sticky="w",
                                                                                     padx=(0, 10), pady=3)
            ttk.Label(g, text=name, font=("Segoe UI Semibold", 11)).grid(row=r, column=1, sticky="w", padx=(0, 16))
            ttk.Label(g, text=txt, style="Muted.TLabel").grid(row=r, column=2, sticky="w")
        ttk.Label(self.body, text="Automationen („Wenn alle weg sind, dann …“) legst du ganz nach deinen Wünschen unter "
                                  "Automationen an – Hauskern schaltet nichts von selbst.", style="Muted.TLabel",
                  wraplength=640, justify="left").pack(anchor="w", pady=(16, 0))
