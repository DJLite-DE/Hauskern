# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
charts.py – Diagramme für die Verlaufs-Seite (reines Tkinter-Canvas, keine Zusatzpakete).

Chart.show(series=[...], t0, t1, unit, kind="line"|"bars", bucket)
  series: {"name", "color", "pts": [(ts, wert)], "step": bool, "dash": bool}
Fährt man mit der Maus über das Diagramm, zeigt eine Linie die Werte zu diesem Zeitpunkt.
"""
import math
import time
import tkinter as tk

from dashboard import CARD, FG, MUTED

GRID = "#30333c"
PALETTE = ["#4f8ff7", "#f0883e", "#3ecf8e", "#b392f0", "#f6c15a", "#5ab0ff", "#ff7b72", "#56d4bc"]
WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def fnum(v, unit=""):
    if v is None:
        return "–"
    a = abs(v)
    if unit == "bit/s":
        for f, u in ((1e9, "Gbit/s"), (1e6, "Mbit/s"), (1e3, "kbit/s")):
            if a >= f:
                return f"{v / f:.1f} {u}".replace(".", ",")
        return f"{v:.0f} bit/s"
    if unit == "kWh" and a < 1:
        return f"{v * 1000:.0f} Wh"
    d = 0 if a >= 100 else (1 if a >= 10 or unit in ("°C", "W", "ms", "%") else 2)
    return f"{v:.{d}f}".replace(".", ",") + (f" {unit}" if unit else "")


def nice_ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    ticks, v = [], start
    while v <= end + step * 0.01:
        ticks.append(round(v, 10))
        v += step
    return ticks


def tick_fmt(v, step):
    d = max(0, -math.floor(math.log10(step) + 1e-9)) if step < 1 else 0
    if step < 1 and round(step * 10 ** d) % 1:  # 0,25 usw.
        d += 1
    if step >= 1 and step % 1:
        d = 1
    return f"{v:.{d}f}".replace(".", ",")


def dstr(ts, with_time=True, year=False):
    lt = time.localtime(ts)
    s = f"{WD[lt.tm_wday]} {lt.tm_mday:02d}.{lt.tm_mon:02d}." + (str(lt.tm_year) if year else "")
    return s + (time.strftime(" %H:%M", lt) if with_time else "")


def time_label(ts, span):
    lt = time.localtime(ts)
    if span <= 2 * 86400:
        return time.strftime("%H:%M", lt)
    if span <= 40 * 86400:
        return f"{WD[lt.tm_wday]} {lt.tm_mday}.{lt.tm_mon}."
    return time.strftime("%m/%Y", lt).replace("/", ".")


def time_ticks(t0, t1):
    span = t1 - t0
    for step in (3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400):
        if span / step <= 8:
            break
    else:
        step = None
    out = []
    if step is None:  # Monate
        lt = time.localtime(t0)
        y, m = lt.tm_year, lt.tm_mon + 1
        while True:
            if m > 12:
                y, m = y + 1, 1
            ts = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
            if ts > t1:
                break
            if ts >= t0:
                out.append(ts)
            m += 2 if span > 200 * 86400 else 1
        return out
    # an Ortszeit ausrichten (volle Stunde / Mitternacht)
    lt = time.localtime(t0)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    ts = base
    while ts < t0:
        ts += step
    while ts <= t1:
        out.append(ts)
        ts += step
    return out


class Chart(tk.Canvas):
    ML, MR, MT, MB = 58, 14, 26, 24

    def __init__(self, master, height=260, **kw):
        super().__init__(master, bg=CARD, highlightthickness=0, height=height, **kw)
        self.data = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("hover"))

    def show(self, series, t0, t1, unit="", kind="line", bucket=60, empty="Noch keine Daten für diesen Zeitraum",
             y_zero=False, label_fmt=None):
        self.data = dict(series=series, t0=t0, t1=t1, unit=unit, kind=kind, bucket=bucket, empty=empty,
                         y_zero=y_zero, label_fmt=label_fmt)
        self.redraw()

    # ---------------------------------------------------------------- #
    def _geom(self):
        w, h = max(self.winfo_width(), 200), max(self.winfo_height(), 120)
        return w, h, self.ML, w - self.MR, self.MT, h - self.MB

    def _x(self, ts):
        d = self.data
        _, _, x0, x1, _, _ = self._geom()
        return x0 + (ts - d["t0"]) / max(1, d["t1"] - d["t0"]) * (x1 - x0)

    def _y(self, v):
        _, _, _, _, y0, y1 = self._geom()
        return y1 - (v - self.lo) / max(1e-9, self.hi - self.lo) * (y1 - y0)

    def redraw(self):
        self.delete("all")
        d = self.data
        if not d:
            return
        w, h, x0, x1, y0, y1 = self._geom()
        vals = [v for s in d["series"] for _, v in s["pts"] if v is not None]
        if not vals:
            self.create_text(w / 2, h / 2, text=d["empty"], fill=MUTED, font=("Segoe UI", 10))
            return
        lo, hi = min(vals), max(vals)
        if d["kind"] == "bars" or d["y_zero"]:
            lo = min(0, lo)
        pad = (hi - lo) * 0.08 or (abs(hi) * 0.1 or 1)
        lo2 = lo if (d["kind"] == "bars" or d["y_zero"]) and lo >= 0 else lo - pad
        ticks = nice_ticks(lo2, hi + pad)
        self.lo, self.hi = ticks[0], ticks[-1]
        # Gitter + Achsen
        step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
        for t in ticks:
            y = self._y(t)
            self.create_line(x0, y, x1, y, fill=GRID)
            self.create_text(x0 - 6, y, text=tick_fmt(t, step), anchor="e", fill=MUTED, font=("Segoe UI", 8))
        if d["unit"]:
            self.create_text(x0 - 6, y0 - 14, text=d["unit"], anchor="e", fill=MUTED, font=("Segoe UI", 8))
        span = d["t1"] - d["t0"]
        for ts in time_ticks(d["t0"], d["t1"]):
            x = self._x(ts)
            self.create_line(x, y0, x, y1, fill=GRID, dash=(2, 4))
            self.create_text(x, y1 + 12, text=time_label(ts, span), fill=MUTED, font=("Segoe UI", 8))
        # Daten
        if d["kind"] == "bars":
            self._bars(d)
        else:
            for s in d["series"]:
                self._line(s, d)
        # Legende
        if len(d["series"]) > 1 or d["series"][0].get("name"):
            lx = x1
            for s in reversed(d["series"]):
                tid = self.create_text(lx, 10, text=s["name"], anchor="e", fill=FG, font=("Segoe UI", 9))
                bx = self.bbox(tid)[0]
                self.create_line(bx - 20, 10, bx - 6, 10, fill=s["color"], width=3, dash=(4, 2) if s.get("dash") else None)
                lx = bx - 32

    def _line(self, s, d):
        gap = max(3 * d["bucket"], 300)
        seg, prev = [], None
        for ts, v in s["pts"]:
            if v is None:
                continue
            if prev is not None and ts - prev > gap:
                self._seg(seg, s)
                seg = []
            x, y = self._x(ts + d["bucket"] / 2), self._y(v)
            if s.get("step") and seg:
                seg += [x, seg[-1]]
            seg += [x, y]
            prev = ts
        self._seg(seg, s)

    def _seg(self, seg, s):
        if len(seg) >= 4:
            self.create_line(*seg, fill=s["color"], width=2, dash=(5, 3) if s.get("dash") else None)
        elif len(seg) == 2:
            self.create_oval(seg[0] - 2, seg[1] - 2, seg[0] + 2, seg[1] + 2, fill=s["color"], outline="")

    def _bars(self, d):
        s = d["series"][0]
        _, _, x0, x1, _, y1 = self._geom()
        for ts, v in s["pts"]:
            xa = max(x0, self._x(ts) + 1)
            xb = min(x1, self._x(ts + d["bucket"]) - 1)
            if xb - xa < 1:
                xb = xa + 1
            self.create_rectangle(xa, self._y(v), xb, self._y(0), fill=s["color"], outline="")

    # ---------------------------------------------------------------- #
    def _hover(self, e):
        self.delete("hover")
        d = self.data
        if not d or not any(s["pts"] for s in d["series"]):
            return
        w, h, x0, x1, y0, y1 = self._geom()
        if not (x0 <= e.x <= x1):
            return
        ts = d["t0"] + (e.x - x0) / (x1 - x0) * (d["t1"] - d["t0"])
        lines = []
        for s in d["series"]:
            best = None
            for t, v in s["pts"]:
                if v is None:
                    continue
                c = t if d["kind"] == "bars" else t + d["bucket"] / 2
                if d["kind"] == "bars" and not (t <= ts < t + d["bucket"]):
                    continue
                if best is None or abs(c - ts) < abs(best[0] - ts):
                    best = (c, v)
            if best and (d["kind"] == "bars" or abs(best[0] - ts) <= max(3 * d["bucket"], 300)):
                lines.append((s, best))
        if not lines:
            return
        x = e.x if d["kind"] == "bars" else self._x(lines[0][1][0])
        self.create_line(x, y0, x, y1, fill="#5a5f6b", tags="hover")
        t_show = lines[0][1][0]
        if d["label_fmt"]:
            head = d["label_fmt"](t_show)
        else:
            head = dstr(t_show, d["bucket"] < 86400, d["bucket"] >= 86400)
        rows = [head] + [f"{s['name'] + ': ' if s.get('name') else ''}{fnum(v, d['unit'])}" for s, (_, v) in lines]
        for s, (_, v) in lines:
            if d["kind"] != "bars":
                y = self._y(v)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=s["color"], outline=CARD, width=2, tags="hover")
        tw = max(len(r) for r in rows) * 6.6 + 16
        th = len(rows) * 16 + 8
        bx = x + 12 if x + 12 + tw < x1 else x - 12 - tw
        by = y0 + 4
        self.create_rectangle(bx, by, bx + tw, by + th, fill="#0d0f13", outline="#343944", tags="hover")
        for i, r in enumerate(rows):
            self.create_text(bx + 8, by + 12 + i * 16, text=r, anchor="w", fill=FG if i else MUTED,
                             font=("Segoe UI Semibold" if i else "Segoe UI", 9), tags="hover")
