# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
power.py – Stromkosten auswerten: pro Steckdose und gesamt, mit Hochrechnung, Trend und Grundlast.

Reine Rechenfunktionen ohne Oberfläche – Daten kommen aus der FRITZ!Box-Statistik (Monate/Tage)
und, wo die fehlt, aus Hauskerns eigener Aufzeichnung.
"""
import calendar
import datetime as dt

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]
MONTHS_SHORT = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

DEFAULTS = {"budget_eur": 0.0, "report": True, "outliers": True, "budget_warn": True, "standby_w": 1.0}


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def month_name(key, short=False):
    y, m = key.split("-")
    return (MONTHS_SHORT if short else MONTHS)[int(m) - 1] + ("" if short else f" {y}")


def prev_months(key, n):
    y, m = map(int, key.split("-"))
    out = []
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        out.append(f"{y:04d}-{m:02d}")
    return out


def month_fraction(now=None):
    now = now or dt.datetime.now()
    days = calendar.monthrange(now.year, now.month)[1]
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(0.02, (now - start).total_seconds() / (days * 86400)), days


def eur(kwh, price_ct):
    return (kwh or 0) * price_ct / 100


def analyze(devices, price_ct, settings=None, now=None):
    """devices: [{"ain","name","now_w","today_kwh","months": {"JJJJ-MM": kWh}, "standby_w"}]
    → {"devices": [...], "total": {...}, "months": [(key, kWh)], "alerts": [...]}"""
    s = dict(DEFAULTS, **(settings or {}))
    now = now or dt.datetime.now()
    cur = month_key(now)
    frac, days = month_fraction(now)
    rows, alerts = [], []
    all_months = {}
    for d in devices:
        mo = {k: v for k, v in (d.get("months") or {}).items() if v is not None}
        for k, v in mo.items():
            all_months[k] = all_months.get(k, 0) + v
        c = mo.get(cur, 0.0)
        f = frac
        since = d.get("since")
        if since:  # Aufzeichnung begann erst in diesem Monat → nur über die erfasste Zeit hochrechnen
            ms = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
            if since > ms:
                f = max(0.02, frac - (since - ms) / (days * 86400))
        prog = c / f
        prevs = [mo[k] for k in prev_months(cur, 3) if k in mo]
        avg3 = sum(prevs) / len(prevs) if prevs else None
        trend = (prog / avg3 - 1) if avg3 and avg3 >= 0.5 else None
        prev = mo.get(prev_months(cur, 1)[0])
        year = sum(mo.get(k, 0) for k in [cur] + prev_months(cur, 11))
        sb = d.get("standby_w")
        sb = sb if sb is not None and sb >= s["standby_w"] else None
        row = {"ain": d["ain"], "name": d["name"], "now_w": d.get("now_w"), "today_kwh": d.get("today_kwh"),
               "month_kwh": c, "month_eur": eur(c, price_ct), "prog_kwh": prog, "prog_eur": eur(prog, price_ct),
               "prev_kwh": prev, "prev_eur": None if prev is None else eur(prev, price_ct),
               "avg3_kwh": avg3, "trend": trend, "year_kwh": year, "year_eur": eur(year, price_ct),
               "standby_w": sb, "standby_eur_year": None if sb is None else sb * 8.76 * price_ct / 100}
        rows.append(row)
        if s["outliers"] and trend is not None and trend >= 0.5 and prog - avg3 >= 3 and now.day >= 7:
            alerts.append({"kind": "outlier", "id": f"outlier:{d['ain']}:{cur}",
                           "title": f"⚡ {d['name']} verbraucht ungewöhnlich viel",
                           "text": f"Hochrechnung {fmt(prog, 0)} kWh ({fmt_eur(eur(prog, price_ct))}) statt sonst "
                                   f"≈ {fmt(avg3, 0)} kWh – {fmt(trend * 100, 0)} % mehr"})
    rows.sort(key=lambda r: -(r["prog_eur"] or 0))
    tot_m = sum(r["month_kwh"] for r in rows)
    tot_p = sum(r["prog_kwh"] for r in rows)
    pk = prev_months(cur, 1)[0]
    tot_prev = all_months.get(pk)
    budget = float(s.get("budget_eur") or 0)
    total = {"month_kwh": tot_m, "month_eur": eur(tot_m, price_ct), "prog_kwh": tot_p, "prog_eur": eur(tot_p, price_ct),
             "prev_kwh": tot_prev, "prev_eur": None if tot_prev is None else eur(tot_prev, price_ct),
             "prev_name": month_name(pk), "cur_name": month_name(cur),
             "year_kwh": sum(r["year_kwh"] for r in rows), "year_eur": sum(r["year_eur"] for r in rows),
             "today_kwh": sum(r["today_kwh"] or 0 for r in rows),
             "standby_w": sum(r["standby_w"] or 0 for r in rows),
             "standby_eur_year": sum(r["standby_eur_year"] or 0 for r in rows),
             "budget_eur": budget, "over_budget": bool(budget and eur(tot_p, price_ct) > budget),
             "days": days, "day": now.day}
    total["today_eur"] = eur(total["today_kwh"], price_ct)
    if budget and s["budget_warn"] and total["over_budget"] and now.day >= 5:
        alerts.append({"kind": "budget", "id": f"budget:{cur}", "title": "⚡ Strom: Monatsbudget wird überschritten",
                       "text": f"Hochrechnung {fmt_eur(total['prog_eur'])} – Budget {fmt_eur(budget)}"})
    if s["report"] and now.day == 1 and now.hour >= 8 and tot_prev is not None:
        pp = all_months.get(prev_months(cur, 2)[1])
        cmp = ""
        if pp and abs(tot_prev / pp - 1) < 0.02:
            cmp = " · etwa so viel wie im Monat davor"
        elif pp:
            cmp = f" · {'+' if tot_prev >= pp else '−'}{fmt(abs(tot_prev / pp - 1) * 100, 0)} % ggü. dem Monat davor"
        top = sorted(((r["name"], (r.get("prev_kwh") or 0)) for r in rows), key=lambda x: -x[1])[:3]
        alerts.append({"kind": "report", "id": f"report:{pk}",
                       "title": f"⚡ Strom im {month_name(pk).split()[0]}: {fmt_eur(eur(tot_prev, price_ct))}",
                       "text": f"{fmt(tot_prev, 1)} kWh{cmp}. Am meisten: "
                               + ", ".join(f"{n} {fmt_eur(eur(k, price_ct))}" for n, k in top if k)})
    months = [(k, all_months.get(k)) for k in reversed([cur] + prev_months(cur, 11))]
    return {"devices": rows, "total": total, "months": months, "alerts": alerts, "price_ct": price_ct}


def fmt(v, digits=1):
    if v is None:
        return "–"
    return f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_eur(v):
    return "–" if v is None else fmt(v, 2) + " €"


def fmt_trend(t):
    if t is None:
        return "–"
    if abs(t) < 0.1:
        return "≈ wie sonst"
    return ("📈 +" if t > 0 else "📉 −") + fmt(abs(t) * 100, 0) + " %"


def percentile(values, p=0.1):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(len(v) * p))]
