# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
dashboard.py – Kachel-Ansicht „Zuhause“ im Stil von Home Assistant.
"""
import math
import time
import tkinter as tk
from tkinter import filedialog, ttk

BG, CARD, CARD2 = "#111318", "#1b1e25", "#272b34"
CARD_ON = "#1b2c47"
FG, MUTED = "#eceef2", "#8e96a3"
ACCENT, OK, BAD, WARN = "#4f8ff7", "#3ecf8e", "#f2626b", "#e0a33a"
AMBER, HEAT, COOL = "#f6c15a", "#ff7849", "#5ab0ff"
CARD_HOVER, CARD_ON_HOVER = "#20242c", "#20365b"
SHADOW = "#0a0b0e"

TW, TH, GAP = 236, 164, 12  # Kachelgröße


def fnum(v, digits=1):
    return f"{v:.{digits}f}".replace(".", ",")


def fdur_min(m):
    m = int(m or 0)
    h, mm = divmod(m, 60)
    return f"{h} h {mm} min" if h else f"{mm} min"


def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, splinesteps=12, **kw)


# --------------------------------------------------------------------------- #
# Vektor-Icons (keine Bilddateien nötig)
# --------------------------------------------------------------------------- #
def dev_icon(category):
    """Passendes Icon für ein hinzugefügtes Gerät."""
    c = (category or "").lower()
    for words, icon in ((("kamera",), "Kamera"), (("drucker",), "Drucker"), (("rauch", "alarm", "sensor", "melder"), "Sensor"),
                        (("licht", "lampe", "zigbee"), "Lampe"), (("steckdose", "schalter"), "Steckdose"),
                        (("router", "repeater"), "Router"), (("handy", "uhr", "watch"), "Person")):
        if any(w in c for w in words):
            return icon
    return "Geräte"


def draw_icon(c, kind, x, y, color, tags=()):
    """Zeichnet ein Icon in einem 44x44-Feld ab (x, y)."""
    cx, cy = x + 22, y + 22
    k = dict(fill="", outline=color, width=2.2, tags=tags)
    ln = dict(fill=color, width=2.2, capstyle="round", tags=tags)
    if kind == "Steckdose":
        c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, **k)
        c.create_oval(cx - 7, cy - 2, cx - 3, cy + 2, fill=color, outline="", tags=tags)
        c.create_oval(cx + 3, cy - 2, cx + 7, cy + 2, fill=color, outline="", tags=tags)
    elif kind in ("Heizkörperregler",):
        c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, **k)
        c.create_arc(cx - 9, cy - 9, cx + 9, cy + 9, start=-45, extent=270, style="arc", outline=color,
                     width=2.2, tags=tags)
        c.create_line(cx, cy, cx + 6, cy - 6, **ln)
    elif kind == "Sensor":
        c.create_line(cx, cy - 14, cx, cy + 4, fill=color, width=6, capstyle="round", tags=tags)
        c.create_line(cx, cy - 14, cx, cy + 4, fill=CARD, width=2, capstyle="round", tags=tags)
        c.create_oval(cx - 6, cy + 2, cx + 6, cy + 14, fill=color, outline="", tags=tags)
    elif kind in ("Fernseher", "TV-Box"):
        c.create_rectangle(cx - 16, cy - 11, cx + 16, cy + 9, **k)
        if kind == "TV-Box":
            c.create_line(cx - 8, cy - 1, cx + 8, cy - 1, **ln)
        c.create_line(cx - 7, cy + 15, cx + 7, cy + 15, **ln)
        c.create_line(cx, cy + 9, cx, cy + 15, **ln)
    elif kind == "Lampe":
        c.create_oval(cx - 11, cy - 15, cx + 11, cy + 7, **k)
        c.create_line(cx - 5, cy + 11, cx + 5, cy + 11, **ln)
        c.create_line(cx - 3, cy + 15, cx + 3, cy + 15, **ln)
    elif kind == "Gruppe":
        rrect(c, cx - 14, cy - 14, cx + 4, cy + 4, 4, fill="", outline=color, width=2.2, tags=tags)
        rrect(c, cx - 4, cy - 4, cx + 14, cy + 14, 4, fill=CARD, outline=color, width=2.2, tags=tags)
    elif kind == "Schalter" or kind == "Taster":
        rrect(c, cx - 10, cy - 15, cx + 10, cy + 15, 5, fill="", outline=color, width=2.2, tags=tags)
        c.create_oval(cx - 4, cy - 9, cx + 4, cy - 1, fill=color, outline="", tags=tags)
    elif kind == "WLAN":
        for r in (16, 10):
            c.create_arc(cx - r, cy - r + 6, cx + r, cy + r + 6, start=45, extent=90, style="arc",
                         outline=color, width=2.4, tags=tags)
        c.create_oval(cx - 3, cy + 3, cx + 3, cy + 9, fill=color, outline="", tags=tags)
    elif kind == "Internet":
        c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, **k)
        c.create_oval(cx - 6, cy - 14, cx + 6, cy + 14, **k)
        c.create_line(cx - 14, cy, cx + 14, cy, **ln)
    elif kind == "Geräte":
        rrect(c, cx - 15, cy - 12, cx + 15, cy + 6, 3, fill="", outline=color, width=2.2, tags=tags)
        c.create_line(cx - 6, cy + 13, cx + 6, cy + 13, **ln)
        c.create_line(cx, cy + 6, cx, cy + 13, **ln)
    elif kind == "Energie":
        c.create_polygon(cx + 3, cy - 16, cx - 9, cy + 2, cx - 1, cy + 2, cx - 4, cy + 16, cx + 9, cy - 3,
                         cx + 1, cy - 3, fill=color, outline="", tags=tags)
    elif kind == "Ping":
        c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=color, outline="", tags=tags)
        c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, **k)
        c.create_arc(cx - 17, cy - 17, cx + 17, cy + 17, start=20, extent=140, style="arc",
                     outline=color, width=2.2, tags=tags)
    elif kind == "Drucker":
        rrect(c, cx - 15, cy - 15, cx + 15, cy + 15, 4, fill="", outline=color, width=2.2, tags=tags)
        c.create_line(cx - 10, cy - 7, cx + 10, cy - 7, **ln)
        c.create_polygon(cx - 3, cy - 4, cx + 3, cy - 4, cx, cy + 1, fill=color, outline="", tags=tags)
        c.create_line(cx - 9, cy + 9, cx + 9, cy + 9, fill=color, width=3, tags=tags)
    elif kind == "Kamera":
        rrect(c, cx - 16, cy - 9, cx + 6, cy + 10, 4, fill="", outline=color, width=2.2, tags=tags)
        c.create_polygon(cx + 6, cy - 2, cx + 16, cy - 8, cx + 16, cy + 9, cx + 6, cy + 3, fill=color, outline="",
                         tags=tags)
    elif kind == "Person":
        c.create_oval(cx - 7, cy - 15, cx + 7, cy - 1, **k)
        c.create_arc(cx - 14, cy + 1, cx + 14, cy + 29, start=0, extent=180, style="arc", outline=color, width=2.2,
                     tags=tags)
    elif kind == "Kalender":
        rrect(c, cx - 15, cy - 13, cx + 15, cy + 15, 4, fill="", outline=color, width=2.2, tags=tags)
        c.create_line(cx - 15, cy - 5, cx + 15, cy - 5, fill=color, width=2.2, tags=tags)
        c.create_line(cx - 7, cy - 17, cx - 7, cy - 9, fill=color, width=2.4, capstyle="round", tags=tags)
        c.create_line(cx + 7, cy - 17, cx + 7, cy - 9, fill=color, width=2.4, capstyle="round", tags=tags)
        for dx, dy in ((-8, 3), (0, 3), (8, 3), (-8, 10), (0, 10)):
            c.create_rectangle(cx + dx - 2, cy + dy - 2, cx + dx + 2, cy + dy + 2, fill=color, outline="", tags=tags)
    elif kind == "Router":
        rrect(c, cx - 16, cy - 2, cx + 16, cy + 12, 4, fill="", outline=color, width=2.2, tags=tags)
        c.create_line(cx - 9, cy - 2, cx - 13, cy - 15, fill=color, width=2.2, capstyle="round", tags=tags)
        c.create_line(cx + 9, cy - 2, cx + 13, cy - 15, fill=color, width=2.2, capstyle="round", tags=tags)
        for dx in (-9, -3, 3):
            c.create_oval(cx + dx - 2, cy + 3, cx + dx + 2, cy + 7, fill=color, outline="", tags=tags)
    elif kind == "Telefon":
        c.create_arc(cx - 15, cy - 15, cx + 15, cy + 15, start=200, extent=140, style="arc", outline=color, width=5,
                     tags=tags)
        c.create_oval(cx - 16, cy - 2, cx - 8, cy + 8, fill=color, outline="", tags=tags)
        c.create_oval(cx + 8, cy - 2, cx + 16, cy + 8, fill=color, outline="", tags=tags)
    elif kind == "Heizung":
        rrect(c, cx - 15, cy - 12, cx + 15, cy + 10, 3, fill="", outline=color, width=2.2, tags=tags)
        for dx in (-7, 0, 7):
            c.create_line(cx + dx, cy - 7, cx + dx, cy + 5, **ln)
        c.create_line(cx - 11, cy + 10, cx - 11, cy + 15, **ln)
        c.create_line(cx + 11, cy + 10, cx + 11, cy + 15, **ln)
    elif kind == "Batterie":
        rrect(c, cx - 16, cy - 9, cx + 12, cy + 9, 3, fill="", outline=color, width=2.2, tags=tags)
        c.create_rectangle(cx + 13, cy - 4, cx + 17, cy + 4, fill=color, outline="", tags=tags)
        c.create_rectangle(cx - 12, cy - 5, cx - 2, cy + 5, fill=color, outline="", tags=tags)
    elif kind == "Szene":
        c.create_oval(cx - 15, cy - 15, cx + 15, cy + 15, **k)
        c.create_polygon(cx - 5, cy - 8, cx + 9, cy, cx - 5, cy + 8, fill=color, outline="", tags=tags)
    else:
        c.create_oval(cx - 12, cy - 12, cx + 12, cy + 12, **k)


def weather_kind(code, day=True):
    code = int(code or 0)
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code >= 51:
        return "rain"
    if code in (45, 48):
        return "fog"
    if code == 3:
        return "cloud"
    if code in (1, 2):
        return "partly" if day else "cloud"
    return "sun" if day else "moon"


def draw_weather(c, kind, cx, cy, r=18, tags=()):
    """Wetter-Symbol aus einfachen Formen (Emoji sind unter Windows in Tk nur einfarbig)."""
    sun, cloud, rain, snow = "#f6c15a", "#c9ced8", "#5ab0ff", "#e8f2ff"

    def cloud_at(x, y, s, col=cloud):
        c.create_oval(x - s, y - s * .35, x - s * .1, y + s * .5, fill=col, outline="", tags=tags)
        c.create_oval(x - s * .55, y - s * .8, x + s * .45, y + s * .4, fill=col, outline="", tags=tags)
        c.create_oval(x - s * .05, y - s * .45, x + s, y + s * .5, fill=col, outline="", tags=tags)
        c.create_rectangle(x - s * .55, y + s * .05, x + s * .55, y + s * .5, fill=col, outline="", tags=tags)

    if kind in ("sun", "partly"):
        sx, sy, rr = (cx, cy, r * .55) if kind == "sun" else (cx - r * .35, cy - r * .35, r * .45)
        for i in range(8):
            a = i * math.pi / 4
            c.create_line(sx + math.cos(a) * rr * 1.35, sy + math.sin(a) * rr * 1.35, sx + math.cos(a) * rr * 1.75,
                          sy + math.sin(a) * rr * 1.75, fill=sun, width=2.5, capstyle="round", tags=tags)
        c.create_oval(sx - rr, sy - rr, sx + rr, sy + rr, fill=sun, outline="", tags=tags)
        if kind == "partly":
            cloud_at(cx + r * .2, cy + r * .2, r * .75)
    elif kind == "moon":
        c.create_oval(cx - r * .6, cy - r * .6, cx + r * .6, cy + r * .6, fill="#e6e1c5", outline="", tags=tags)
        c.create_oval(cx - r * .3, cy - r * .8, cx + r * .8, cy + r * .3, fill=CARD, outline="",
                      tags=tags)
    elif kind == "fog":
        for i, w in enumerate((1, .8, 1)):
            y = cy - r * .4 + i * r * .4
            c.create_line(cx - r * w, y, cx + r * w, y, fill=cloud, width=3, capstyle="round", tags=tags)
    else:
        cloud_at(cx, cy - r * .2, r * .85, "#9aa1ad" if kind in ("rain", "storm", "snow") else cloud)
        if kind == "rain":
            for dx in (-.45, 0, .45):
                c.create_line(cx + dx * r, cy + r * .45, cx + dx * r - 3, cy + r * .85, fill=rain, width=2.4,
                              capstyle="round", tags=tags)
        elif kind == "snow":
            for dx in (-.45, 0, .45):
                c.create_oval(cx + dx * r - 2.5, cy + r * .55, cx + dx * r + 2.5, cy + r * .55 + 5, fill=snow,
                              outline="", tags=tags)
        elif kind == "storm":
            c.create_polygon(cx, cy + r * .3, cx - r * .25, cy + r * .75, cx, cy + r * .7, cx - r * .1, cy + r,
                             cx + r * .25, cy + r * .55, cx, cy + r * .6, fill=sun, outline="", tags=tags)


def blend(c1, c2, a):
    """Mischt zwei Hex-Farben (a = Anteil von c1)."""
    r1, g1, b1 = (int(c1[i:i + 2], 16) for i in (1, 3, 5))
    r2, g2, b2 = (int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(r1 * a + r2 * (1 - a)), round(g1 * a + g2 * (1 - a)), round(b1 * a + b2 * (1 - a)))


def sparkline(c, x1, y1, x2, y2, values, color, bg=CARD):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or max(1.0, abs(hi) * 0.1)
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        if v is None:
            continue
        pts += [x1 + (x2 - x1) * i / max(1, n - 1), y2 - (y2 - y1) * (v - lo) / span]
    if len(pts) >= 4:
        c.create_polygon(pts + [pts[-2], y2, pts[0], y2], fill=blend(color, bg, 0.18), outline="")
        c.create_line(*pts, fill=color, width=2, smooth=True)


# --------------------------------------------------------------------------- #
# Kachel
# --------------------------------------------------------------------------- #
class Tile(tk.Canvas):
    """Eine Kachel. `model` ist ein dict, das bei jedem Update ersetzt wird."""

    def __init__(self, master, dash, key, cols=1, rows=1):
        self.cols, self.rows = cols, rows
        w, h = TW * cols + GAP * (cols - 1), TH * rows + GAP * (rows - 1)
        super().__init__(master, width=w, height=h, bg=BG, highlightthickness=0, cursor="hand2")
        self.dash, self.key, self.w, self.h = dash, key, w, h
        self.model = {}
        self._hover, self._active = False, False
        self.bind("<Button-3>", self._menu)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    def _set_hover(self, on):
        """Kachel hellt beim Drüberfahren sanft auf."""
        self._hover = on
        try:
            self.itemconfigure("base", fill=self._fill(), outline=self._outline())
        except tk.TclError:
            pass

    def _fill(self):
        if self._active:
            return CARD_ON_HOVER if self._hover else CARD_ON
        return CARD_HOVER if self._hover else CARD

    def _outline(self):
        if self._active:
            return "#3d64a3" if self._hover else "#2f4f80"
        return "#363b46" if self._hover else "#252932"

    def set(self, model):
        self.model = model
        self.redraw()

    # Bild-Unterstützung (eigene Bilder pro Kachel)
    def _icon(self, kind, color, active):
        x, y = 14, 14
        img = self.dash.image_for(self.key)
        tint = "#3a3520" if active and color == AMBER else ("#203048" if active else CARD2)
        self.create_oval(x - 2, y - 2, x + 46, y + 46, fill=tint, outline="")
        if img:
            self.create_image(x + 22, y + 22, image=img)
        else:
            draw_icon(self, kind, x, y, color)

    def _base(self, active=False, offline=False):
        self.delete("all")
        self._active = active
        rrect(self, 2, 4, self.w - 1, self.h - 1, 18, fill=SHADOW, outline="")  # weicher Schatten
        rrect(self, 1, 1, self.w - 2, self.h - 3, 18, fill=self._fill(), outline=self._outline(), tags=("base",))
        self.create_line(18, 2, self.w - 19, 2, fill="#2a3a58" if active else "#262a33")  # feine Lichtkante oben

    def _toggle(self, x, y, on, enabled=True):
        """Schalter rechts oben; Klickfläche bekommt den Tag 'toggle'."""
        col = ACCENT if on else "#3b404b"
        if not enabled:
            col = "#2c3038"
        rrect(self, x, y, x + 46, y + 26, 13, fill=col, outline="", tags=("toggle", "track"))
        kx = x + 33 if on else x + 13
        self.create_oval(kx - 9, y + 4, kx + 9, y + 22, fill="#ffffff" if enabled else MUTED, outline="",
                         tags=("toggle", "knob"))
        self._tg = (on, enabled)

    def _slide_toggle(self):
        """Knopf gleitet sofort zur neuen Seite – die echte Rückmeldung zeichnet danach neu."""
        on, enabled = getattr(self, "_tg", (None, False))
        if on is None or not enabled:
            return
        self._tg = (not on, enabled)
        dist = -20 if on else 20
        steps = (0.18, 0.42, 0.66, 0.84, 0.95, 1.0)  # sanft abbremsen
        if not on:
            self.itemconfigure("track", fill=ACCENT)

        def go(i=0, done=0.0):
            if i >= len(steps):
                if on:
                    self.itemconfigure("track", fill="#3b404b")
                return
            dx = dist * (steps[i] - done)
            self.move("knob", dx, 0)
            self.after(16, go, i + 1, steps[i])
        try:
            go()
        except tk.TclError:
            pass

    def _button(self, x, y, text, tag, color=CARD2, w=None):
        w = w or 12 + 7 * len(text)
        rrect(self, x, y, x + w, y + 28, 10, fill=color, outline="", tags=(tag, "btn"))
        self.create_text(x + w / 2, y + 14, text=text, fill=FG, font=("Segoe UI Semibold", 9), tags=(tag, "btn"))
        return x + w + 8

    def _hit(self, event, tag):
        return tag in [t for i in self.find_overlapping(event.x - 1, event.y - 1, event.x + 1, event.y + 1)
                       for t in self.gettags(i)]

    def _click(self, event):
        for tag in ("toggle", "minus", "plus", "pause", "resume", "stop", "light", "camera", "run", "mplay", "mpause",
                    "mvolup", "mvoldown", "remote"):
            if self._hit(event, tag):
                if tag == "toggle":
                    self._slide_toggle()
                self.dash.on_action(self.key, tag, self.model)
                return
        self.dash.on_action(self.key, "open", self.model)

    def _menu(self, event):
        m = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG, activebackground=ACCENT, bd=0)
        m.add_command(label="Eigenes Bild wählen …", command=lambda: self.dash.choose_image(self.key))
        if self.dash.image_for(self.key):
            m.add_command(label="Bild zurücksetzen", command=lambda: self.dash.reset_image(self.key))
        m.add_command(label="Umbenennen …", command=lambda: self.dash.rename(self.key))
        rooms = tk.Menu(m, tearoff=0, bg=CARD2, fg=FG, activebackground=ACCENT, bd=0)
        cur = self.dash.room_of(self.key)
        for r in self.dash.rooms():
            rooms.add_command(label=("✓ " if r == cur else "   ") + r, command=lambda r=r: self.dash.set_room(self.key, r))
        if self.dash.rooms():
            rooms.add_separator()
        rooms.add_command(label="Neuer Raum …", command=lambda: self.dash.new_room(self.key))
        if cur:
            rooms.add_command(label="Aus Raum entfernen", command=lambda: self.dash.set_room(self.key, None))
        m.add_cascade(label="Raum", menu=rooms)
        m.add_command(label="Kachel ausblenden", command=lambda: self.dash.hide(self.key))
        if self.key.startswith("printer:"):
            m.add_separator()
            m.add_command(label="🧵 Filament-Lager …", command=lambda: self.dash.on_action(self.key, "filament", self.model))
            tl = (self.model.get("p") or {}).get("tl") or {}
            if tl.get("mode") == "ask":
                on = tl.get("armed") or tl.get("rec") is not None
                m.add_separator()
                m.add_command(label="🎬 Zeitraffer für diesen Druck " + ("ausschalten" if on else "einschalten"),
                              command=lambda: self.dash.on_action(self.key, "timelapse", dict(self.model, tl_on=not on)))
        if self.key.startswith("dev:"):
            m.add_separator()
            m.add_command(label="Gerät bearbeiten …", command=lambda: self.dash.on_action(self.key, "edit", self.model))
            m.add_command(label="Gerät entfernen", command=lambda: self.dash.on_action(self.key, "remove", self.model))
        if self.key.startswith("wol:"):
            m.add_separator()
            m.add_command(label="Aufweck-Kachel entfernen", command=lambda: self.dash.on_action(self.key, "remove", self.model))
        if self.key.startswith("printer:"):
            m.add_separator()
            m.add_command(label="Drucker bearbeiten …", command=lambda: self.dash.on_action(self.key, "edit", self.model))
            m.add_command(label="Drucker entfernen", command=lambda: self.dash.on_action(self.key, "remove", self.model))
        m.tk_popup(event.x_root, event.y_root)

    # ---------------- Zeichnen je Typ ----------------
    def redraw(self):
        t = self.model.get("type")
        getattr(self, "_draw_" + t, self._draw_generic)()

    def _title(self, name, sub, y=70, sub_color=MUTED):
        name = self.dash.name_for(self.key, name)
        name = name if len(name) <= 26 else name[:25] + "…"
        self.create_text(16, y, text=name, anchor="w", fill=FG, font=("Segoe UI Semibold", 11))
        self.create_text(16, y + 21, text=sub, anchor="w", fill=sub_color, font=("Segoe UI", 9))

    def _big(self, text, color=FG, x=16, y=None):
        self.create_text(x, y or self.h - 26, text=text, anchor="w", fill=color, font=("Segoe UI Semibold", 17))

    def _draw_generic(self):
        self._base()
        self._title(self.model.get("name", "?"), "")

    def _draw_dect(self):
        d = self.model["dev"]
        on = d["switch"] if d["switch"] is not None else d["simpleonoff"]
        present = d["present"]
        kind = d["kind"]
        active = bool(on) and present
        self._base(active=active)
        color = (AMBER if active else MUTED) if on is not None else (HEAT if d["hkr"] else COOL)
        if not present:
            color = "#555a66"
        sens_txt, mood = "", "mut"
        if d.get("sensor"):
            from providers import sensor_text
            sens_txt, mood = sensor_text(d["sensor"])
            sens_txt = sens_txt.lstrip("💧🔥🚨 ")  # Emojis zeigt Windows im Kachel-Bild nicht immer an
            if present and mood in ("bad", "warn"):
                color = BAD if mood == "bad" else AMBER
        icon_kind = kind if kind in ("Steckdose", "Heizkörperregler", "Sensor", "Lampe", "Gruppe", "Schalter", "Taster",
                                     "Fernseher", "TV-Box") \
            else ("Steckdose" if kind == "Messgerät" else "Taster" if kind == "Taster" else "Sensor")
        self._icon(icon_kind, color, active or (present and mood == "bad"))
        if on is not None or d["is_group"]:
            self._toggle(self.w - 62, 22, bool(on), enabled=present)
        sub = "nicht erreichbar" if not present else ("An" if on else "Aus") if on is not None else kind
        if present and d.get("media") and on and d["media"].get("now"):
            sub = d["media"]["now"]  # was gerade läuft
        elif present and d.get("vendor") and d["vendor"] not in ("AVM", d["name"]):
            sub += f" · {d['vendor']}"
        if present and d["batterylow"]:
            sub += " · Batterie schwach"
        self._title(d["name"], sub, sub_color=BAD if not present else (WARN if d["batterylow"] else MUTED))
        if not present:
            return
        hist = self.dash.power_hist.get(d["ain"])
        if d.get("media"):
            self._big("an" if on else "aus", AMBER if on else MUTED)
            x = self.w - 132
            if on and d["media"].get("box") == "sky":
                x = self._button(x, self.h - 40, "❚❚", "mpause", w=30)
                x = self._button(x, self.h - 40, "▶", "mplay", w=30)
            elif on and "vol_up" in d["media"].get("keys", []):
                x = self._button(x, self.h - 40, "−", "mvoldown", w=30)
                x = self._button(x, self.h - 40, "+", "mvolup", w=30)
            else:
                x += 76
            self._button(x, self.h - 40, "···", "remote", w=40)
            return
        if sens_txt and d["power_w"] is None and not d["hkr"]:
            self._big(sens_txt, {"bad": BAD, "warn": AMBER, "ok": OK}.get(mood, MUTED))
            if d["temp"] is not None:
                self.create_text(self.w - 16, self.h - 26, text=f"{fnum(d['temp'])} °C", anchor="e", fill=COOL,
                                 font=("Segoe UI Semibold", 11))
        elif d.get("level") is not None and d["power_w"] is None and not d["hkr"]:
            self._big(f"{d['level']} %" if on else "aus", AMBER if on else MUTED)
            x = self.w - 126
            self._button(x, self.h - 40, "−", "minus", w=30)
            self.create_text(x + 55, self.h - 26, text="☀", fill=FG, font=("Segoe UI", 11))
            self._button(x + 80, self.h - 40, "+", "plus", w=30)
        elif d["power_w"] is not None:
            self._big(f"{fnum(d['power_w'])} W", AMBER if d["power_w"] > 0.5 else FG)
            if d["energy_kwh"] is not None:
                self.create_text(self.w - 16, self.h - 18, text=f"{fnum(d['energy_kwh'], 2)} kWh gesamt",
                                 anchor="e", fill=MUTED, font=("Segoe UI", 8))
            if hist:
                sparkline(self, 120, self.h - 58, self.w - 16, self.h - 32, list(hist), AMBER)
        elif d["hkr"]:
            ist, soll = d["hkr"]["ist"], d["hkr"]["soll"]
            ist_t = f"{fnum(ist)} °C" if isinstance(ist, float) else "–"
            self._big(ist_t, HEAT)
            soll_t = soll if isinstance(soll, str) else (f"{fnum(soll)}°" if soll is not None else "–")
            pend = self.dash.pending_temp.get(d["ain"])
            if pend is not None:
                soll_t = f"{fnum(pend)}°"
            x = self.w - 126
            self._button(x, self.h - 40, "−", "minus", w=30)
            self.create_text(x + 55, self.h - 26, text=soll_t, fill=FG, font=("Segoe UI Semibold", 11))
            self._button(x + 80, self.h - 40, "+", "plus", w=30)
            if d["hkr"]["window_open"]:
                self.create_text(self.w - 16, 80, text="Fenster offen", anchor="e", fill=COOL, font=("Segoe UI", 8))
            if d["battery"]:
                self.create_text(self.w - 16, 58, text=f"Batterie {d['battery']} %", anchor="e", fill=MUTED,
                                 font=("Segoe UI", 8))
        elif d["temp"] is not None:
            txt = f"{fnum(d['temp'])} °C"
            if d["humidity"] is not None:
                txt += f" · {d['humidity']} %"
            self._big(txt, COOL)

    def _draw_energy(self):
        m = self.model
        self._base()
        self._icon("Energie", AMBER, True)
        sub = f"{m['count']} Messsteckdose" + ("" if m["count"] == 1 else "n")
        if m.get("month_eur") is not None:
            sub = f"Monat {fnum(m['month_eur'], 2)} € · ≈ {fnum(m['prog_eur'], 0)} €" + (" ⚠" if m.get("over_budget") else "")
        self._title("Verbrauch jetzt", sub, sub_color=BAD if m.get("over_budget") else MUTED)
        self._big(f"{fnum(m['power'])} W", AMBER)
        sparkline(self, 120, self.h - 58, self.w - 16, self.h - 32, list(self.dash.total_hist), AMBER)

    def _draw_internet(self):
        m = self.model
        online = m.get("online")
        self._base()
        self._icon("Internet", OK if online else BAD, bool(online))
        self._title("Internet", ("Online" if online else "Offline") + (f" · {m['ext_ip']}" if m.get("ext_ip") else ""),
                    sub_color=OK if online else BAD)
        if "down" in m:
            self.create_text(16, self.h - 36, text=f"↓ {m['down']}", anchor="w", fill=ACCENT,
                             font=("Segoe UI Semibold", 12))
            self.create_text(16, self.h - 15, text=f"↑ {m['up']}", anchor="w", fill="#f0883e",
                             font=("Segoe UI Semibold", 10))
        sparkline(self, 130, self.h - 58, self.w - 16, self.h - 18, list(self.dash.down_hist), ACCENT)

    def _draw_wlan(self):
        w = self.model["wlan"]
        self._base(active=w["enabled"])
        self._icon("WLAN", ACCENT if w["enabled"] else MUTED, w["enabled"])
        self._toggle(self.w - 62, 22, w["enabled"])
        self._title(w["ssid"] or f"WLAN {w['index']}",
                    (f"{w['box_name']} · " if w.get("box_name") else "") + f"{w['band']} · Kanal {w['channel'] or '–'}")
        if w["clients"] is not None:
            self._big(f"{w['clients']} Geräte" if w["clients"] != 1 else "1 Gerät", FG if w["enabled"] else MUTED)

    def _draw_guest(self):
        m = self.model
        w = m["wlan"]
        on = w["enabled"]
        self._base(active=on)
        self._icon("WLAN", AMBER if on else MUTED, on)
        self._toggle(self.w - 62, 22, on)
        sub = m.get("ssid") or w["ssid"]
        if on and m.get("off_at"):
            sub += " · aus um " + time.strftime("%H:%M", time.localtime(m["off_at"]))
        self._title("Gast-WLAN", sub)
        if on:
            n = w.get("clients")
            self._big("an" if n is None else (f"{n} Gäste" if n != 1 else "1 Gast"), AMBER, y=self.h - 30)
        else:
            self._big("aus", MUTED, y=self.h - 30)
        if m.get("has_qr"):
            self._button(self.w - 82, self.h - 44, "▣ QR-Code", "open", color=CARD2, w=70)

    def _draw_battery(self):
        m = self.model
        w = m["items"][0]
        col = BAD if w["low"] or w["pct"] <= 20 else (WARN if w["pct"] <= 40 else OK)
        self._base()
        self._icon("Batterie", col, False)
        self._title("Batterien", f"{m['count']} Geräte" + (f" · {m['low']} schwach" if m["low"] else ""),
                    sub_color=BAD if m["low"] else MUTED)
        nm = w["name"] if len(w["name"]) <= 18 else w["name"][:17] + "…"
        self._big(f"{nm} {w['pct']} %", col, y=self.h - 44)
        self.create_text(16, self.h - 20, text=w["left_text"], anchor="w", fill=MUTED, font=("Segoe UI", 9))

    def _draw_calls(self):
        import calls as CL
        m = self.model
        n = m.get("missed_today") or 0
        self._base()
        self._icon("Telefon", BAD if n else MUTED, bool(n))
        self._title("Anrufe", f"heute {n} verpasst" if n else "nichts verpasst", sub_color=BAD if n else MUTED)
        y = self.h - 64
        for c in (m.get("items") or [])[:3]:
            col = BAD if CL.is_missed(c) else FG
            nm = CL.who(c)
            nm = nm if len(nm) <= 16 else nm[:15] + "…"
            self.create_text(16, y, text=f"{CL.ICON.get(c['type'], '')} {nm}", anchor="w", fill=col, font=("Segoe UI", 9))
            self.create_text(self.w - 14, y, text=CL.when(c["ts"]), anchor="e", fill=MUTED, font=("Segoe UI", 8))
            y += 18
        if not m.get("items"):
            self.create_text(16, y, text="Noch keine Anrufe", anchor="w", fill=MUTED, font=("Segoe UI", 9))

    def _draw_plugin(self):
        m = self.model
        self._base()
        self.create_text(16, 22, text=f"{m.get('icon') or '🧩'}  {m.get('title', '')}", anchor="w", fill=FG,
                         font=("Segoe UI Semibold", 11))
        val = m.get("value", "")
        self.create_text(16, self.h / 2 + 4, text=val if len(val) <= 22 else val[:21] + "…", anchor="w",
                         fill=m.get("color") or FG, font=("Segoe UI Semibold", 16))
        sub = m.get("sub", "")
        if sub:
            self.create_text(16, self.h - 20, text=sub if len(sub) <= 38 else sub[:37] + "…", anchor="w", fill=MUTED,
                             font=("Segoe UI", 9))
        if m.get("tap"):
            self.create_text(self.w - 14, 22, text="›", anchor="e", fill=MUTED, font=("Segoe UI", 14))

    def _draw_lists(self):
        m = self.model
        self._base()
        self.create_text(16, 22, text=f"🛒  Einkaufsliste · {m.get('shop_n', 0)}", anchor="w", fill=FG,
                         font=("Segoe UI Semibold", 11))
        y = 46
        for t in (m.get("shop") or [])[:4]:
            self.create_text(22, y, text="○  " + (t if len(t) <= 26 else t[:25] + "…"), anchor="w", fill=FG,
                             font=("Segoe UI", 10))
            y += 18
        if not m.get("shop"):
            self.create_text(22, y, text="nichts offen 👍", anchor="w", fill=MUTED, font=("Segoe UI", 10))
            y += 18
        if m.get("todo_n"):
            self.create_text(16, self.h - 18, text=f"✅ Aufgaben: {m['todo_n']} offen", anchor="w", fill=MUTED,
                             font=("Segoe UI", 9))

    def _draw_heating(self):
        self._base()
        self._icon("Heizung", HEAT, False)
        self._title("Heizungs-Check", "Hinweise aus dem Temperaturverlauf")
        self._big("ansehen ›", FG, y=self.h - 30)

    def _draw_hosts(self):
        m = self.model
        self._base()
        self._icon("Geräte", ACCENT, False)
        self._title("Geräte im Heimnetz", f"{m['lan']} per LAN · {m['wlan']} per WLAN")
        self._big(f"{m['online']} online")

    def _draw_ping(self):
        m = self.model
        ok = m.get("ok")
        self._base()
        self._icon("Ping", OK if ok else (BAD if ok is False else MUTED), bool(ok))
        sub = "erreichbar" if ok else ("nicht erreichbar" if ok is False else "prüfe …")
        self._title(m["name"], f"{m['host']} · {sub}", sub_color=OK if ok else (BAD if ok is False else MUTED))
        if ok and m.get("rtt"):
            self._big(m["rtt"])

    def _draw_scene(self):
        m = self.model
        busy = m.get("busy")
        self._base(active=busy)
        self._icon("Szene", ACCENT if busy else "#b392f0", busy)
        self._title(m["name"], "läuft …" if busy else f"Szene · {m['count']} Aktion(en)")
        self._button(16, self.h - 42, "▶ Ausführen", "run", color=ACCENT if not busy else CARD2, w=104)

    def _draw_person(self):
        m = self.model
        home = m.get("home")
        self._base(active=bool(home))
        col = OK if home else (MUTED if home is False else "#555a66")
        item = m.get("item")
        self._icon("Geräte" if item else "Person", col, bool(home))
        if item:
            status = "ist da" if home else ("weg" if home is False else "wird ermittelt …")
        else:
            status = "zu Hause" if home else ("unterwegs" if home is False else "wird ermittelt …")
        self._title(m["name"], m.get("devices", ""))
        self._big(("✔ " if item else "🏠 ") * bool(home) + status, OK if home else MUTED)
        if m.get("since"):
            self.create_text(self.w - 16, self.h - 18, text="seit " + m["since"], anchor="e", fill=MUTED,
                             font=("Segoe UI", 8))

    def _draw_agenda(self):
        m = self.model
        self._base()
        self._icon("Kalender", "#ff6b6b", False)
        evs = m.get("events", [])
        self._title("Termine", m.get("status") or (f"{m.get('today', 0)} heute" if evs else "nichts geplant 🎉"))
        y, last_day = 112, None
        for e in evs:
            if y > self.h - 18:
                break
            d = e["day"]
            if d != last_day:
                self.create_text(16, y, text=d, anchor="w", fill=MUTED, font=("Segoe UI Semibold", 9))
                y += 20
                last_day = d
                if y > self.h - 18:
                    break
            self.create_rectangle(16, y - 7, 19, y + 7, fill=e["color"] or ACCENT, outline="")
            self.create_text(26, y, text=e["time"], anchor="w", fill=MUTED, font=("Segoe UI", 9))
            t = e["title"] if len(e["title"]) <= 42 else e["title"][:41] + "…"
            self.create_text(112, y, text=t, anchor="w", fill=FG, font=("Segoe UI", 10))
            y += 21

    def _draw_weather(self):
        m = self.model
        self._base()
        cur = m.get("current")
        if not cur:
            self._icon("Generic", MUTED, False)
            self._title("Wetter", m.get("status") or "wird geladen …")
            return
        draw_weather(self, weather_kind(cur["code"], cur.get("day", True)), 46, 46, 24)
        self.create_text(86, 34, text=f"{round(cur['temp'])}°", anchor="w", fill=FG, font=("Segoe UI Semibold", 26))
        self.create_text(86, 64, text=m.get("text", ""), anchor="w", fill=FG, font=("Segoe UI", 10))
        self.create_text(16, 96, text=m.get("place", ""), anchor="w", fill=MUTED, font=("Segoe UI Semibold", 9))
        self.create_text(16, 116, text=" · ".join(m.get("sub", "").split(" · ")[:2]), anchor="w", fill=MUTED,
                         font=("Segoe UI", 9))
        if m.get("warn"):
            w = m["warn"] if len(m["warn"]) <= 44 else m["warn"][:43] + "…"
            self.create_text(16, 138, text=w, anchor="w", fill=m.get("warn_color") or AMBER,
                             font=("Segoe UI Semibold", 9))
        days = m.get("days", [])
        if self.w > 300 and days:
            x0 = 250
            step = (self.w - x0 - 10) / max(1, len(days))
            for i, d in enumerate(days):
                x = x0 + step * i + step / 2
                self.create_text(x, 22, text=d["label"], fill=MUTED, font=("Segoe UI", 9))
                draw_weather(self, weather_kind(d["code"]), x, 58, 14)
                self.create_text(x, 96, text=f"{round(d['max'])}°", fill=FG, font=("Segoe UI Semibold", 10))
                self.create_text(x, 114, text=f"{round(d['min'])}°", fill=MUTED, font=("Segoe UI", 9))
                if d.get("prob") is not None and d["prob"] >= 30:
                    self.create_text(x, 134, text=f"{d['prob']}%", fill="#5ab0ff", font=("Segoe UI", 8))

    def _draw_ams(self):
        m = self.model
        u = m["unit"]
        self._base()
        self.create_text(16, 20, text=u["name"], anchor="w", fill=FG, font=("Segoe UI Semibold", 11))
        info = [x for x in (m.get("hum"), f"{u['temp']:.0f} °C".replace(".", ",") if u.get("temp") else "") if x]
        if u.get("drying_min"):
            info.append(f"trocknet noch {u['drying_min'] // 60}:{u['drying_min'] % 60:02d} h")
        self.create_text(self.w - 16, 20, text=" · ".join(info), anchor="e", fill=MUTED, font=("Segoe UI", 9))
        trays = u["trays"] or []
        n = max(4, len(trays)) if len(trays) > 1 else 1
        slot_w = (self.w - 32) / n
        top, bot = 40, 108
        for i, t in enumerate(trays):
            cx = 16 + slot_w * i + slot_w / 2
            bw = min(46, slot_w - 18)
            x0, x1 = cx - bw / 2, cx + bw / 2
            col = t["color"] or "#555a66"
            rrect(self, x0, top, x1, bot, 8, fill=CARD2, outline=FG if t["active"] else "#343944",
                  width=2 if t["active"] else 1)
            if t["empty"]:
                self.create_text(cx, (top + bot) / 2, text="leer", fill=MUTED, font=("Segoe UI", 9))
            else:
                frac = (t["remain"] / 100) if t["remain"] is not None else 1.0
                fy = bot - 3 - (bot - top - 6) * max(0.0, min(1.0, frac))
                lum = int(col[1:3], 16) * .3 + int(col[3:5], 16) * .59 + int(col[5:7], 16) * .11
                if frac > 0:
                    rrect(self, x0 + 3, fy, x1 - 3, bot - 3, 6, fill=col, outline="#6a6f7b" if lum < 60 else "")
                if t["remain"] is None:  # unbekannt (keine Bambu-Rolle): gestrichelt
                    self.create_rectangle(x0 + 3, top + 3, x1 - 3, bot - 3, outline=MUTED, dash=(3, 3))
                light = lum > 150
                pct = f"{t['remain']}%" if t["remain"] is not None else "?"
                self.create_text(cx, bot - 14 if frac > .25 else fy - 10, text=pct,
                                 fill=("#0d0f13" if light and frac > .25 else FG), font=("Segoe UI Semibold", 9))
            self.create_text(cx, bot + 14, text=(t["type"] or "–")[:9], fill=FG if not t["empty"] else MUTED,
                             font=("Segoe UI Semibold", 9))
            sub = f"~{t['grams']} g" if t.get("grams") is not None else (t["brand"][:10] or t["slot"])
            self.create_text(cx, bot + 31, text=sub, fill=MUTED, font=("Segoe UI", 8))
            if t["active"]:
                self.create_text(cx, top - 8, text="▼", fill=ACCENT, font=("Segoe UI", 8))

    def _draw_netdev(self):
        m = self.model
        on = m.get("online")
        self._base(active=bool(on))
        self._icon(dev_icon(m.get("category", "")), OK if on else MUTED, bool(on))
        self._title(m["name"], m.get("category") or m.get("ip", ""))
        kids = m.get("children", [])
        extra = bool(kids or m.get("radio"))
        y = self.h - 42 if extra else None
        if not m.get("known"):
            self._big("unbekannt", MUTED, y=y)
        else:
            self._big("● online" if on else "○ offline", OK if on else MUTED, y=y)
        if extra:
            off = [k["name"] for k in kids if not k["online"]]
            txt = m.get("sub", "")
            if off:
                txt = "⚠ offline: " + ", ".join(off)
            self.create_text(16, self.h - 16, text=txt[:46], anchor="w", fill=AMBER if off else MUTED,
                             font=("Segoe UI", 9))

    def _draw_wol(self):
        m = self.model
        on = m.get("online")
        self._base(active=bool(on))
        self._icon("Geräte", OK if on else MUTED, bool(on))
        self._title(m["name"], m.get("sub", ""))
        if m.get("busy"):
            self._big("weckt …", AMBER)
        elif on:
            self._big("● an", OK)
        else:
            self._button(16, self.h - 44, "⏻ Aufwecken", "run", color=ACCENT, w=120)

    def _draw_box(self):
        b = self.model["box"]
        on = b.get("online")
        self._base(active=bool(on))
        self._icon("Router", OK if on else (BAD if on is False else MUTED), bool(on))
        self._title(b.get("name") or b.get("model") or "FRITZ!Box", b.get("role", ""))
        if b.get("error"):
            self.create_text(16, self.h - 40, text="⚠ " + b["error"][:40], anchor="w", fill=BAD, font=("Segoe UI", 9))
            return
        self.create_text(16, self.h - 44, text=b.get("model", ""), anchor="w", fill=FG, font=("Segoe UI Semibold", 11))
        self.create_text(16, self.h - 22, text=b.get("sub", ""), anchor="w", fill=MUTED, font=("Segoe UI", 9))

    def _draw_camera(self):
        m = self.model
        self.delete("all")
        rrect(self, 1, 1, self.w - 1, self.h - 1, 16, fill="#0d0e11", outline=CARD2)
        img = m.get("img")
        if img is not None:
            self.create_image(self.w / 2, (self.h - 40) / 2 + 4, image=img)
        else:
            draw_icon(self, "Kamera", self.w / 2 - 22, (self.h - 40) / 2 - 18, MUTED)
            self.create_text(self.w / 2, (self.h - 40) / 2 + 40, text=m.get("hint", ""), fill=MUTED,
                             font=("Segoe UI", 9))
        rrect(self, 1, self.h - 40, self.w - 1, self.h - 1, 12, fill=CARD, outline="")
        name = self.dash.name_for(self.key, m["name"])
        self.create_text(16, self.h - 20, text=name, anchor="w", fill=FG, font=("Segoe UI Semibold", 11))
        st = m.get("status", "")
        col = OK if st.startswith("●") else (BAD if st.startswith("⚠") else MUTED)
        self.create_text(self.w - 16, self.h - 20, text=st + (" · PTZ" if m.get("ptz") else ""), anchor="e",
                         fill=col, font=("Segoe UI", 9))

    def _draw_printer(self):
        p = self.model["p"]
        st = p["state"]
        busy = st in ("RUNNING", "PREPARE", "PAUSE", "SLICING")
        self._base(active=busy)
        col = {"RUNNING": OK, "PAUSE": WARN, "FAILED": BAD, "FINISH": ACCENT}.get(st, MUTED)
        if not p["connected"]:
            col = BAD
        self._icon("Drucker", col if p["connected"] else "#555a66", busy)
        sub = p["state_text"] if p["connected"] else (p["error"] or "verbinde …")
        self._title(p["name"], sub, sub_color=col)
        if p["light"] is not None:
            self.create_text(self.w - 76, 36, text="Licht", anchor="e", fill=MUTED, font=("Segoe UI", 9))
            self._toggle(self.w - 62, 22, p["light"])
        tl = p.get("tl") or {}
        if tl.get("rec") is not None:
            self.create_text(self.w - 16, 70, text=f"● REC · {tl['rec']} Bilder", anchor="e", fill=BAD,
                             font=("Segoe UI Semibold", 9))
        elif tl.get("armed"):
            self.create_text(self.w - 16, 70, text="🎬 Zeitraffer bereit", anchor="e", fill=MUTED, font=("Segoe UI", 9))
        y = 120
        if p["file"]:
            f = p["file"] if len(p["file"]) <= 60 else p["file"][:59] + "…"
            self.create_text(16, y, text=f, anchor="w", fill=FG, font=("Segoe UI", 10))
        # Fortschritt
        y = 146
        pct = p["percent"] if isinstance(p["percent"], (int, float)) else 0
        rrect(self, 16, y, self.w - 16, y + 14, 7, fill=CARD2, outline="")
        if pct > 0:
            x2 = 16 + max(14, (self.w - 32) * pct / 100)
            rrect(self, 16, y, x2, y + 14, 7, fill=col if busy else ACCENT, outline="")
        y = 182
        self.create_text(16, y, text=f"{pct} %" if p["connected"] else "–", anchor="w", fill=FG,
                         font=("Segoe UI Semibold", 20))
        info = []
        if busy and p["remaining_min"]:
            eta = time.strftime("%H:%M", time.localtime(time.time() + int(p["remaining_min"]) * 60))
            info.append(f"noch {fdur_min(p['remaining_min'])} · fertig ca. {eta} Uhr")
        if p["layers"]:
            info.append(f"Schicht {p['layer'] or 0} / {p['layers']}")
        self.create_text(self.w - 16, y - 8, text=info[0] if info else "", anchor="e", fill=FG, font=("Segoe UI", 10))
        self.create_text(self.w - 16, y + 12, text=info[1] if len(info) > 1 else "", anchor="e", fill=MUTED,
                         font=("Segoe UI", 9))
        # Temperaturen
        y = 226
        temps = []
        for i, (cur, tgt) in enumerate(p["nozzles"]):
            label = "Düse" if len(p["nozzles"]) == 1 else ("Düse L" if i == 0 else "Düse R")
            temps.append((label, cur, tgt))
        if p["bed"] is not None:
            temps.append(("Bett", p["bed"], p["bed_target"]))
        if p["chamber"] is not None:
            temps.append(("Kammer", p["chamber"], None))
        cw = (self.w - 32) / max(1, len(temps))
        for i, (label, cur, tgt) in enumerate(temps):
            x = 16 + i * cw
            self.create_text(x, y, text=label, anchor="w", fill=MUTED, font=("Segoe UI", 9))
            t = f"{round(cur)}°" if isinstance(cur, (int, float)) else "–"
            if isinstance(tgt, (int, float)) and tgt > 0:
                t += f" / {round(tgt)}°"
            self.create_text(x, y + 20, text=t, anchor="w", fill=HEAT if (tgt or 0) > 0 else FG,
                             font=("Segoe UI Semibold", 12))
        # AMS-Farben
        y = 276
        if p["ams"]:
            self.create_text(16, y, text="AMS", anchor="w", fill=MUTED, font=("Segoe UI", 9))
            x = 52
            for tray in p["ams"][:16]:
                if tray["color"]:
                    self.create_oval(x, y - 9, x + 18, y + 9, fill=tray["color"],
                                     outline=FG if tray["active"] else CARD2, width=3 if tray["active"] else 1)
                else:
                    self.create_oval(x, y - 9, x + 18, y + 9, fill="", outline=MUTED, dash=(2, 2))
                x += 24
        # Kosten / Kamera
        if p.get("cost_text"):
            self.create_text(self.w - 16, 94, text=p["cost_text"], anchor="e", fill=AMBER, font=("Segoe UI", 9))
        y = self.h - 42
        self._button(16, y, "Kamera", "camera", w=78)
        if p["connected"]:
            x = self.w - 16
            btns = []
            if st == "PAUSE":
                btns.append(("Fortsetzen", "resume", ACCENT))
            elif st in ("RUNNING", "PREPARE"):
                btns.append(("Pause", "pause", CARD2))
            if busy:
                btns.append(("Stopp", "stop", "#5a2629"))
            for text, tag, color in reversed(btns):
                w = 12 + 8 * len(text)
                x -= w
                self._button(x, y, text, tag, color=color, w=w)
                x -= 8


# --------------------------------------------------------------------------- #
# Dashboard (scrollbar, Abschnitte, automatischer Umbruch)
# --------------------------------------------------------------------------- #
class Dashboard(ttk.Frame):
    SECTIONS = ("Wetter", "Personen", "Termine", "Szenen", "Kameras", "3D-Druck", "Smart Home", "Geräte", "Erweiterungen",
                "Netzwerk")

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.tiles = {}          # key -> Tile
        self.sections = {name: [] for name in self.SECTIONS}   # Standard-Bereich -> [keys]
        self.headers = {}
        self.images = {}
        self.power_hist = {}
        self.total_hist = []
        self.down_hist = []
        self.pending_temp = {}
        self._cols = 0
        self._dirty = False

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.l_hint = ttk.Label(bar, text="Schalter = an/aus · Rechtsklick = Raum, Name, Bild, ausblenden",
                                style="Muted.TLabel")
        self.l_hint.pack(side="left")
        ttk.Button(bar, text="Ausgeblendete zeigen", command=self.unhide_all).pack(side="right")
        ttk.Button(bar, text="+ 3D-Drucker", command=lambda: app.printer_dialog()).pack(side="right", padx=6)
        ttk.Button(bar, text="+ Kamera", command=lambda: app.camera_dialog()).pack(side="right")
        ttk.Button(bar, text="+ Person", command=lambda: app.persons_dialog()).pack(side="right", padx=6)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.empty = tk.Label(self.inner, bg=BG, fg=MUTED, font=("Segoe UI", 11), justify="left",
                              text="Willkommen 👋  Sobald die FRITZ!Box verbunden ist, erscheinen hier deine Geräte.\n"
                                   "Über „+ Kamera“ und „+ 3D-Drucker“ kommt der Rest dazu.")

    def _wheel(self, e):
        if str(self.app.nb.select()) == str(self.app.tab_home):
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _on_resize(self, e):
        self.canvas.itemconfigure(self.win, width=e.width)
        cols = max(1, (e.width - 8) // (TW + GAP))
        if cols != self._cols:
            self._cols = cols
            self.layout()

    # ---------------- Namen / Räume ----------------
    def name_for(self, key, default):
        return self.app.cfg.get("tile_names", {}).get(key) or default

    def rename(self, key):
        from tkinter import simpledialog
        cur = self.app.cfg.get("tile_names", {}).get(key, "")
        new = simpledialog.askstring("Umbenennen", "Neuer Name (leer = Originalname):", initialvalue=cur, parent=self)
        if new is None:
            return
        names = self.app.cfg.setdefault("tile_names", {})
        if new.strip():
            names[key] = new.strip()
        else:
            names.pop(key, None)
        self.app.save()
        self.tiles[key].redraw()

    def rooms(self):
        return list(self.app.cfg.get("rooms", []))

    def room_of(self, key):
        r = self.app.cfg.get("tile_room", {}).get(key)
        return r if r in self.rooms() else None

    def set_room(self, key, room):
        tr = self.app.cfg.setdefault("tile_room", {})
        if room:
            tr[key] = room
        else:
            tr.pop(key, None)
        # leere Räume aufräumen
        used = set(tr.values())
        self.app.cfg["rooms"] = [r for r in self.rooms() if r in used]
        self.app.save()
        self.layout()

    def new_room(self, key):
        from tkinter import simpledialog
        name = simpledialog.askstring("Neuer Raum", "Name des Raums (z. B. Wohnzimmer, Werkstatt):", parent=self)
        if name and name.strip():
            rooms = self.app.cfg.setdefault("rooms", [])
            if name.strip() not in rooms:
                rooms.append(name.strip())
            self.set_room(key, name.strip())

    # ---------------- Bilder / Ausblenden ----------------
    def image_for(self, key):
        path = self.app.cfg.get("tile_images", {}).get(key)
        if not path:
            return None
        if path not in self.images:
            try:
                img = tk.PhotoImage(file=path)
                f = max(1, math.ceil(max(img.width(), img.height()) / 40))
                self.images[path] = img.subsample(f, f) if f > 1 else img
            except tk.TclError:
                self.images[path] = None
        return self.images[path]

    def choose_image(self, key):
        path = filedialog.askopenfilename(title="Bild für Kachel wählen (PNG oder GIF)",
                                          filetypes=[("Bilder", "*.png *.gif"), ("Alle Dateien", "*.*")])
        if path:
            self.app.cfg.setdefault("tile_images", {})[key] = path
            self.app.save()
            self.images.pop(path, None)
            if self.image_for(key) is None:
                self.app.cfg["tile_images"].pop(key, None)
                self.app.save()
                from tkinter import messagebox
                messagebox.showwarning("Bild", "Das Bild konnte nicht geladen werden. Bitte PNG oder GIF verwenden.")
            self.tiles[key].redraw()

    def reset_image(self, key):
        self.app.cfg.get("tile_images", {}).pop(key, None)
        self.app.save()
        self.tiles[key].redraw()

    def hide(self, key):
        hidden = self.app.cfg.setdefault("hidden_tiles", [])
        if key not in hidden:
            hidden.append(key)
        self.app.save()
        self.layout()

    def unhide_all(self):
        self.app.cfg["hidden_tiles"] = []
        self.app.save()
        self.layout()

    # ---------------- Kacheln verwalten ----------------
    def put(self, section, key, model, cols=1, rows=1):
        tile = self.tiles.get(key)
        if tile is None:
            tile = Tile(self.inner, self, key, cols, rows)
            self.tiles[key] = tile
            self.sections[section].append(key)
            self._dirty = True
        tile.set(model)

    def remove_missing(self, section, keep_prefix, keys):
        lst = self.sections[section]
        for k in [k for k in lst if k.startswith(keep_prefix) and k not in keys]:
            lst.remove(k)
            self.tiles.pop(k).destroy()
            self._dirty = True

    def commit(self):
        if self._dirty:
            self._dirty = False
            self.layout()

    def _header(self, name):
        h = self.headers.get(name)
        if h is None:
            h = tk.Label(self.inner, text=name, bg=BG, fg=FG, font=("Segoe UI Semibold", 13), anchor="w")
            self.headers[name] = h
        return h

    def layout(self):
        cols = max(1, self._cols or 4)
        hidden = set(self.app.cfg.get("hidden_tiles", []))
        groups = [(r, []) for r in self.rooms()] + [(s, []) for s in self.SECTIONS]
        gmap = dict(groups)
        for sec in self.SECTIONS:
            for k in self.sections[sec]:
                self.tiles[k].grid_forget()
                if k in hidden:
                    continue
                gmap[self.room_of(k) or sec].append(k)
        for h in self.headers.values():
            h.grid_forget()
        self.empty.grid_forget()
        row = 0
        any_tile = False
        for name, keys in groups:
            if not keys:
                continue
            any_tile = True
            self._header(name).grid(row=row, column=0, columnspan=cols, sticky="w", padx=6, pady=(14 if row else 2, 4))
            row += 1
            occ, maxr = set(), 0
            for k in keys:
                t = self.tiles[k]
                cs, rs = min(t.cols, cols), t.rows
                r = 0
                while True:
                    spot = next((c for c in range(cols - cs + 1)
                                 if not {(r + i, c + j) for i in range(rs) for j in range(cs)} & occ), None)
                    if spot is not None:
                        occ |= {(r + i, spot + j) for i in range(rs) for j in range(cs)}
                        t.grid(row=row + r, column=spot, rowspan=rs, columnspan=cs, padx=GAP // 2,
                               pady=GAP // 2, sticky="nw")
                        maxr = max(maxr, r + rs)
                        break
                    r += 1
            row += maxr
        if not any_tile:
            self.empty.grid(row=0, column=0, padx=10, pady=30, sticky="w")

    def on_action(self, key, action, model):
        self.app.tile_action(key, action, model)
