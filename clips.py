# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
clips.py – Kamera-Aufnahmen (MP4) auflisten, aufräumen und löschen.

Die Aufnahmen liegen direkt im Video-Ordner von Hauskern (Zeitraffer haben einen eigenen Unterordner
und werden hier nicht angefasst). Dateiname: „<Kamera> JJJJ-MM-TT HH-MM-SS.mp4“.

    list_clips(folder, active=())          → [{"name","path","size","ts","cam"}], neueste zuerst
    enforce(folder, settings, active=())   → [gelöschte Namen]
    delete(folder, name, active=())        → True/False
"""
import os
import re
import time

DEFAULTS = {"max_gb": 10, "keep_days": 30}
_PAT = re.compile(r"^(.*) (\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})\.mp4$")


def parse_name(name):
    """→ (Kameraname, Zeitstempel) oder (None, None), wenn der Name nicht passt."""
    m = _PAT.match(name)
    if not m:
        return None, None
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[1:])
    try:
        return m.group(1), time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    except (OverflowError, ValueError):
        return m.group(1), None


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def list_clips(folder, active=()):
    act = {_norm(p) for p in active if p}
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if not n.lower().endswith(".mp4") or n.endswith(".part.mp4"):
            continue
        p = os.path.join(folder, n)
        if _norm(p) in act:  # läuft gerade noch – nicht anzeigen und nicht löschen
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        cam, ts = parse_name(n)
        out.append({"name": n, "path": p, "size": st.st_size, "ts": ts or st.st_mtime, "cam": cam or ""})
    out.sort(key=lambda v: -v["ts"])
    return out


def enforce(folder, settings, active=(), now=None):
    """Älter als keep_days oder über max_gb → die ältesten Aufnahmen löschen. Die neueste bleibt immer."""
    now = now or time.time()
    vids = list_clips(folder, active)
    keep_s = float(settings.get("keep_days", DEFAULTS["keep_days"]) or 0) * 86400
    max_b = float(settings.get("max_gb", DEFAULTS["max_gb"]) or 0) * 1e9
    removed, total = [], sum(v["size"] for v in vids)
    for i, v in enumerate(reversed(vids)):  # älteste zuerst
        newest = i == len(vids) - 1
        too_old = keep_s and now - v["ts"] > keep_s and not newest
        too_big = max_b and total > max_b and not newest
        if too_old or too_big:
            try:
                os.remove(v["path"])
                removed.append(v["name"])
                total -= v["size"]
            except OSError:
                pass
    return removed


def delete(folder, name, active=()):
    if not name or os.path.basename(name) != name:
        return False
    v = next((x for x in list_clips(folder, active) if x["name"] == name), None)
    if not v:
        return False
    os.remove(v["path"])
    return True
