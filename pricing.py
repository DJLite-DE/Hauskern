# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
pricing.py – Druckkosten-Kalkulation für den 3D-Drucker.

calculate(job, settings, filaments) -> Aufschlüsselung (alles in Euro)

job = {"name", "items": [{"filament": id oder "", "type": "PLA", "color": "#fff", "grams": 23.4}],
       "hours": 2.5, "kwh": None (gemessen) , "qty": 1, "labor_min": 10, "extras": 0.5,
       "margin": None (Standard aus settings)}

Hinweis: Das ist eine Kalkulationshilfe, keine Steuerberatung.
"""
import math

DEFAULT_FILAMENTS = [
    {"id": "pla", "name": "PLA (Standard)", "type": "PLA", "price_kg": 20.0, "spool_g": 1000, "color": ""},
    {"id": "petg", "name": "PETG", "type": "PETG", "price_kg": 22.0, "spool_g": 1000, "color": ""},
    {"id": "abs", "name": "ABS", "type": "ABS", "price_kg": 22.0, "spool_g": 1000, "color": ""},
    {"id": "asa", "name": "ASA", "type": "ASA", "price_kg": 28.0, "spool_g": 1000, "color": ""},
    {"id": "tpu", "name": "TPU", "type": "TPU", "price_kg": 30.0, "spool_g": 1000, "color": ""},
    {"id": "placf", "name": "PLA-CF", "type": "PLA-CF", "price_kg": 35.0, "spool_g": 1000, "color": ""},
    {"id": "support", "name": "Stützmaterial", "type": "SUPPORT", "price_kg": 50.0, "spool_g": 500, "color": ""},
]

DEFAULT_SETTINGS = {
    "printer_price": 0.0,       # Anschaffung in €
    "lifetime_h": 5000.0,       # erwartete Lebensdauer in Druckstunden
    "wear_h": 0.10,             # Verschleißteile (Düse, Druckplatte, Riemen, Fett …) in € pro Stunde
    "avg_watt": 120.0,          # falls keine Messsteckdose: angenommener Durchschnittsverbrauch
    "fail_pct": 5.0,            # Fehldruck-Risiko: Aufschlag auf Material/Strom/Maschine
    "waste_pct": 3.0,           # Verschnitt (Anfahren, Spülen, Reste am Spulenende)
    "hourly_rate": 0.0,         # dein Stundenlohn für Vor-/Nacharbeit
    "labor_min": 0.0,           # Standard-Arbeitszeit pro Auftrag in Minuten
    "packaging": 0.0,           # Verpackung/Versandmaterial pro Auftrag
    "margin_pct": 30.0,         # Gewinnaufschlag
    "vat_pct": 19.0,            # MwSt.
    "small_business": False,    # Kleinunternehmer (§ 19 UStG): keine MwSt. ausweisen
    "fee_pct": 0.0,             # Plattformgebühr (z. B. Etsy/eBay) – wird eingepreist
    "round_to": 0.10,           # Endpreis aufrunden auf (0 = nicht runden)
}


def find_filament(filaments, fid="", ftype="", color=""):
    """Passendes Filament: per ID, sonst Typ + Farbe, sonst nur Typ, sonst das erste."""
    if fid:
        f = next((x for x in filaments if x["id"] == fid), None)
        if f:
            return f
    t, c = (ftype or "").upper(), (color or "").lower()[:7]
    if t:
        cand = [x for x in filaments if (x.get("type") or "").upper() == t]
        if c:
            f = next((x for x in cand if (x.get("color") or "").lower()[:7] == c), None)
            if f:
                return f
        f = next((x for x in cand if not x.get("color")), None) or (cand[0] if cand else None)
        if f:
            return f
        base = t.split("-")[0]  # "PLA-S" → PLA
        f = next((x for x in filaments if (x.get("type") or "").upper() == base), None)
        if f:
            return f
    return filaments[0] if filaments else {"id": "", "name": "Filament", "type": "", "price_kg": 20.0}


def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return d


def round_up(v, step):
    if step <= 0:
        return round(v, 2)
    return round(math.ceil(v / step - 1e-9) * step, 2)


def calculate(job, settings=None, filaments=None, price_ct=35.0):
    s = dict(DEFAULT_SETTINGS)
    s.update(settings or {})
    fil = filaments or DEFAULT_FILAMENTS
    qty = max(1, int(_num(job.get("qty", 1), 1)))
    hours = max(0.0, _num(job.get("hours")))
    lines = []

    # Material
    mat, grams = 0.0, 0.0
    waste = 1 + _num(s["waste_pct"]) / 100
    for it in job.get("items", []):
        g = max(0.0, _num(it.get("grams")))
        if not g:
            continue
        f = find_filament(fil, it.get("filament", ""), it.get("type", ""), it.get("color", ""))
        cost = g / 1000 * _num(f.get("price_kg"), 20) * waste
        mat += cost
        grams += g
        lines.append({"label": f"{f['name']} · {g:.1f} g".replace(".", ","), "eur": cost, "group": "Material"})

    # Strom
    kwh = job.get("kwh")
    measured = kwh is not None
    if not measured:
        kwh = hours * _num(s["avg_watt"], 120) / 1000
    power = _num(kwh) * _num(price_ct, 35) / 100

    # Maschine
    life = max(1.0, _num(s["lifetime_h"], 5000))
    depreciation = hours * _num(s["printer_price"]) / life
    wear = hours * _num(s["wear_h"])
    base = mat + power + depreciation + wear
    risk = base * _num(s["fail_pct"]) / 100

    labor_min = _num(job["labor_min"]) if job.get("labor_min") not in (None, "") else _num(s["labor_min"])
    labor = labor_min / 60 * _num(s["hourly_rate"])
    extras = (_num(job["extras"]) if job.get("extras") not in (None, "") else 0.0) + _num(s["packaging"])

    cost = base + risk + labor + extras
    margin_pct = _num(job["margin"]) if job.get("margin") not in (None, "") else _num(s["margin_pct"])
    profit = cost * margin_pct / 100
    net = cost + profit
    vat_pct = 0.0 if s["small_business"] else _num(s["vat_pct"])
    vat = net * vat_pct / 100
    gross = net + vat
    fee_pct = _num(s["fee_pct"])
    fee = gross / (1 - fee_pct / 100) - gross if 0 < fee_pct < 90 else 0.0
    final = gross + fee
    step = _num(s["round_to"])
    final_r = round(round_up(final / qty, step) * qty, 2) if step else round(final, 2)
    # Aufschlüsselung aus dem (gerundeten) Endpreis zurückrechnen, damit alles genau aufgeht
    fee = final_r * fee_pct / 100 if 0 < fee_pct < 90 else 0.0
    net = (final_r - fee) / (1 + vat_pct / 100)
    vat = final_r - fee - net
    profit = net - cost
    gross = net + vat

    return {
        "qty": qty, "hours": hours, "grams": grams, "kwh": _num(kwh), "kwh_measured": measured,
        "material": mat, "material_lines": lines, "power": power, "depreciation": depreciation, "wear": wear,
        "risk": risk, "labor": labor, "labor_min": labor_min, "extras": extras, "cost": cost,
        "margin_pct": margin_pct, "profit": profit, "net": net, "vat_pct": vat_pct, "vat": vat, "gross": gross,
        "fee_pct": fee_pct, "fee": fee, "price": final_r, "price_each": final_r / qty, "cost_each": cost / qty,
        "small_business": bool(s["small_business"]),
        "earn": profit,  # was nach Kosten, Steuer und Gebühren übrig bleibt
    }


def eur(v):
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v):
    return f"{float(v):g}".replace(".", ",")


def as_text(job, r):
    L = [f"Kalkulation: {job.get('name') or '3D-Druck'}" + (f" ({r['qty']} Stück)" if r["qty"] > 1 else ""), ""]
    h = int(r["hours"])
    m = round((r["hours"] - h) * 60)
    L.append(f"Druckzeit {h}:{m:02d} h · {r['grams']:.1f} g Filament".replace(".", ","))
    for ln in r["material_lines"]:
        L.append(f"  Material {ln['label']}: {eur(ln['eur'])}")
    L += [f"  Strom ({r['kwh']:.2f} kWh{' gemessen' if r['kwh_measured'] else ' geschätzt'}): {eur(r['power'])}".replace(".", ",", 1),
          f"  Abschreibung Drucker: {eur(r['depreciation'])}", f"  Verschleiß: {eur(r['wear'])}",
          f"  Fehldruck-Risiko: {eur(r['risk'])}"]
    if r["labor"]:
        L.append(f"  Arbeitszeit ({r['labor_min']:.0f} Min.): {eur(r['labor'])}")
    if r["extras"]:
        L.append(f"  Verpackung/Sonstiges: {eur(r['extras'])}")
    L += ["", f"Selbstkosten: {eur(r['cost'])}" + (f" ({eur(r['cost_each'])} je Stück)" if r["qty"] > 1 else ""),
          f"+ Gewinn (Ziel {pct(r['margin_pct'])} %, mit Rundung): {eur(r['profit'])}", f"= Netto: {eur(r['net'])}"]
    if r["small_business"]:
        L.append("Kein Ausweis der Umsatzsteuer (Kleinunternehmer, § 19 UStG)")
    else:
        L.append(f"+ MwSt. {pct(r['vat_pct'])} %: {eur(r['vat'])}")
    if r["fee"]:
        L.append(f"+ Plattformgebühr {pct(r['fee_pct'])} %: {eur(r['fee'])}")
    L += [f"VERKAUFSPREIS: {eur(r['price'])}" + (f" ({eur(r['price_each'])} je Stück)" if r["qty"] > 1 else ""),
          f"Davon bleiben dir nach Kosten, Steuer und Gebühren: {eur(r['earn'])}"]
    return "\n".join(L)
