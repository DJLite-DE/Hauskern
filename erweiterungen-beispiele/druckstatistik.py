# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
Beispiel-Erweiterung: Wie viel wurde diesen Monat gedruckt?
Zeigt eine Kachel, aktualisiert sich nach jedem Druck und versteht „Hey Siri, Hauskern: Druckstatistik“.
"""
import time

NAME = "Druckstatistik"
DESCRIPTION = "Kachel mit Drucken, Druckzeit und Filament im laufenden Monat, Siri: „Druckstatistik“"
VERSION = "1.0"


def setup(hk):
    def month():
        lt = time.localtime()
        start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        days = (time.time() - start) / 86400 + 0.01
        rows = [p for p in hk.prints(days) if p["start"] >= start]
        ok = sum(1 for p in rows if p["result"] == "fertig")
        hours = sum(((p["end"] or p["start"]) - p["start"]) for p in rows) / 3600
        grams = sum(float(p["grams"] or 0) for p in rows)
        return len(rows), ok, hours, grams

    def update(_ev=None):
        n, ok, h, g = month()
        hk.tile("monat", "🖨 Drucke im Monat", f"{n} Drucke",
                f"{ok} fertig · {h:.1f} h · {g:.0f} g Filament".replace(".", ","))

    def answer(_):
        n, ok, h, g = month()
        if not n:
            return "Diesen Monat wurde noch nichts gedruckt."
        return f"Diesen Monat {n} Drucke, davon {ok} fertig, zusammen {h:.0f} Stunden und {g:.0f} Gramm Filament."

    hk.every(1800, update)
    hk.on("print_finished", update)
    hk.on("print_failed", update)
    hk.siri(["druckstatistik"], answer)
