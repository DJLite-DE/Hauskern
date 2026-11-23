# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
updater.py – Hauskern aktualisiert sich selbst (ohne Internet-Server).

* Am PC: neue Hauskern*.zip im Download-Ordner wird beim Start erkannt → auf Wunsch installiert
* vorher wird die laufende Version gesichert (Datenordner/updates) → jederzeit zurück
* Raspberry Pi: der PC schickt das Update selbst hinüber, abgesichert per HMAC mit einem
  geheimen Schlüssel, den nur PC und Pi kennen (kommt mit „Einstellungen an den Pi senden“)

Es werden nur Programmdateien ersetzt – Einstellungen, Verlauf, Logbuch & ffmpeg liegen im Datenordner.

Ab 8.12: Updates aus Downloads/„Datei wählen“ müssen eine gültige digitale Unterschrift (SIGNATUR.json,
siehe signing.py) tragen und zur installierten Variante (Familie/Consumer) passen. Ausgenommen sind nur die
eigenen Sicherungen und Updates vom eigenen PC an den Heimserver (dort schützt der gemeinsame Schlüssel).
"""
import hashlib
import hmac
import io
import os
import py_compile
import re
import shutil
import tempfile
import time
import zipfile

import signing

PROGRAM_EXT = (".py", ".pyw", ".md", ".sh", ".svg", ".png", ".ico", ".txt", ".json", ".cmd", ".ps1")
SKIP_DIRS = {"__pycache__", "ffmpeg", ".git"}
SKIP_FILES = {"Hauskern-Umzug.zip"}
ONLY_IF_MISSING = {"meldeziel.json"}   # Meldeziel der Familie: kommt per Familien-Paket, wird aber nie überschrieben
MAX_SIZE = 60_000_000


class UpdateError(Exception):
    pass


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:4]) or (0,)


def _locate(z):
    """Findet Hauskern.pyw im Archiv – mit oder ohne Oberordner. Gibt (prefix, version) zurück."""
    cands = [n for n in z.namelist() if n.replace("\\", "/").split("/")[-1] == "Hauskern.pyw"
             and n.replace("\\", "/").count("/") <= 1]
    if not cands:
        raise UpdateError("Das ist kein Hauskern-Paket (Hauskern.pyw fehlt)")
    name = sorted(cands, key=len)[0]
    prefix = name[: -len("Hauskern.pyw")]
    m = re.search(rb'^VERSION\s*=\s*"([^"]+)"', z.read(name), re.M)
    if not m:
        raise UpdateError("Versionsnummer im Paket nicht gefunden")
    return prefix, m.group(1).decode()


def read_version(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return _locate(z)[1]
    except zipfile.BadZipFile:
        raise UpdateError("Datei ist kein gültiges ZIP")


def installed_edition(app_dir):
    """Variante der Installation laut edition.py – ältere Installationen (vor 8.12) sind Familien-Versionen."""
    try:
        with open(os.path.join(app_dir, "edition.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^EDITION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "familie"
    except OSError:
        return "familie"


def manifest_of(z, prefix):
    """Geprüftes Manifest aus dem Paket oder None (fehlt / Unterschrift falsch)."""
    try:
        raw = z.read(prefix + signing.MANIFEST)
    except KeyError:
        return None
    return signing.check_manifest(raw)


def package_edition(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            prefix, _ = _locate(z)
            man = manifest_of(z, prefix)
            return man.get("edition") if man else None
    except (zipfile.BadZipFile, UpdateError):
        return None


def find_download(current, folders=None, max_age_days=60, edition=None):
    """Neueste Hauskern-ZIP in Downloads/Desktop, die neuer ist als die laufende Version → (pfad, version) oder None.
    Mit edition werden Pakete der anderen Variante übersprungen."""
    home = os.path.expanduser("~")
    folders = folders or [os.path.join(home, "Downloads"), os.path.join(home, "Desktop")]
    best = None
    for d in folders:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not ("hauskern" in n.lower() and n.lower().endswith(".zip")) or "umzug" in n.lower() \
                    or "meldung" in n.lower():
                continue
            p = os.path.join(d, n)
            try:
                if time.time() - os.path.getmtime(p) > max_age_days * 86400 or os.path.getsize(p) > MAX_SIZE:
                    continue
                with open(p, "rb") as f:
                    raw = f.read()
                v = read_version(raw)
                if edition and package_edition(raw) not in (None, edition):
                    continue
            except (OSError, UpdateError):
                continue
            if vtuple(v) > vtuple(current) and (best is None or vtuple(v) > vtuple(best[1])):
                best = (p, v)
    return best


def _program_files(app_dir):
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or not f.lower().endswith(PROGRAM_EXT):
                continue
            full = os.path.join(root, f)
            yield full, os.path.relpath(full, app_dir).replace("\\", "/")


def build_package(app_dir):
    """Die laufende Version als Update-Paket (für den Pi)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in _program_files(app_dir):
            z.write(full, "Hauskern/" + rel)
    return buf.getvalue()


def build_signed_copy(app_dir, extra=()):
    """Die laufende Installation als Paket MIT der offiziellen Signatur (z. B. Familien-Paket).
    Klappt nur, wenn alle Programmdateien unverändert sind. extra: zusätzliche Dateien (nicht signiert,
    werden beim Update ignoriert, beim ersten Entpacken aber mitgenommen – z. B. meldeziel.json)."""
    try:
        with open(os.path.join(app_dir, signing.MANIFEST), "rb") as f:
            raw = f.read()
    except OSError:
        raise UpdateError("Diese Installation hat keine Signatur (älter als 8.12)")
    man = signing.check_manifest(raw)
    if not man:
        raise UpdateError("Signatur der Installation ungültig")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, h in man["files"].items():
            try:
                with open(os.path.join(app_dir, *rel.split("/")), "rb") as f:
                    data = f.read()
            except OSError:
                raise UpdateError(f"Datei fehlt: {rel}")
            if signing.sha256(data) != h:
                raise UpdateError(f"Datei wurde verändert: {rel}")
            z.writestr("Hauskern/" + rel, data)
        z.writestr("Hauskern/" + signing.MANIFEST, raw)
        for rel in extra:
            fp = os.path.join(app_dir, rel)
            if os.path.isfile(fp):
                z.write(fp, "Hauskern/" + rel)
    return buf.getvalue()


