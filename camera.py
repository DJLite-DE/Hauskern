# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
camera.py – Livebild von Bambu-Lab-Druckern mit RTSPS-Kamera (X2D, H2D, X1 …).

Der Videostrom (H.264) wird mit ffmpeg in einzelne PNG-Bilder umgewandelt, die Tk direkt anzeigen kann.
ffmpeg wird gesucht: im App-Ordner (ffmpeg.exe oder ffmpeg\\bin\\ffmpeg.exe), dann im PATH.
"""
import hashlib
import os
import shutil
import urllib.error
import urllib.request
import zipfile
import subprocess
import threading
import time
import urllib.parse

PNG_END = b"IEND\xaeB`\x82"


def input_args(url):
    """Eingabe-Optionen für ffmpeg. rtsps (Bambu-Drucker) nutzt ein selbstsigniertes Zertifikat."""
    args = ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    if url.startswith("rtsps://"):
        args += ["-tls_verify", "0"]
    return args + ["-i", url]
HERE = os.path.dirname(os.path.abspath(__file__))


def _appdata_ff(app="Hauskern"):
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, app, "ffmpeg", "ffmpeg.exe")


def find_ffmpeg():
    # zuerst im Datenordner (überlebt Updates, bei denen der App-Ordner ersetzt wird)
    for app in ("Hauskern", "Heimora", "Heimzentrale", "FritzMonitor"):
        if os.name == "nt" and os.path.exists(_appdata_ff(app)):
            return _appdata_ff(app)
    for p in (os.path.join(HERE, "ffmpeg.exe"), os.path.join(HERE, "ffmpeg", "bin", "ffmpeg.exe"),
              os.path.join(HERE, "ffmpeg", "ffmpeg.exe")):
        if os.path.exists(p):
            return p
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget-Installation (Gyan.FFmpeg) liegt oft nicht sofort im PATH
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return None


# --------------------------------------------------------------------------- #
# ffmpeg automatisch einrichten (offizielle Builds von gyan.dev, mit SHA-256-Prüfung)
# --------------------------------------------------------------------------- #
FF_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FF_SHA_URL = FF_URL + ".sha256"
FF_VER_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
FF_MIRROR = "https://github.com/GyanD/codexffmpeg/releases/download/{v}/ffmpeg-{v}-essentials_build.zip"
FF_TARGET = _appdata_ff() if os.name == "nt" else os.path.join(HERE, "ffmpeg", "ffmpeg.exe")  # Datenordner: bleibt bei Updates erhalten


class SetupError(Exception):
    pass


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Hauskern"})
    return urllib.request.urlopen(req, timeout=timeout)


def install_ffmpeg(progress=lambda done, total: None, cancel=lambda: False):
    """Lädt ffmpeg herunter, prüft die Prüfsumme und entpackt nur ffmpeg.exe. Gibt den Pfad zurück."""
    os.makedirs(os.path.dirname(FF_TARGET), exist_ok=True)
    tmp = FF_TARGET + ".download.zip"
    expected = None
    try:
        with _get(FF_SHA_URL) as r:
            expected = r.read().decode(errors="ignore").split()[0].strip().lower()
    except Exception:
        pass
    urls = [FF_URL]
    try:
        with _get(FF_VER_URL) as r:
            v = r.read().decode(errors="ignore").strip()
        if v:
            urls.append(FF_MIRROR.format(v=v))
    except Exception:
        pass
    last_err = None
    for url in urls:
        try:
            h = hashlib.sha256()
            with _get(url, timeout=30) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if cancel():
                        raise SetupError("abgebrochen")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    progress(done, total)
            if expected and h.hexdigest() != expected:
                raise SetupError("Prüfsumme stimmt nicht – Download beschädigt")
            with zipfile.ZipFile(tmp) as z:
                name = next((n for n in z.namelist() if n.lower().endswith("/bin/ffmpeg.exe")), None)
                if not name:
                    raise SetupError("ffmpeg.exe nicht im Archiv gefunden")
                with z.open(name) as src, open(FF_TARGET, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            return FF_TARGET
        except SetupError as e:
            last_err = e
            if str(e) == "abgebrochen":
                break
        except (OSError, zipfile.BadZipFile, urllib.error.URLError) as e:
            last_err = e
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    raise SetupError(f"Download fehlgeschlagen ({last_err})")


def check_ffmpeg(exe):
    """Startet ffmpeg testweise. Gibt (ok, meldung) zurück – erkennt auch eine Sperre durch Smart App Control."""
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, timeout=15, creationflags=flags)
    except OSError as e:
        if getattr(e, "winerror", None) in (4551, 4553, 1260, 225):
            return False, "Windows (Smart App Control) blockiert ffmpeg."
        return False, f"ffmpeg startet nicht ({e})"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg reagiert nicht"
    if r.returncode != 0 or b"ffmpeg version" not in r.stdout:
        return False, "ffmpeg ist beschädigt – bitte neu einrichten"
    return True, r.stdout.split(b"\n")[0].decode(errors="ignore")


def bambu_url(ip, code):
    return f"rtsps://bblp:{urllib.parse.quote(code, safe='')}@{ip}:322/streaming/live/1"


class CameraStream:
    def __init__(self, url, fps=4, width=960):
        self.url = url
        self.fps, self.width = fps, width
        self.frame = None        # letztes PNG (bytes)
        self.frame_no = 0
        self.error = ""
        self.running = False
        self._proc = None

    def start(self):
        exe = find_ffmpeg()
        if not exe:
            self.error = "ffmpeg nicht gefunden"
            return False
        cmd = [exe, "-hide_banner", "-loglevel", "error"] + input_args(self.url) + [
               "-an", "-vf", f"fps={self.fps},scale={self.width}:-2",
               "-f", "image2pipe", "-vcodec", "png", "-"]
        flags = 0x08000000 if os.name == "nt" else 0  # kein Konsolenfenster
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            if getattr(e, "winerror", None) in (4551, 4553, 1260, 225):
                self.error = "Windows (Smart App Control) blockiert ffmpeg."
            else:
                self.error = f"ffmpeg konnte nicht gestartet werden ({e})"
            return False
        self.running = True
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        return True

    def _read(self):
        buf = b""
        out = self._proc.stdout
        while self.running:
            chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                i = buf.find(PNG_END)
                if i < 0:
                    break
                self.frame = buf[:i + len(PNG_END)]
                self.frame_no += 1
                buf = buf[i + len(PNG_END):]
            if len(buf) > 20_000_000:
                buf = b""
        self.running = False
        if not self.error:
            self.error = "Stream beendet"

    def _read_err(self):
        for line in self._proc.stderr:
            text = line.decode(errors="ignore").strip()
            if text:
                low = text.lower()
                if "401" in low or "unauthorized" in low:
                    self.error = "Zugangsdaten abgelehnt (Zugangscode / Passwort prüfen)"
                elif "i/o error" in low or "input/output error" in low or "end of file" in low:
                    self.error = ("Verbindung wird abgelehnt – meist ist schon Bambu Studio/Handy verbunden "
                                  "oder die LAN-Live-Ansicht ist aus" if "rtsps://" in self.url else
                                  "Kamera bricht die Verbindung ab – evtl. zu viele gleichzeitige Zugriffe")
                elif "tls" in low or "ssl" in low or "handshake" in low:
                    self.error = "Verschlüsselte Verbindung fehlgeschlagen (TLS)"
                elif "refused" in low or "timed out" in low or "timeout" in low:
                    self.error = "Kamera nicht erreichbar – „LAN Only Liveview“ am Drucker aktiv?"
                else:
                    self.error = text[:120]

    def stop(self):
        self.running = False
        if self._proc:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None


def grab_frame(url, timeout=25, width=1280):
    """Holt ein einzelnes JPEG-Bild aus einem Stream (für Push-Nachrichten). Gibt bytes oder None zurück."""
    exe = find_ffmpeg()
    if not exe:
        return None
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run([exe, "-hide_banner", "-loglevel", "error"] + input_args(url) +
                           ["-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg",
                            "-q:v", "5", "-"], capture_output=True, timeout=timeout, creationflags=flags)
        return r.stdout if r.stdout[:2] == b"\xff\xd8" else None
    except (subprocess.TimeoutExpired, OSError):
        return None


class Recorder:
    """Nimmt den Kamerastream ohne Neukodierung als MP4 auf (ffmpeg -c copy)."""
    ACTIVE = set()  # Pfade laufender Aufnahmen – die werden nicht angezeigt und nicht aufgeräumt

    def __init__(self, url, folder, name):
        self.url, self.folder, self.name = url, folder, name
        self.proc = None
        self.path = None
        self.started = 0

    def start(self):
        exe = find_ffmpeg()
        if not exe:
            raise SetupError("ffmpeg fehlt – bitte zuerst „Kamera einrichten“")
        os.makedirs(self.folder, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in self.name).strip() or "Kamera"
        self.path = os.path.join(self.folder, f"{safe} {time.strftime('%Y-%m-%d %H-%M-%S')}.mp4")
        flags = 0x08000000 if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [exe, "-hide_banner", "-loglevel", "error"] + input_args(self.url) + [
             "-c:v", "copy", "-an", "-movflags", "+faststart", "-y", self.path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        self.started = time.time()
        Recorder.ACTIVE.add(self.path)
        return self.path

    @property
    def active(self):
        return bool(self.proc) and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.communicate(b"q", timeout=5)  # sauber beenden, damit die MP4 abspielbar ist
            except Exception:
                self.proc.kill()
        self.proc = None
        Recorder.ACTIVE.discard(self.path)
        return self.path


if __name__ == "__main__":  # Schnelltest: python camera.py RTSP-URL
    import sys
    s = CameraStream(sys.argv[1])
    print("ffmpeg:", find_ffmpeg(), "start:", s.start())
    time.sleep(8)
    print("Bilder:", s.frame_no, "Fehler:", s.error)
    s.stop()
