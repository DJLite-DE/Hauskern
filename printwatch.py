# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
printwatch.py – Fehldruck-Wächter: wertet die KI-Erkennung des Druckers aus (Bambu X1/X2D/H2D …).

Hauskern ändert dabei nichts am Drucker. Es liest nur, was der Drucker selbst meldet:
  * HMS 0C00-0300-0003-0008 – „Möglicher Spaghetti-Fehler“ (Drucker druckt weiter)
  * print_error 0300 8003   – „Spaghetti erkannt“ (Drucker hat pausiert, wenn „Bei Fehler pausieren“ an ist)
  * xcam-Einstellungen       – ist die KI-Überwachung überhaupt an?
"""

SPAGHETTI_HMS = {"0C00-0300-0003-0008"}
SPAGHETTI_ERR = {0x03008003}
SENS = {"low": "niedrig", "medium": "mittel", "high": "hoch", "never_halt": "nie anhalten"}


def hms_code(item):
    """{"attr": int, "code": int} → „0C00-0300-0003-0008“"""
    try:
        a, c = int(item.get("attr", 0)), int(item.get("code", 0))
    except (TypeError, ValueError, AttributeError):
        return ""
    return f"{a >> 16:04X}-{a & 0xFFFF:04X}-{c >> 16:04X}-{c & 0xFFFF:04X}"


def hms_codes(raw_hms):
    return [x for x in (hms_code(i) for i in (raw_hms or []) if isinstance(i, dict)) if x]


def xcam_info(xc):
    """xcam-Block des Druckers → {"on": bool|None, "halt": bool|None, "sens": text} oder None (unbekannt)."""
    if not isinstance(xc, dict) or not xc:
        return None
    on = xc.get("printing_monitor", xc.get("spaghetti_detector"))
    if on is None and "spaghetti_detector" not in xc:
        return None
    return {"on": None if on is None else bool(on), "halt": None if xc.get("print_halt") is None else bool(xc["print_halt"]),
            "sens": SENS.get(str(xc.get("halt_print_sensitivity") or ""), "")}


def xcam_text(info):
    if not info or info["on"] is None:
        return ""
    if not info["on"]:
        return "🤖 KI-Überwachung im Drucker ist aus"
    t = "🤖 KI-Überwachung an"
    if info["halt"] is True:
        t += " · pausiert bei Fehler"
    elif info["halt"] is False:
        t += " · meldet nur"
    if info["sens"]:
        t += f" · Empfindlichkeit {info['sens']}"
    return t


def events(print_error, codes):
    """→ Liste von Ereignissen: ("spaghetti_paused" | "spaghetti_warn")"""
    out = []
    if (print_error or 0) in SPAGHETTI_ERR:
        out.append("spaghetti_paused")
    if any(c in SPAGHETTI_HMS for c in codes or []):
        out.append("spaghetti_warn")
    if "spaghetti_paused" in out and "spaghetti_warn" in out:
        out.remove("spaghetti_warn")
    return out


def message(event, name):
    if event == "spaghetti_paused":
        return (f"🍝 {name}: Fehldruck erkannt – Drucker hat pausiert",
                "Die KI im Drucker sieht „Spaghetti“. Schau aufs Foto: Ist alles gut, in Hauskern „Fortsetzen“, "
                "sonst „Stopp“.")
    return (f"🍝 {name}: möglicher Fehldruck",
            "Die KI im Drucker ist sich nicht sicher und druckt weiter – kurz aufs Foto schauen.")