def backup(app_dir, backup_dir, version):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"Hauskern-{version}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
    with open(path, "wb") as f:
        f.write(build_package(app_dir))
    # nur die letzten 5 Sicherungen behalten
    olds = sorted((os.path.join(backup_dir, n) for n in os.listdir(backup_dir) if n.endswith(".zip")),
                  key=os.path.getmtime)
    for p in olds[:-5]:
        try:
            os.remove(p)
        except OSError:
            pass
    return path


def list_backups(backup_dir):
    out = []
    try:
        for n in os.listdir(backup_dir):
            if n.startswith("Hauskern-") and n.endswith(".zip"):
                p = os.path.join(backup_dir, n)
                out.append((p, n[len("Hauskern-"):].split("-")[0], os.path.getmtime(p)))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x[2])


def apply(data, app_dir, backup_dir, current, allow_older=False, require_sig=True):
    """Prüft das Paket (Signatur, Variante, Prüfsummen), sichert die laufende Version und spielt die neue ein.
    Gibt (neue Version, Sicherung, Anzahl Dateien) zurück."""
    if len(data) > MAX_SIZE:
        raise UpdateError("Paket zu groß")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("Datei ist kein gültiges ZIP")
    with z:
        prefix, version = _locate(z)
        if not allow_older and vtuple(version) <= vtuple(current):
            raise UpdateError(f"Version {version} ist nicht neuer als die installierte {current}")
        man = None
        if require_sig:
            if prefix + signing.MANIFEST not in z.namelist():
                raise UpdateError("Dieses Paket ist nicht digital unterschrieben – es stammt nicht aus der offiziellen "
                                  "Quelle. Zur Sicherheit wird es nicht installiert.")
            man = manifest_of(z, prefix)
            if not man:
                raise UpdateError("Die digitale Unterschrift des Pakets stimmt nicht – es wurde verändert oder ist "
                                  "gefälscht. Zur Sicherheit wird es nicht installiert.")
            if man.get("version") != version:
                raise UpdateError("Versionsnummer passt nicht zur Unterschrift – Paket nicht installiert")
            mine, theirs = installed_edition(app_dir), man.get("edition")
            if theirs != mine:
                names = {"familie": "Familien-Version", "consumer": "öffentliche Version"}
                raise UpdateError(f"Das ist die {names.get(theirs, theirs)}, installiert ist die "
                                  f"{names.get(mine, mine)}. Bitte das passende Paket nehmen.")
        tmp = tempfile.mkdtemp(prefix="hauskern-update-")
        try:
            count = 0
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if not name.startswith(prefix) or name.endswith("/"):
                    continue
                rel = name[len(prefix):]
                parts = rel.split("/")
                if not rel or rel.startswith("/") or ".." in parts or ":" in rel or set(parts) & SKIP_DIRS \
                        or parts[-1] in SKIP_FILES or not rel.lower().endswith(PROGRAM_EXT):
                    continue
                if rel in ONLY_IF_MISSING:
                    if os.path.exists(os.path.join(app_dir, rel)) or installed_edition(app_dir) != "familie":
                        continue
                if man is not None and rel != signing.MANIFEST and rel not in man["files"] and rel not in ONLY_IF_MISSING:
                    continue        # nicht unterschrieben → wird nicht installiert
                dst = os.path.join(tmp, *parts)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
            if man is not None:   # jede unterschriebene Datei muss da sein und genau stimmen
                for rel, h in man["files"].items():
                    fp = os.path.join(tmp, *rel.split("/"))
                    try:
                        with open(fp, "rb") as f:
                            ok = signing.sha256(f.read()) == h
                    except OSError:
                        raise UpdateError(f"Paket unvollständig ({rel} fehlt) – nicht installiert")
                    if not ok:
                        raise UpdateError(f"Datei {rel} wurde verändert – Paket nicht installiert")
            # alles muss sich übersetzen lassen – sonst lieber gar nichts ersetzen
            for root, _, files in os.walk(tmp):
                for f in files:
                    if f.endswith((".py", ".pyw")):
                        try:
                            py_compile.compile(os.path.join(root, f), doraise=True,
                                               cfile=os.path.join(tempfile.gettempdir(), "hk_check.pyc"))
                        except py_compile.PyCompileError as e:
                            raise UpdateError(f"Paket fehlerhaft ({f}): {str(e.exc_value)[:120]}")
            if not os.access(app_dir, os.W_OK):
                raise UpdateError(f"Keine Schreibrechte im Programmordner {app_dir}")
            saved = backup(app_dir, backup_dir, current)
            for root, _, files in os.walk(tmp):
                for f in files:
                    src = os.path.join(root, f)
                    dst = os.path.join(app_dir, os.path.relpath(src, tmp))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst + ".neu")
                    os.replace(dst + ".neu", dst)
            shutil.rmtree(os.path.join(app_dir, "__pycache__"), ignore_errors=True)
            return version, saved, count
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def sign(token, data):
    return hmac.new(token.encode(), data, hashlib.sha256).hexdigest()


def verify(token, data, sig):
    return bool(token) and bool(sig) and hmac.compare_digest(sign(token, data), sig)
