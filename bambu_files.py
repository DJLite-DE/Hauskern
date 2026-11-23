# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
bambu_files.py – Liest den geplanten Filamentverbrauch eines Drucks direkt vom Bambu-Drucker.

Der Drucker stellt seine Dateien per FTPS bereit (Port 990, implizites TLS, Benutzer „bblp“,
Passwort = LAN-Zugangscode). In der .3mf steckt Metadata/slice_info.config mit Gramm pro Filament.

job_usage(ip, code, gcode_file, subtask) -> {"file", "plate", "grams", "seconds", "filaments": [{type, color, grams}]}
parse_slice_info(xml_text, plate=None)    -> dasselbe ohne Download (auch für .3mf vom PC)
"""
import ftplib
import io
import posixpath
import re
import socket
import ssl
import xml.etree.ElementTree as ET
import zipfile


class FilesError(Exception):
    pass


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTPS mit implizitem TLS (Port 990) – die Datenverbindung nutzt die TLS-Sitzung erneut (verlangt der Drucker)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def _context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # selbstsigniertes Zertifikat des Druckers, nur im Heimnetz
    return ctx


def parse_slice_info(text, plate=None):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FilesError(f"slice_info unlesbar ({e})")
    plates = root.findall("plate")
    if not plates:
        raise FilesError("keine Platte in der Datei")

    def meta(p, key):
        el = next((m for m in p.findall("metadata") if m.get("key") == key), None)
        return el.get("value") if el is not None else None

    if plate == "all":
        use = plates
    else:
        use = [next((x for x in plates if plate and meta(x, "index") == str(plate)), None) or plates[0]]
    acc, secs = {}, 0
    for p in use:
        for f in p.findall("filament"):
            try:
                g = float(f.get("used_g") or 0)
            except ValueError:
                g = 0.0
            if g > 0:
                k = (f.get("type", ""), (f.get("color") or "")[:7])
                acc[k] = acc.get(k, 0.0) + g
        try:
            secs += int(float(meta(p, "prediction") or 0))
        except ValueError:
            pass
    fil = [{"type": t, "color": c, "grams": round(g, 2)} for (t, c), g in acc.items()]
    return {"plate": "alle" if plate == "all" else meta(use[0], "index"), "plates": len(plates),
            "grams": round(sum(f["grams"] for f in fil), 2), "seconds": secs, "filaments": fil}


def parse_3mf(data, plate=None):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        txt = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise FilesError(f"keine geslicte .3mf ({e})")
    return parse_slice_info(txt, plate)


def plate_of(gcode_file):
    m = re.search(r"plate_(\d+)\.gcode", gcode_file or "")
    return m.group(1) if m else None


def _candidates(gcode_file, subtask):
    names = []
    gf = (gcode_file or "").strip()
    if gf and gf.lower().endswith(".3mf"):
        names.append(posixpath.basename(gf))
    for n in (subtask or "",):
        if n:
            names += [n if n.lower().endswith(".3mf") else n + ".gcode.3mf", n + ".3mf"]
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def job_usage(ip, code, gcode_file="", subtask="", timeout=15):
    names = _candidates(gcode_file, subtask)
    if not names:
        raise FilesError("Dateiname des Drucks unbekannt")
    ftp = ImplicitFTP_TLS(context=_context(), timeout=timeout)
    try:
        ftp.connect(ip, 990, timeout=timeout)
        ftp.login("bblp", code)
        ftp.prot_p()
        found = None
        for d in ("/", "/cache", "/model", "/sdcard", "/sdcard/cache"):
            lines = []
            try:
                ftp.retrlines(f"NLST {d}", lines.append)
            except ssl.SSLError:
                pass  # Datenkanal unsauber geschlossen – die Liste ist trotzdem vollständig
            except ftplib.all_errors:
                continue
            listing = [posixpath.basename(x) for x in lines]
            low = {x.lower(): x for x in listing}
            for n in names:
                if n.lower() in low:
                    found = posixpath.join(d, low[n.lower()])
                    break
            if found:
                break
        if not found:
            raise FilesError(f"Datei „{names[0]}“ nicht auf dem Drucker gefunden (aus der Cloud gestartet?)")
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {found}", buf.write)
        except ssl.SSLError:
            if not buf.tell():
                raise
    except ftplib.error_perm as e:
        raise FilesError(f"Drucker lehnt ab: {e} – Zugangscode prüfen")
    except (OSError, socket.timeout, ssl.SSLError, EOFError) as e:
        raise FilesError(f"Drucker-Dateien nicht erreichbar ({e})")
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass
    res = parse_3mf(buf.getvalue(), plate_of(gcode_file))
    res["file"] = found
    return res
