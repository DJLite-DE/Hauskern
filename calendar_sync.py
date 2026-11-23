# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
calendar_sync.py – Termine aus Apple iCloud (CalDAV) und Google / beliebigen iCal-Links lesen.
Nur Python-Standardbibliothek. Nur lesen – es wird nichts im Kalender verändert.

  * iCloud:  Apple-ID + app-spezifisches Passwort → alle Kalender werden automatisch gefunden
  * Google:  „Privatadresse im iCal-Format“ (…/basic.ics) einfügen
  * Sonstige: jede iCal-/webcal-Adresse (z. B. Feiertage, Müllkalender, Outlook-Freigaben)
"""
import base64
import datetime as dt
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


class CalendarError(Exception):
    pass


# --------------------------------------------------------------------------- #
# iCalendar (RFC 5545) – das Nötigste inkl. Wiederholungen
# --------------------------------------------------------------------------- #
WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def _prop(line):
    """'DTSTART;TZID=Europe/Berlin:20261115T100000' -> ('DTSTART', {'TZID': ...}, '2026...')"""
    m = re.match(r'^([A-Za-z0-9-]+)((?:;[^:;]+=(?:"[^"]*"|[^:;]*))*):(.*)$', line)
    if not m:
        return None, {}, ""
    params = {}
    for p in re.findall(r';([^=;:]+)=("[^"]*"|[^;:]*)', m.group(2)):
        params[p[0].upper()] = p[1].strip('"')
    return m.group(1).upper(), params, m.group(3)


def _unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def parse_dt(value, params):
    """Gibt (datetime/date, ganztägig) zurück; datetimes sind naive in Ortszeit."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), True
    utc = value.endswith("Z")
    d = dt.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return d.replace(tzinfo=dt.timezone.utc).astimezone(_local_tz()).replace(tzinfo=None), False
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tzid)
            return d.replace(tzinfo=tz).astimezone(_local_tz()).replace(tzinfo=None), False
        except Exception:
            pass  # unbekannte Zeitzone oder keine Zeitzonen-Datenbank (Windows) -> als Ortszeit behandeln
    return d, False


def _as_dt(x):
    return x if isinstance(x, dt.datetime) else dt.datetime.combine(x, dt.time())


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m)


