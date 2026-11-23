# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
backup.py – Sicherungen von Hauskerns Daten (Einstellungen, Verlauf, Druck- und Stromdaten).

    create(data_dir, dest, db_path, version, kind="auto") → Pfad der neuen ZIP
    prune(dest)                                             → behält 7 Tage + 4 Wochen + 5 manuelle
    list_backups(dest)                                      → [{"name","path","ts","size","meta"}], neueste zuerst
    stage_restore(zip_path, data_dir)                       → legt die Sicherung für den nächsten Start bereit
    apply_pending(data_dir)                                 → beim Start, BEVOR die Datenbank geöffnet wird

Die Datenbank wird mit der SQLite-Sicherungsfunktion kopiert – auch während Hauskern läuft in sich stimmig.
Videos, ffmpeg und alte Programmversionen gehören nicht in die Sicherung.
"""
import datetime as dt
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import time
import zipfile

FILES = ["config.json", "stromstatistik.json", "geraetesuche.json", "diagnose.json"]
PREFIX = "hauskern-sicherung-"
PENDING = "wiederherstellen"


class BackupError(Exception):
    pass


def create(data_dir, dest, db_path, version, kind="auto"):
    os.makedirs(dest, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(now))
    name = f"{PREFIX}{stamp}{'-manuell' if kind == 'manual' else ''}.zip"
    final = os.path.join(dest, name)
    tmp = final + ".tmp"
    meta = {"app": "Hauskern", "version": version, "created": int(now), "kind": kind, "host": platform.node(),
            "os": os.name, "files": []}
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            if db_path and os.path.exists(db_path):
                snap = os.path.join(td, "verlauf.db")
                src = sqlite3.connect(db_path, timeout=30)
                dst = sqlite3.connect(snap)
                try:
                    src.backup(dst)  # konsistente Kopie, auch während geschrieben wird
                finally:
                    dst.close()
                    src.close()
                z.write(snap, "verlauf.db")
                meta["files"].append("verlauf.db")
            for f in FILES:
                p = os.path.join(data_dir, f)
                if os.path.isfile(p):
                    z.write(p, f)
                    meta["files"].append(f)
            z.writestr("sicherung.json", json.dumps(meta, ensure_ascii=False, indent=1))
    os.replace(tmp, final)
    return final


def read_meta(path):
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("sicherung.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def list_backups(dest):
    out = []
    try:
        names = os.listdir(dest)
    except OSError:
        return out
    for n in names:
        if not (n.startswith(PREFIX) and n.endswith(".zip")):
            continue
        p = os.path.join(dest, n)
        meta = read_meta(p) or {}
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": n, "path": p, "ts": meta.get("created") or int(os.path.getmtime(p)), "size": size,
                    "meta": meta, "manual": n.endswith("-manuell.zip")})
    out.sort(key=lambda b: -b["ts"])
    return out


def prune(dest, keep_days=7, keep_weeks=4, keep_manual=5, now=None):
    """Behält die neueste Sicherung je Tag (7 Tage), je Woche (4 Wochen) und die letzten manuellen."""
    now = now or time.time()
    keep = set()
    days, weeks = {}, {}
    bs = list_backups(dest)
    for b in bs:
        if b["manual"]:
            continue
        d = dt.date.fromtimestamp(b["ts"])
        age = (dt.date.fromtimestamp(now) - d).days
        if age < keep_days and d not in days:
            days[d] = b["name"]
        wk = d.isocalendar()[:2]
        if age < keep_weeks * 7 + 7 and wk not in weeks:
            weeks[wk] = b["name"]
    keep |= set(days.values()) | set(list(weeks.values())[:keep_weeks + 1])
    keep |= {b["name"] for b in [x for x in bs if x["manual"]][:keep_manual]}
    if bs:
        keep.add(bs[0]["name"])  # die neueste immer
    removed = []
    for b in bs:
        if b["name"] not in keep:
            try:
                os.remove(b["path"])
                removed.append(b["name"])
            except OSError:
                pass
    return removed


def copy_to(path, dest, keep=7):
    """Weitere Kopie (USB-Stick, NAS, PC) – dort nur die letzten `keep`."""
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, os.path.basename(path))
    tmp = target + ".tmp"
    shutil.copyfile(path, tmp)
    os.replace(tmp, target)
    for b in list_backups(dest)[keep:]:
        try:
            os.remove(b["path"])
        except OSError:
            pass
    return target


def check(path):
    """Ist die ZIP eine brauchbare Hauskern-Sicherung?"""
    meta = read_meta(path)
    if not meta or meta.get("app") != "Hauskern":
        raise BackupError("Das ist keine Hauskern-Sicherung")
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise BackupError(f"Sicherung beschädigt ({bad})")
        names = z.namelist()
        if "config.json" not in names:
            raise BackupError("In der Sicherung fehlen die Einstellungen")
        json.loads(z.read("config.json"))
        if "verlauf.db" in names:
            with tempfile.TemporaryDirectory() as td:
                z.extract("verlauf.db", td)
                con = sqlite3.connect(os.path.join(td, "verlauf.db"))
                try:
                    ok = con.execute("PRAGMA quick_check").fetchone()[0]
                finally:
                    con.close()
                if ok != "ok":
                    raise BackupError("Die Datenbank in der Sicherung ist beschädigt")
    return meta


def stage_restore(path, data_dir):
    """Prüft die Sicherung und legt sie für den nächsten Start bereit (die laufende Datenbank bleibt unberührt)."""
    meta = check(path)
    stage = os.path.join(data_dir, PENDING)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n in FILES + ["verlauf.db", "sicherung.json"]:
                z.extract(n, stage)
    return meta


def apply_pending(data_dir, log=None):
    """Beim Start: bereitgelegte Sicherung einspielen. Die bisherigen Dateien wandern in einen Ordner daneben."""
    stage = os.path.join(data_dir, PENDING)
    if not os.path.isdir(stage) or not os.path.exists(os.path.join(stage, "config.json")):
        return None
    keep = os.path.join(data_dir, "vor-wiederherstellung-" + time.strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(keep, exist_ok=True)
    for n in FILES + ["verlauf.db", "verlauf.db-wal", "verlauf.db-shm"]:
        p = os.path.join(data_dir, n)
        if os.path.exists(p):
            shutil.move(p, os.path.join(keep, n))
    for n in os.listdir(stage):
        if n != "sicherung.json":
            shutil.move(os.path.join(stage, n), os.path.join(data_dir, n))
    meta = read_meta_file(os.path.join(stage, "sicherung.json"))
    shutil.rmtree(stage, ignore_errors=True)
    if log:
        log(f"Sicherung vom {fmt_ts(meta.get('created'))} eingespielt – vorherige Daten in {os.path.basename(keep)}")
    return meta


def read_meta_file(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fmt_ts(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "?"


def fmt_size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} kB"


# --------------------------------------------------------------------------- #
# Netzwerkspeicher (z. B. SSD an der FRITZ!Box = FRITZ!NAS) über FTP
# --------------------------------------------------------------------------- #
def _is_lan(host):
    import ipaddress
    import socket
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return host.endswith((".box", ".local", ".lan", ".fritz.box"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def split_host(host, port=21):
    """„fritz.box“ oder „192.168.178.1:2121“ → (Adresse, Port)"""
    h = str(host or "").strip()
    if h.count(":") == 1 and h.rsplit(":", 1)[1].isdigit():
        h, p = h.rsplit(":", 1)
        return h, int(p)
    return h, port


def nas_connect(host, user, pw, port=21, timeout=20):
    """Erst verschlüsselt (FTPS); kann die Gegenstelle das nicht, im Heimnetz unverschlüsselt.
    Gibt (ftp, verschlüsselt) zurück."""
    import ftplib
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # FRITZ!Box hat ein selbst ausgestelltes Zertifikat
    try:
        f = ftplib.FTP_TLS(context=ctx, timeout=timeout)
        f.connect(host, port)
        f.auth()
        f.login(user, pw)
        f.prot_p()
        return f, True
    except ftplib.error_perm as e:
        if str(e)[:3] in ("530", "430"):
            raise BackupError("FRITZ!Box-Benutzer oder Passwort falsch") from e
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass
    except (OSError, EOFError, ftplib.Error) as e:
        if isinstance(e, OSError) and not isinstance(e, ssl.SSLError):
            raise BackupError(f"Netzwerkspeicher nicht erreichbar ({e})") from e
    if not _is_lan(host):
        raise BackupError("Der Netzwerkspeicher kann keine Verschlüsselung – außerhalb des Heimnetzes wird das abgelehnt")
    f = ftplib.FTP(timeout=timeout)
    try:
        f.connect(host, port)
        f.login(user, pw)
    except ftplib.error_perm as e:
        raise BackupError("FRITZ!Box-Benutzer oder Passwort falsch") from e
    except (OSError, EOFError) as e:
        raise BackupError(f"Netzwerkspeicher nicht erreichbar ({e})") from e
    return f, False


def _nas_cd(f, folder):
    import ftplib
    for part in [p for p in (folder or "").replace("\\", "/").split("/") if p]:
        try:
            f.cwd(part)
        except ftplib.error_perm:
            try:
                f.mkd(part)
                f.cwd(part)
            except ftplib.error_perm as e:
                raise BackupError(f"Ordner „{part}“ lässt sich auf dem Netzwerkspeicher nicht anlegen ({e})") from e


def _nas_list(f):
    import ftplib
    try:
        names = f.nlst()
    except ftplib.error_perm:  # leerer Ordner
        return []
    return sorted(os.path.basename(n) for n in names if os.path.basename(n).startswith(PREFIX)
                  and n.endswith(".zip"))


def nas_upload(path, host, user, pw, folder="Hauskern-Sicherung", keep=14, port=21):
    """Lädt die Sicherung hoch und behält dort die letzten `keep`. Gibt (Name, verschlüsselt, Anzahl) zurück."""
    import ftplib
    host, port = split_host(host, port)
    f, tls = nas_connect(host, user, pw, port)
    try:
        _nas_cd(f, folder)
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            f.storbinary(f"STOR {name}.part", fh)
        try:
            f.delete(name)
        except ftplib.error_perm:
            pass
        f.rename(f"{name}.part", name)
        have = _nas_list(f)
        for old in have[:-keep] if keep else []:
            try:
                f.delete(old)
            except ftplib.error_perm:
                pass
        return name, tls, min(len(have), keep)
    except ftplib.all_errors as e:
        raise BackupError(f"Hochladen fehlgeschlagen: {e}") from e
    finally:
        try:
            f.quit()
        except Exception:  # noqa: BLE001
            f.close()


def nas_test(host, user, pw, folder="Hauskern-Sicherung", port=21):
    """Verbindung prüfen. Gibt (verschlüsselt, Anzahl Sicherungen dort, Ordner im Hauptverzeichnis) zurück."""
    import ftplib
    host, port = split_host(host, port)
    f, tls = nas_connect(host, user, pw, port)
    try:
        try:
            roots = [os.path.basename(n) for n in f.nlst() if os.path.basename(n) not in (".", "..")]
        except ftplib.error_perm:
            roots = []
        try:
            _nas_cd(f, folder)
        except BackupError as e:
            pick = next((r for r in roots if r.upper() != "FRITZ"), (roots or ["Laufwerk"])[0])  # FRITZ = Box-intern
            raise BackupError(f"{e}. Ordner auf dem Netzwerkspeicher: {', '.join(roots) or '–'} – trag z. B. "
                              f"„{pick}/Hauskern-Sicherung“ ein") from e
        return tls, len(_nas_list(f)), roots
    finally:
        try:
            f.quit()
        except Exception:  # noqa: BLE001
            f.close()
