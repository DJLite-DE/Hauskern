# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
store.py – Dauerhafte Speicherung von Verbrauchswerten und Drucken (SQLite, Standardbibliothek).

Die FRITZ!DECT-Steckdosen liefern einen fortlaufenden Zählerstand in Wh. Wir speichern ihn
einmal pro Minute; der Verbrauch eines Zeitraums ist die Summe der Zählerdifferenzen.
"""
import datetime as dt
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS energy (ain TEXT, ts INTEGER, wh REAL, w REAL);
                CREATE INDEX IF NOT EXISTS ix_energy ON energy (ain, ts);
                CREATE TABLE IF NOT EXISTS devices (ain TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE IF NOT EXISTS prints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, printer TEXT, file TEXT,
                    start INTEGER, end INTEGER, start_wh REAL, end_wh REAL, result TEXT);
                CREATE TABLE IF NOT EXISTS events (ts INTEGER, kind TEXT, name TEXT, text TEXT);
                CREATE INDEX IF NOT EXISTS ix_events ON events (ts);
                CREATE TABLE IF NOT EXISTS known_hosts (mac TEXT PRIMARY KEY, name TEXT, first_seen INTEGER);
                CREATE TABLE IF NOT EXISTS series (key TEXT, ts INTEGER, v REAL, vmin REAL, vmax REAL);
                CREATE INDEX IF NOT EXISTS ix_series ON series (key, ts);
                CREATE TABLE IF NOT EXISTS series_q (key TEXT, ts INTEGER, v REAL, vmin REAL, vmax REAL);
                CREATE INDEX IF NOT EXISTS ix_series_q ON series_q (key, ts);
                CREATE TABLE IF NOT EXISTS series_names (key TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE IF NOT EXISTS calcs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, name TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS battery (ain TEXT, day INTEGER, ts INTEGER, pct REAL, PRIMARY KEY (ain, day));
            """)
        self._last = {}
        for col in ("grams REAL", "filaments TEXT", "est_s INTEGER", "file3mf TEXT"):  # neuere Spalten nachrüsten
            try:
                with self.lock, self.db:
                    self.db.execute(f"ALTER TABLE prints ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self._acc = {}  # key -> [minute, sum, n, min, max]
        self._names = {}
        self.prune()

    # ---------------- Bekannte Geräte im Netzwerk ----------------
    def known_macs(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT mac FROM known_hosts")}

    def add_known(self, rows):
        """rows: [(mac, name)]"""
        now = int(time.time())
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO known_hosts VALUES (?,?,?)", [(m, n, now) for m, n in rows])

    def forget_known(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM known_hosts")

    # ---------------- Verläufe (Temperatur, Internet …) ----------------
    RAW_DAYS = 10      # Minutenwerte so lange behalten, danach 15-Minuten-Mittel
    Q = 900

    def add_series(self, key, v):
        """Sammelt Werte und schreibt pro Minute einen Mittelwert (mit Min/Max)."""
        if v is None:
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        minute = int(time.time()) // 60 * 60
        a = self._acc.get(key)
        if a and a[0] != minute:
            self._flush(key, a)
            a = None
        if a is None:
            self._acc[key] = [minute, v, 1, v, v]
        else:
            a[1] += v
            a[2] += 1
            a[3] = min(a[3], v)
            a[4] = max(a[4], v)

    def _flush(self, key, a):
        with self.lock, self.db:
            self.db.execute("INSERT INTO series VALUES (?,?,?,?,?)", (key, a[0], a[1] / a[2], a[3], a[4]))

    def flush_series(self):
        for k, a in list(self._acc.items()):
            self._flush(k, a)
        self._acc.clear()

    def name_series(self, key, name):
        if self._names.get(key) != name:
            self._names[key] = name
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO series_names VALUES (?,?)", (key, name))

    def series_names(self):
        with self.lock:
            return dict(self.db.execute("SELECT key, name FROM series_names").fetchall())

    def series_keys(self, prefix=""):
        with self.lock:
            rows = self.db.execute("SELECT DISTINCT key FROM series WHERE key LIKE ? UNION "
                                   "SELECT DISTINCT key FROM series_q WHERE key LIKE ?", (prefix + "%", prefix + "%"))
            return sorted(r[0] for r in rows)

    def series(self, key, start, end, bucket=60):
        """[(ts, mittel, min, max)] in Zeitfenstern von `bucket` Sekunden."""
        b = max(60, int(bucket))
        q = ("SELECT (ts / ?) * ? AS t, AVG(v), MIN(vmin), MAX(vmax) FROM ("
             " SELECT ts, v, vmin, vmax FROM series WHERE key = ? AND ts >= ? AND ts < ?"
             " UNION ALL SELECT ts, v, vmin, vmax FROM series_q WHERE key = ? AND ts >= ? AND ts < ?"
             ") GROUP BY t ORDER BY t")
        with self.lock:
            rows = self.db.execute(q, (b, b, key, int(start), int(end), key, int(start), int(end))).fetchall()
        cur = self._acc.get(key)  # angefangene Minute gleich mit anzeigen
        if cur and start <= cur[0] < end:
            t = cur[0] // b * b
            val = (t, cur[1] / cur[2], cur[3], cur[4])
            rows = rows[:-1] + [val] if rows and rows[-1][0] == t else rows + [val]
        return rows

    def power_series(self, ain, start, end, bucket=60):
        """Leistung einer Steckdose aus der Verbrauchstabelle: [(ts, mittel, min, max)]"""
        b = max(60, int(bucket))
        with self.lock:
            return self.db.execute("SELECT (ts / ?) * ? AS t, AVG(w), MIN(w), MAX(w) FROM energy WHERE ain = ? "
                                   "AND ts >= ? AND ts < ? GROUP BY t ORDER BY t",
                                   (b, b, ain, int(start), int(end))).fetchall()

    def consumption(self, ain, start, end, bucket):
        """Verbrauch je Zeitfenster in kWh: [(ts, kwh)] – Fenster nach Ortszeit (Stunde/Tag)."""
        res = {}
        for ts, d in self._deltas(ain, start, end):
            if bucket >= 86400:
                k = int(time.mktime(dt.date.fromtimestamp(ts).timetuple()))
            else:
                k = ts // bucket * bucket
            res[k] = res.get(k, 0.0) + d / 1000
        return sorted(res.items())

    def outages(self, start, end, key="net:online"):
        """Internet-Ausfälle aus dem Verlauf: [(beginn, ende)] – Zeiten ohne Daten zählen nicht."""
        with self.lock:
            rows = self.db.execute("SELECT ts, vmin, 60 FROM series WHERE key = ? AND ts >= ? AND ts < ? UNION ALL "
                                   "SELECT ts, vmin, ? FROM series_q WHERE key = ? AND ts >= ? AND ts < ? ORDER BY ts",
                                   (key, int(start), int(end), self.Q, key, int(start), int(end))).fetchall()
        out, cur = [], None
        last_end = None
        for ts, vmin, length in rows:
            gap = last_end is not None and ts - last_end > 180
            if cur and (vmin >= 0.5 or gap):
                out.append((cur[0], cur[1]))
                cur = None
            if vmin < 0.5:
                cur = (cur[0], ts + length) if cur else (ts, ts + length)
            last_end = ts + length
        if cur:
            out.append(cur)
        return out

    def compact_series(self):
        cutoff = int(time.time()) - self.RAW_DAYS * 86400
        cutoff -= cutoff % self.Q
        with self.lock, self.db:
            self.db.execute("INSERT INTO series_q SELECT key, (ts / ?) * ?, AVG(v), MIN(vmin), MAX(vmax) FROM series "
                            "WHERE ts < ? GROUP BY key, (ts / ?)", (self.Q, self.Q, cutoff, self.Q))
            self.db.execute("DELETE FROM series WHERE ts < ?", (cutoff,))

    # ---------------- Verbrauch ----------------
    def add_energy(self, ain, name, wh, w, min_gap=60):
        now = int(time.time())
        if now - self._last.get(ain, 0) < min_gap:
            return
        self._last[ain] = now
        with self.lock, self.db:
            self.db.execute("INSERT INTO energy VALUES (?,?,?,?)", (ain, now, wh, w))
            self.db.execute("INSERT OR REPLACE INTO devices VALUES (?,?)", (ain, name))

    def power_samples(self, ain, since):
        """Leistungswerte (W) seit `since` – für die Grundlast."""
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT w FROM energy WHERE ain = ? AND ts >= ?", (ain, int(since)))]

    def devices(self):
        with self.lock:
            return dict(self.db.execute("SELECT ain, name FROM devices ORDER BY name").fetchall())

    def _deltas(self, ain, start, end):
        """Liefert [(ts, wh_delta)] – Zählersprünge nach unten (Reset) werden ignoriert."""
        with self.lock:
            q = "SELECT ain, ts, wh FROM energy WHERE ts >= ? AND ts < ?"
            args = [start - 3 * 3600, end]  # etwas Vorlauf, damit der erste Wert eine Basis hat
            if ain:
                q += " AND ain = ?"
                args.append(ain)
            rows = self.db.execute(q + " ORDER BY ain, ts", args).fetchall()
        out, prev = [], {}
        for a, ts, wh in rows:
            p = prev.get(a)
            prev[a] = wh
            if p is None or ts < start:
                continue
            d = wh - p
            if 0 <= d < 50000:
                out.append((ts, d))
        return out

    def kwh(self, ain, start, end):
        return sum(d for _, d in self._deltas(ain, start, end)) / 1000

    def daily(self, ain, days=30):
        today = dt.date.today()
        first = today - dt.timedelta(days=days - 1)
        start = int(time.mktime(first.timetuple()))
        res = {first + dt.timedelta(days=i): 0.0 for i in range(days)}
        for ts, d in self._deltas(ain, start, int(time.time()) + 60):
            day = dt.date.fromtimestamp(ts)
            if day in res:
                res[day] += d / 1000
        return sorted(res.items())

    @staticmethod
    def periods():
        now = time.time()
        today = dt.date.today()
        t0 = int(time.mktime(today.timetuple()))
        week = int(time.mktime((today - dt.timedelta(days=6)).timetuple()))
        month = int(time.mktime(today.replace(day=1).timetuple()))
        return {"heute": (t0, now), "7 Tage": (week, now), "Monat": (month, now)}

    def first_ts(self, ain=None):
        with self.lock:
            if ain:
                r = self.db.execute("SELECT MIN(ts) FROM energy WHERE ain = ?", (ain,)).fetchone()
            else:
                r = self.db.execute("SELECT MIN(ts) FROM energy").fetchone()
        return r[0]

    def prune(self, keep_days=400):
        with self.lock, self.db:
            self.db.execute("DELETE FROM energy WHERE ts < ?", (int(time.time()) - keep_days * 86400,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (int(time.time()) - 90 * 86400,))
            self.db.execute("DELETE FROM series_q WHERE ts < ?", (int(time.time()) - keep_days * 86400,))
        self.compact_series()

    # ---------------- Batterien ----------------
    def add_battery(self, ain, pct, ts=None):
        """Ein Wert pro Gerät und Tag (der letzte des Tages zählt)."""
        ts = int(ts or time.time())
        day = int(time.mktime(time.localtime(ts)[:3] + (0, 0, 0, 0, 0, -1)))
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO battery VALUES (?,?,?,?)", (ain, day, ts, float(pct)))

    def battery_history(self, since=0):
        """{ain: [(ts, pct)]} aufsteigend."""
        out = {}
        with self.lock:
            for ain, ts, pct in self.db.execute("SELECT ain, ts, pct FROM battery WHERE ts >= ? ORDER BY ain, ts",
                                                (int(since),)):
                out.setdefault(ain, []).append((ts, pct))
        return out

    # ---------------- Logbuch ----------------
    def log(self, kind, name, text):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (int(time.time()), kind, name, text))

    def events(self, kind=None, limit=500, search=""):
        q, args = "SELECT ts, kind, name, text FROM events WHERE 1=1", []
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        if search:
            q += " AND (name LIKE ? OR text LIKE ?)"
            args += [f"%{search}%", f"%{search}%"]
        with self.lock:
            return self.db.execute(q + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()

    # ---------------- Drucke ----------------
    def open_print(self, serial):
        with self.lock:
            return self.db.execute("SELECT id, start, start_wh, file FROM prints WHERE serial=? AND end IS NULL "
                                   "ORDER BY id DESC LIMIT 1", (serial,)).fetchone()

    def start_print(self, serial, printer, file, wh):
        with self.lock, self.db:
            self.db.execute("UPDATE prints SET end=?, result='abgebrochen' WHERE serial=? AND end IS NULL",
                            (int(time.time()), serial))
            self.db.execute("INSERT INTO prints (serial, printer, file, start, start_wh) VALUES (?,?,?,?,?)",
                            (serial, printer, file, int(time.time()), wh))

    def set_start_wh(self, pid, wh):
        with self.lock, self.db:
            self.db.execute("UPDATE prints SET start_wh=? WHERE id=? AND start_wh IS NULL", (wh, pid))

    def end_print(self, serial, wh, result):
        with self.lock, self.db:
            self.db.execute("UPDATE prints SET end=?, end_wh=?, result=? WHERE serial=? AND end IS NULL",
                            (int(time.time()), wh, result, serial))

    def prints_between(self, start, end, serial=None):
        q = ("SELECT printer, file, start, end, start_wh, end_wh, result, serial, grams, filaments, id FROM prints "
             "WHERE end IS NOT NULL AND start >= ? AND start < ?")
        args = [int(start), int(end)]
        if serial:
            q += " AND serial = ?"
            args.append(serial)
        with self.lock:
            return self.db.execute(q + " ORDER BY start DESC", args).fetchall()

    def set_print_usage(self, pid, grams, filaments_json, est_s, file3mf):
        with self.lock, self.db:
            self.db.execute("UPDATE prints SET grams=?, filaments=?, est_s=?, file3mf=? WHERE id=?",
                            (grams, filaments_json, est_s, file3mf, pid))

    def print_row(self, pid):
        with self.lock:
            return self.db.execute("SELECT printer, file, start, end, start_wh, end_wh, result, serial, grams, filaments, id "
                                   "FROM prints WHERE id=?", (pid,)).fetchone()

    # ---------------- Kalkulationen ----------------
    def save_calc(self, name, data):
        import json
        with self.lock, self.db:
            self.db.execute("INSERT INTO calcs (ts, name, data) VALUES (?,?,?)",
                            (int(time.time()), name, json.dumps(data, ensure_ascii=False)))

    def calcs(self, limit=200):
        import json
        with self.lock:
            rows = self.db.execute("SELECT id, ts, name, data FROM calcs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [(i, ts, n, json.loads(d)) for i, ts, n, d in rows]

    def delete_calc(self, cid):
        with self.lock, self.db:
            self.db.execute("DELETE FROM calcs WHERE id=?", (cid,))

    def prints(self, limit=50):
        with self.lock:
            return self.db.execute("SELECT printer, file, start, end, start_wh, end_wh, result FROM prints "
                                   "WHERE end IS NOT NULL ORDER BY start DESC LIMIT ?", (limit,)).fetchall()