def expand_rrule(start, rule, win_start, win_end, exdates=(), limit=1000):
    """Erzeugt Startzeitpunkte einer Wiederholungsregel im Fenster [win_start, win_end)."""
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = r.get("FREQ", "")
    interval = max(1, int(r.get("INTERVAL", 1) or 1))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = None
    if "UNTIL" in r:
        u, _ = parse_dt(r["UNTIL"], {})
        until = _as_dt(u) if not isinstance(u, dt.datetime) else u
        if not isinstance(start, dt.datetime):
            until = until.replace(hour=23, minute=59, second=59)
    byday = [WD[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in WD]
    bymonthday = [int(x) for x in r.get("BYMONTHDAY", "").split(",") if x.strip().lstrip("-").isdigit()]
    ex = {_as_dt(e) for e in exdates}
    out, n = [], 0
    s0 = _as_dt(start)

    def emit(occ):
        nonlocal n
        if occ < s0:
            return True
        if until and occ > until:
            return False
        n += 1
        if count is not None and n > count:
            return False
        if win_start <= occ < win_end and occ not in ex:
            out.append(occ if isinstance(start, dt.datetime) else occ.date())
        return occ < win_end and len(out) < limit

    period = 0
    for _ in range(100000):
        if freq == "DAILY":
            base = s0 + dt.timedelta(days=period * interval)
            cands = [base] if not byday or base.weekday() in byday else []
        elif freq == "WEEKLY":
            week0 = s0 - dt.timedelta(days=s0.weekday())
            wk = week0 + dt.timedelta(weeks=period * interval)
            days = sorted(byday) if byday else [s0.weekday()]
            cands = [wk + dt.timedelta(days=d) for d in days]
        elif freq == "MONTHLY":
            try:
                base = _add_months(s0.replace(day=1), period * interval)
            except ValueError:
                period += 1
                continue
            cands = []
            if byday and "BYDAY" in r:
                # z. B. 1MO (erster Montag) oder -1FR (letzter Freitag)
                for tok in r["BYDAY"].split(","):
                    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", tok.strip())
                    if not m:
                        continue
                    wd, nth = WD[m.group(2)], int(m.group(1) or 0)
                    days = [base + dt.timedelta(days=i) for i in range(31)
                            if (base + dt.timedelta(days=i)).month == base.month
                            and (base + dt.timedelta(days=i)).weekday() == wd]
                    if nth > 0 and len(days) >= nth:
                        cands.append(days[nth - 1])
                    elif nth < 0 and len(days) >= -nth:
                        cands.append(days[nth])
                    elif nth == 0:
                        cands += days
            else:
                for md in bymonthday or [s0.day]:
                    try:
                        if md < 0:
                            nxt = _add_months(base, 1)
                            cands.append(nxt + dt.timedelta(days=md))
                        else:
                            cands.append(base.replace(day=md))
                    except ValueError:
                        pass  # z. B. 31. im Februar
            cands = [c.replace(hour=s0.hour, minute=s0.minute, second=s0.second) for c in cands]
        elif freq == "YEARLY":
            try:
                cands = [s0.replace(year=s0.year + period * interval)]
            except ValueError:  # 29. Februar
                cands = []
        else:
            return [start] if win_start <= s0 < win_end else []
        cont = True
        for c in sorted(cands):
            if not emit(c):
                cont = False
                break
        if not cont:
            break
        period += 1
    return out


def parse_ics(text, win_start, win_end, calendar="", color=""):
    """Liest VEVENTs und liefert Termine im Fenster [win_start, win_end) (naive Ortszeit)."""
    events, cur, depth = [], None, 0
    for line in _unfold(text):
        if not line:
            continue
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur, depth = {"exdate": [], "params": {}}, 0
            continue
        if cur is None:
            continue
        if up.startswith("BEGIN:"):
            depth += 1  # z. B. VALARM – ignorieren
            continue
        if up.startswith("END:") and depth:
            depth -= 1
            continue
        if up == "END:VEVENT":
            events.append(cur)
            cur = None
            continue
        if depth:
            continue
        name, params, value = _prop(line)
        if not name:
            continue
        if name == "EXDATE":
            for v in value.split(","):
                try:
                    cur["exdate"].append(parse_dt(v, params)[0])
                except ValueError:
                    pass
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            try:
                cur[name] = parse_dt(value, params)
            except ValueError:
                pass
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "DURATION", "STATUS"):
            cur[name] = _unescape(value) if name not in ("RRULE",) else value

    overrides = {}
    for e in events:
        if "RECURRENCE-ID" in e:
            overrides.setdefault(e.get("UID"), set()).add(_as_dt(e["RECURRENCE-ID"][0]))
    out = []
    for e in events:
        if "DTSTART" not in e or e.get("STATUS", "").upper() == "CANCELLED":
            continue
        start, allday = e["DTSTART"]
        if "DTEND" in e:
            dur = _as_dt(e["DTEND"][0]) - _as_dt(start)
        elif "DURATION" in e:
            dur = _parse_duration(e["DURATION"])
        else:
            dur = dt.timedelta(days=1) if allday else dt.timedelta(0)
        if "RRULE" in e and "RECURRENCE-ID" not in e:
            # etwas früher anfangen, damit laufende mehrtägige Termine mitkommen
            starts = expand_rrule(start, e["RRULE"], win_start - dur, win_end,
                                  list(e["exdate"]) + list(overrides.get(e.get("UID"), ())))
        else:
            starts = [start] if _as_dt(start) + max(dur, dt.timedelta(0)) > win_start and _as_dt(start) < win_end \
                or (dur == dt.timedelta(0) and win_start <= _as_dt(start) < win_end) else []
        for s in starts:
            sd = _as_dt(s)
            if sd + dur <= win_start and dur > dt.timedelta(0):
                continue
            out.append({"title": e.get("SUMMARY", "(ohne Titel)"), "location": e.get("LOCATION", ""),
                        "start": sd, "end": sd + dur, "allday": allday, "calendar": calendar, "color": color,
                        "uid": e.get("UID", "")})
    out.sort(key=lambda x: (x["start"], not x["allday"], x["title"]))
    return out


def _parse_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return dt.timedelta(0)
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


# --------------------------------------------------------------------------- #
# Quellen
# --------------------------------------------------------------------------- #
def _opener():
    return urllib.request.build_opener()


