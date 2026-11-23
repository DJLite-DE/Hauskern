# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
health.py – Gesundheit des Rechners, auf dem Hauskern läuft (Heimserver: Raspberry Pi oder Mini-PC).

    snapshot(data_dir) → {"pi", "model", "temp", "throttle": {...}, "storage": {...}, "mem", "uptime", "load"}
    problems(snap)     → [(schlüssel, stufe "warn"|"bad", titel, text)] – für Push und Anzeige

Unterspannung erkennt der Pi selbst (vcgencmd get_throttled). Auf Windows gibt es Speicherplatz und den
SSD-Zustand laut Windows.

SSD-Zustand (Linux): Ein kleiner Systemdienst aus install.sh liest alle 6 Stunden die Selbstdiagnose (SMART) jeder
Platte und legt sie als JSON in /var/lib/hauskern ab. Hauskern selbst braucht dafür keine Admin-Rechte.
"""
import os
import re
import shutil
import subprocess

TEMP_WARN, TEMP_BAD = 75.0, 82.0        # Raspberry Pi (drosselt ab ca. 80–85 °C)
TEMP_WARN_PC, TEMP_BAD_PC = 85.0, 95.0  # Mini-PC / normaler PC (Intel/AMD vertragen mehr)
JUNK = ("to be filled", "default string", "system product name", "system manufacturer", "not specified", "o.e.m",
        "none", "invalid")
THROTTLE_BITS = {0: "Unterspannung jetzt", 1: "Takt begrenzt jetzt", 2: "gedrosselt jetzt", 3: "Temperaturgrenze jetzt",
                 16: "Unterspannung seit Start", 17: "Takt begrenzt seit Start", 18: "gedrosselt seit Start",
                 19: "Temperaturgrenze seit Start"}


def _read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read().strip("\x00 \n")
    except OSError:
        return ""


def pi_model():
    m = _read("/proc/device-tree/model")
    return m if "Raspberry Pi" in m else ""


def pc_model():
    """Mini-PC/PC unter Linux: Hersteller + Modell aus dem BIOS (z. B. „Lenovo ThinkCentre M720q“)."""
    vendor = _read("/sys/class/dmi/id/sys_vendor")
    name = _read("/sys/class/dmi/id/product_name")
    ver = _read("/sys/class/dmi/id/product_version")
    ok = lambda x: x and not any(j in x.lower() for j in JUNK)  # noqa: E731
    if vendor.upper() == "LENOVO" and ok(ver):  # Lenovo: Modellname steht in product_version
        name = ver
    vendor = {"LENOVO": "Lenovo", "Dell Inc.": "Dell", "HP": "HP", "Hewlett-Packard": "HP",
              "FUJITSU": "Fujitsu", "ASUSTeK COMPUTER INC.": "ASUS", "Intel Corporation": "Intel"}.get(vendor, vendor)
    parts = [x for x in (vendor, name) if ok(x)]
    if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
        parts = parts[1:]
    if not parts:
        board = _read("/sys/class/dmi/id/board_name")
        parts = [board] if ok(board) else []
    return " ".join(parts)


def _temps(base="/sys/class"):
    """Alle Temperaturfühler: [(art, °C)] aus thermal_zone* und hwmon* (coretemp, k10temp …)."""
    out = []
    tz = os.path.join(base, "thermal")
    try:
        for z in sorted(os.listdir(tz)):
            if z.startswith("thermal_zone"):
                typ = _read(os.path.join(tz, z, "type"))
                t = _read(os.path.join(tz, z, "temp"))
                if t.lstrip("-").isdigit():
                    out.append((typ, int(t) / 1000))
    except OSError:
        pass
    hw = os.path.join(base, "hwmon")
    try:
        for h in sorted(os.listdir(hw)):
            name = _read(os.path.join(hw, h, "name"))
            t = _read(os.path.join(hw, h, "temp1_input"))
            if t.lstrip("-").isdigit():
                out.append((name, int(t) / 1000))
    except OSError:
        pass
    return out


def cpu_temp(base="/sys/class"):
    """CPU-Temperatur: Pi „cpu-thermal“, Intel „x86_pkg_temp“/„coretemp“, AMD „k10temp“ – sonst der wärmste Fühler."""
    temps = [(k, v) for k, v in _temps(base) if 0 < v < 125]
    if not temps:
        return None
    for want in ("cpu-thermal", "x86_pkg_temp", "coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal"):
        hit = [v for k, v in temps if k == want]
        if hit:
            return round(max(hit), 1)
    return round(max(v for k, v in temps if k not in ("acpitz", "nvme", "iwlwifi_1", "pch_cannonlake")) if any(
        k not in ("acpitz", "nvme", "iwlwifi_1", "pch_cannonlake") for k, _ in temps) else max(v for _, v in temps), 1)


def parse_throttled(text):
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", text or "")
    if not m:
        return None
    v = int(m.group(1), 16)
    return {"raw": m.group(1), "under_now": bool(v & 1), "under_ever": bool(v & (1 << 16)),
            "throttled_now": bool(v & 0b1110), "throttled_ever": bool(v & (0b1110 << 16)),
            "flags": [t for b, t in THROTTLE_BITS.items() if v & (1 << b)]}


def throttled():
    for exe in ("vcgencmd", "/usr/bin/vcgencmd"):
        try:
            r = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                return parse_throttled(r.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def parse_mounts(text):
    out = []
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) >= 2:
            out.append((p[0], p[1].replace("\\040", " ")))
    return out


def storage_kind(dev, rotational=None, usb=None):
    d = os.path.basename(dev or "")
    if usb is None and re.match(r"sd[a-z]", d):
        usb = "/usb" in os.path.realpath(f"/sys/block/{d}")  # SATA im Mini-PC oder USB-Gehäuse am Pi?
    if d.startswith("mmcblk"):
        return "SD-Karte"
    if d.startswith("nvme"):
        return "NVMe-SSD"
    if re.match(r"sd[a-z]", d):
        if usb:
            return "USB-Festplatte" if rotational else "USB-SSD/-Laufwerk"
        return "Festplatte (SATA)" if rotational else "SATA-SSD"
    if re.match(r"(vd|xvd)[a-z]", d):
        return "virtuelle Festplatte"
    if d.startswith(("root", "overlay")):
        return "System"
    return d or "?"


def storage(path, mounts_text=None):
    """Wo liegen Hauskerns Daten? Art des Speichers + freier Platz."""
    out = {"path": path}
    try:
        u = shutil.disk_usage(path)
        out.update(total=u.total, free=u.free, used_pct=round(u.used / u.total * 100, 1) if u.total else 0)
    except OSError:
        pass
    if os.name == "nt":
        out["kind"] = "Festplatte/SSD"
        return out
    dev, mnt = "", ""
    if mounts_text is None:
        try:  # findmnt kennt auch „/dev/root“ (so meldet Raspberry Pi OS die Systempartition)
            r = subprocess.run(["findmnt", "-n", "-o", "SOURCE,TARGET", "--target", path], capture_output=True,
                               text=True, timeout=4)
            if r.returncode == 0 and r.stdout.split():
                dev, mnt = r.stdout.split()[:2]
        except (OSError, subprocess.SubprocessError):
            pass
    if not dev or dev == "/dev/root":
        mounts = parse_mounts(mounts_text if mounts_text is not None else _read("/proc/mounts"))
        real = os.path.realpath(path)
        best = max((m for m in mounts if real == m[1] or real.startswith(m[1].rstrip("/") + "/")),
                   key=lambda m: len(m[1]), default=None)
        if best:
            dev, mnt = best
    if dev == "/dev/root" and os.path.exists("/sys/block/mmcblk0") and not os.path.exists("/sys/block/sda") \
            and not os.path.exists("/sys/block/nvme0n1"):
        dev = "/dev/mmcblk0p2"
    if dev:
        name = os.path.basename(dev)
        base = re.sub(r"p\d+$", "", name) if name.startswith(("nvme", "mmcblk")) else re.sub(r"\d+$", "", name)
        rot = _read(f"/sys/block/{base}/queue/rotational")
        out.update(device=dev, mount=mnt, kind=storage_kind(base, rot == "1"))
    return out


SMART_DIR = "/var/lib/hauskern"
SMART_MAX_AGE = 3 * 86400
LIFE_WARN, LIFE_BAD = 25, 10  # Prozent Lebensdauer übrig


def _attr(table, *ids):
    for a in table or []:
        if a.get("id") in ids:
            return a
    return None


def parse_smart(j):
    """smartctl -j → {"passed", "life", "realloc", "pending", "media_err", "model"} (fehlende Werte = None)."""
    out = {"passed": None, "life": None, "realloc": None, "pending": None, "media_err": None,
           "model": (j.get("model_name") or j.get("model_family") or "").strip()}
    st = j.get("smart_status")
    if isinstance(st, dict) and "passed" in st:
        out["passed"] = bool(st["passed"])
    nv = j.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        if nv.get("percentage_used") is not None:
            out["life"] = max(0, 100 - int(nv["percentage_used"]))
        out["media_err"] = nv.get("media_errors")
        spare, thr = nv.get("available_spare"), nv.get("available_spare_threshold")
        if spare is not None and thr is not None and spare <= thr:
            out["passed"] = False
        if nv.get("critical_warning"):
            out["passed"] = False
        return out
    table = (j.get("ata_smart_attributes") or {}).get("table") or []
    a = _attr(table, 5)
    if a:
        out["realloc"] = (a.get("raw") or {}).get("value")
    pend = [(_attr(table, i) or {}).get("raw", {}).get("value") for i in (197, 198)]
    pend = [x for x in pend if x is not None]
    if pend:
        out["pending"] = sum(pend)
    for i in (231, 233, 202, 177, 169):  # „Lebensdauer übrig“ – je Hersteller ein anderes Feld
        a = _attr(table, i)
        if a and a.get("value") is not None and 0 <= int(a["value"]) <= 100:
            v = int(a["value"])
            out["life"] = v  # normierter Wert: 100 = neu
            break
    return out


def smart_linux(base, folder=SMART_DIR, now=None):
    import json
    import time as _t
    p = os.path.join(folder, f"smart-{base}.json")
    try:
        age = (now or _t.time()) - os.path.getmtime(p)
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
    except (OSError, ValueError):
        return None
    if age > SMART_MAX_AGE or not isinstance(j, dict) or not (j.get("smart_status") or j.get("ata_smart_attributes")
                                                              or j.get("nvme_smart_health_information_log")):
        return None
    return parse_smart(j)


_WIN_CACHE = {"t": 0, "v": None}


def smart_windows():
    """Windows meldet je Laufwerk „Healthy/Warning/Unhealthy“ – ohne Admin-Rechte. Alle 6 Std. neu fragen."""
    import json
    import time as _t
    if _t.time() - _WIN_CACHE["t"] < 6 * 3600:
        return _WIN_CACHE["v"]
    _WIN_CACHE["t"] = _t.time()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-PhysicalDisk | Select-Object FriendlyName,HealthStatus | ConvertTo-Json"],
                           capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        data = json.loads(r.stdout or "null")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if isinstance(data, dict):
        data = [data]
    bad = [d.get("FriendlyName", "?") for d in data or [] if str(d.get("HealthStatus")) not in ("Healthy", "0")]
    _WIN_CACHE["v"] = {"passed": not bad, "life": None, "realloc": None, "pending": None, "media_err": None,
                       "model": ", ".join(bad) if bad else ""} if data else None
    return _WIN_CACHE["v"]


def readonly_mount(dev, mounts_text=None):
    """Hat Linux die Partition schreibgeschützt eingebunden? Passiert bei Dateisystemfehlern."""
    for line in (mounts_text if mounts_text is not None else _read("/proc/mounts")).splitlines():
        p = line.split()
        if len(p) >= 4 and p[0] == dev:
            return "ro" in p[3].split(",")
    return False


def disk_text(d):
    """Kurztext für die Anzeige, z. B. „gut · 96 % Lebensdauer übrig“."""
    if not d:
        return ""
    if d.get("passed") is False:
        return "⚠ meldet einen Defekt"
    bits = ["gut"]
    if d.get("life") is not None:
        bits.append(f"{d['life']} % Lebensdauer übrig")
    if (d.get("pending") or 0) + (d.get("media_err") or 0) > 0:
        bits[0] = "⚠ Lesefehler"
    elif (d.get("realloc") or 0) > 0:
        bits[0] = "ok, einzelne Ersatzsektoren"
    return " · ".join(bits)


def meminfo():
    t = _read("/proc/meminfo")
    vals = {k: int(v) * 1024 for k, v in re.findall(r"^(\w+):\s+(\d+)", t, re.M)}
    if "MemTotal" in vals:
        return {"total": vals["MemTotal"], "available": vals.get("MemAvailable", 0)}
    return None


def disk_health(st):
    """SMART-Zustand des Laufwerks, auf dem Hauskerns Daten liegen (oder None, wenn unbekannt)."""
    if os.name == "nt":
        return smart_windows()
    dev = st.get("device") or ""
    if not dev or (st.get("kind") or "") in ("SD-Karte", "virtuelle Festplatte", "System"):
        return None
    name = os.path.basename(dev)
    base = re.sub(r"p\d+$", "", name) if name.startswith(("nvme", "mmcblk")) else re.sub(r"\d+$", "", name)
    d = smart_linux(base) or {}
    if readonly_mount(dev):
        d["readonly"] = True
    return d or None


def snapshot(data_dir):
    model = pi_model()
    st = storage(data_dir)
    s = {"pi": bool(model), "model": model or (pc_model() if os.name != "nt" else ""), "temp": cpu_temp(),
         "throttle": throttled() if model else None, "storage": st, "mem": meminfo()}
    try:
        s["disk"] = disk_health(st)
    except Exception:  # noqa: BLE001 – die SSD-Prüfung darf den Gesundheitscheck nie kaputt machen
        s["disk"] = None
    up = _read("/proc/uptime")
    if up:
        s["uptime"] = int(float(up.split()[0]))
    ld = _read("/proc/loadavg")
    if ld:
        s["load"] = float(ld.split()[0])
    return s


def problems(s):
    out = []
    t = s.get("temp")
    pi = s.get("pi")
    who = "Raspberry Pi" if pi else "Heimserver"
    warn, bad = (TEMP_WARN, TEMP_BAD) if pi else (TEMP_WARN_PC, TEMP_BAD_PC)
    if t is not None and t >= bad:
        out.append(("temp", "bad", f"🌡 {who} ist heiß ({t:.0f} °C)",
                    "Er bremst sich selbst. Gehäuse belüften, Lüfter/Kühlkörper prüfen." if pi else
                    "Er bremst sich selbst. Lüftungsschlitze frei? Lüfter verstaubt?"))
    elif t is not None and t >= warn:
        out.append(("temp", "warn", f"🌡 {who} wird warm ({t:.0f} °C)", "Für Luft ums Gehäuse sorgen."))
    th = s.get("throttle") or {}
    if th.get("under_now") or th.get("under_ever"):
        out.append(("power", "bad", "⚡ Raspberry Pi bekommt zu wenig Strom",
                    "Unterspannung erkannt – das offizielle Netzteil nehmen und kurze, dicke Kabel. Mit einer "
                    "USB-SSD erst recht."))
    st = s.get("storage") or {}
    if st.get("free") is not None and st.get("total"):
        gb = st["free"] / 1e9
        if gb < 1 or st.get("used_pct", 0) >= 97:
            out.append(("disk", "bad", f"💾 Speicher fast voll ({gb:.1f} GB frei)".replace(".", ","), "Alte Aufnahmen/Sicherungen löschen."))
        elif gb < 3 or st.get("used_pct", 0) >= 90:
            out.append(("disk", "warn", f"💾 Speicher wird knapp ({gb:.1f} GB frei)".replace(".", ","), "Bald aufräumen."))
    d = s.get("disk") or {}
    if d.get("readonly"):
        out.append(("ssd_ro", "bad", "💽 Speicher ist schreibgeschützt",
                    "Linux hat Fehler auf dem Laufwerk gefunden und lässt nichts mehr speichern. Neu starten; kommt "
                    "es wieder, ist das Laufwerk am Ende – Sicherung bereithalten."))
    if d.get("passed") is False:
        out.append(("ssd", "bad", "💽 SSD meldet einen Defekt" + (f" ({d['model']})" if d.get("model") else ""),
                    "Die SSD sagt selbst, dass sie bald ausfällt. Prüf, ob die letzte Sicherung geklappt hat, "
                    "und tausch die SSD zeitnah."))
    elif (d.get("pending") or 0) + (d.get("media_err") or 0) > 0:
        n = (d.get("pending") or 0) + (d.get("media_err") or 0)
        out.append(("ssd", "bad", f"💽 SSD hat Lesefehler ({n})",
                    "Erste Stellen lassen sich nicht mehr sicher lesen. Sicherung prüfen und SSD bald tauschen."))
    elif d.get("life") is not None and d["life"] <= LIFE_BAD:
        out.append(("ssd", "bad", f"💽 SSD fast verbraucht (noch {d['life']} %)", "Bald tauschen – Sicherung prüfen."))
    elif d.get("life") is not None and d["life"] <= LIFE_WARN:
        out.append(("ssd", "warn", f"💽 SSD nutzt sich ab (noch {d['life']} %)", "Kein Grund zur Eile, aber im Blick "
                                                                                  "behalten."))
    elif (d.get("realloc") or 0) >= 50:
        out.append(("ssd", "warn", f"💽 SSD hat {d['realloc']} Ersatzsektoren benutzt",
                    "Noch funktioniert alles. Steigt die Zahl weiter, die SSD tauschen."))
    m = s.get("mem")
    if m and m["total"] and m["available"] / m["total"] < 0.05:
        out.append(("mem", "warn", "🧠 Arbeitsspeicher fast voll", "Hauskern neu starten; hilft das nicht, Bescheid geben."))
    return out


def fmt_uptime(sec):
    d, r = divmod(int(sec or 0), 86400)
    h = r // 3600
    return f"{d} Tg. {h} Std." if d else f"{h} Std. {(r % 3600) // 60} Min."



def timezone():
    """Zeitzone des Rechners (Linux: /etc/timezone bzw. Link /etc/localtime) → (Name, Abweichung in Stunden)."""
    import time as _t
    name = (os.environ.get("TZ") or "").lstrip(":")  # gilt für das Programm vor der Systemeinstellung
    if name.startswith("/"):
        name = name.split("zoneinfo/")[-1] if "zoneinfo/" in name else ""
    try:
        name = name or open("/etc/timezone", encoding="utf-8").read().strip()
    except OSError:
        try:
            link = os.readlink("/etc/localtime")
            name = link.split("zoneinfo/")[-1] if "zoneinfo/" in link else ""
        except OSError:
            pass
    off = -(_t.altzone if _t.daylight and _t.localtime().tm_isdst else _t.timezone) / 3600
    return name or _t.tzname[0], off


GERMAN_LIKE = ("Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Amsterdam", "Europe/Brussels",
               "Europe/Paris", "Europe/Luxembourg", "Europe/Copenhagen", "Europe/Rome", "Europe/Madrid",
               "Europe/Prague", "Europe/Warsaw", "Europe/Busingen", "CET", "MET")


def tz_suspicious(name):
    """UTC/London auf einem Pi in Deutschland = meist vergessen umzustellen."""
    return name in ("Etc/UTC", "UTC", "Universal", "Etc/Universal", "GMT", "Etc/GMT", "Europe/London", "Zulu")