def fetch_ics(url, timeout=20):
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    req = urllib.request.Request(url, headers={"User-Agent": "Hauskern-Kalender"})
    try:
        with _opener().open(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise CalendarError("Zugriff verweigert – ist es die Privatadresse (geheime Adresse)?")
        if e.code == 404:
            raise CalendarError("Kalender nicht gefunden (404) – Adresse prüfen")
        raise CalendarError(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise CalendarError(f"nicht erreichbar ({getattr(e, 'reason', e)})")
    text = data.decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        raise CalendarError("Unter der Adresse liegt kein Kalender (iCal)")
    return text


DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
ICAL = "http://apple.com/ns/ical/"


class CalDAV:
    """Minimaler CalDAV-Client (für iCloud, funktioniert auch mit Nextcloud, Posteo, mailbox.org …)."""

    def __init__(self, url, user, password, timeout=20):
        self.url = url.rstrip("/") + "/"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout

    def _req(self, method, url, body, depth="0"):
        for _ in range(5):
            req = urllib.request.Request(url, data=body.encode("utf-8"), method=method, headers={
                "Authorization": self.auth, "Depth": depth, "Content-Type": "application/xml; charset=utf-8",
                "User-Agent": "Hauskern-Kalender"})
            try:
                with _opener().open(req, timeout=self.timeout) as r:
                    return r.geturl() or url, ET.fromstring(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 307, 308) and e.headers.get("Location"):
                    url = urllib.parse.urljoin(url, e.headers["Location"])
                    continue
                if e.code == 401:
                    raise CalendarError("Anmeldung abgelehnt – Apple-ID und app-spezifisches Passwort prüfen")
                raise CalendarError(f"Kalenderserver meldet HTTP {e.code}")
            except (urllib.error.URLError, OSError) as e:
                raise CalendarError(f"Kalenderserver nicht erreichbar ({getattr(e, 'reason', e)})")
            except ET.ParseError:
                raise CalendarError("Ungültige Antwort vom Kalenderserver")
        raise CalendarError("Zu viele Weiterleitungen")

    @staticmethod
    def _href(root, *path):
        for resp in root.iter(f"{{{DAV}}}response"):
            el = resp
            for p in path:
                el = el.find(f".//{p}") if el is not None else None
            if el is not None:
                h = el.find(f"{{{DAV}}}href")
                if h is not None and h.text:
                    return h.text.strip()
        return None

    def calendars(self):
        """Liefert [{href, name, color}] aller Kalender mit Terminen."""
        base, root = self._req("PROPFIND", self.url,
                               f'<d:propfind xmlns:d="{DAV}"><d:prop><d:current-user-principal/></d:prop></d:propfind>')
        principal = self._href(root, f"{{{DAV}}}current-user-principal")
        if not principal:
            raise CalendarError("Kalenderkonto nicht gefunden")
        purl = urllib.parse.urljoin(base, principal)
        base, root = self._req("PROPFIND", purl,
                               f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop><c:calendar-home-set/>'
                               "</d:prop></d:propfind>")
        home = self._href(root, f"{{{CALDAV}}}calendar-home-set")
        if not home:
            raise CalendarError("Keine Kalender im Konto gefunden")
        hurl = urllib.parse.urljoin(base, home)
        base, root = self._req("PROPFIND", hurl,
                               f'<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}" xmlns:i="{ICAL}"><d:prop>'
                               "<d:resourcetype/><d:displayname/><i:calendar-color/>"
                               "<c:supported-calendar-component-set/></d:prop></d:propfind>", depth="1")
        cals = []
        for resp in root.iter(f"{{{DAV}}}response"):
            rt = resp.find(f".//{{{DAV}}}resourcetype")
            if rt is None or rt.find(f"{{{CALDAV}}}calendar") is None:
                continue
            comps = [c.get("name") for c in resp.iter(f"{{{CALDAV}}}comp")]
            if comps and "VEVENT" not in comps:
                continue  # z. B. Erinnerungen
            href = resp.find(f"{{{DAV}}}href").text.strip()
            name = (resp.findtext(f".//{{{DAV}}}displayname") or "").strip() or href.rstrip("/").rsplit("/", 1)[-1]
            color = (resp.findtext(f".//{{{ICAL}}}calendar-color") or "").strip()[:7]
            cals.append({"href": urllib.parse.urljoin(base, href), "name": name, "color": color})
        return cals

    def events(self, cal_href, start, end, name="", color=""):
        f = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
        s, e = start.astimezone(), end.astimezone()
        body = (f'<c:calendar-query xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop><c:calendar-data/></d:prop>'
                '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{f(s)}" end="{f(e)}"/></c:comp-filter></c:comp-filter></c:filter>'
                "</c:calendar-query>")
        _, root = self._req("REPORT", cal_href, body, depth="1")
        out = []
        for cd in root.iter(f"{{{CALDAV}}}calendar-data"):
            if cd.text:
                out += parse_ics(cd.text, start, end, name, color)
        return out


ICLOUD_URL = "https://caldav.icloud.com/"


def load_source(src, password, start, end):
    """src: {"type": "icloud"|"ics", "name", "color", "url", "user", "calendars": [href…] oder leer = alle}"""
    if src.get("type") == "ics":
        return parse_ics(fetch_ics(src["url"]), start, end, src.get("name", "Kalender"), src.get("color", ""))
    dav = CalDAV(src.get("url") or ICLOUD_URL, src.get("user", ""), password)
    out = []
    chosen = set(src.get("calendars") or [])
    for cal in dav.calendars():
        if chosen and cal["href"] not in chosen:
            continue
        out += dav.events(cal["href"], start, end, cal["name"], src.get("color") or cal["color"])
    return out
