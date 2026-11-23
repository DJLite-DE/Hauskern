# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
timelapse.py – Zeitraffer von 3D-Drucken.

Standardmäßig AUS. Pro Drucker: „aus“, „nur wenn ich ihn einschalte“ (für einen Druck) oder „jeden Druck“.
Während des Drucks: ein Bild pro Schicht (oder alle X Sekunden, wenn der Drucker keine Schichten meldet).
Am Ende: ffmpeg macht daraus ein MP4, die Einzelbilder werden gelöscht. Ein Speicherlimit und ein
Höchstalter räumen alte Videos automatisch weg.
"""
import os
import re
import shutil
import subprocess
import threading
import time

MODES = [("off", "aus"), ("ask", "nur wenn ich ihn einschalte"), ("always", "jeden Druck")]
DEFAULTS = {"interval": 20, "max_gb": 5.0, "keep_days": 60, "fps": 30, "width": 1280}
MIN_GAP = 4          # nie öfter als alle 4 Sekunden ein Bild
MAX_FRAMES = 2400    # danach nur noch jedes zweite Mal (≈ 80 s Video bei 30 fps)
MIN_FRAMES = 12      # weniger Bilder → kein Video (z. B. Druck sofort abgebrochen)
RUNNING = ("RUNNING",)
ACTIVE = ("RUNNING", "PAUSE", "PREPARE", "SLICING")


def safe(name):
    n = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name or "").strip()
    n = re.sub(r"\.(gcode|3mf)$", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n)[:70] or "Druck"


class Session:
    def __init__(self, serial, name, file, folder):
        self.serial, self.name, self.file, self.dir = serial, name, file, folder
        self.started = time.time()
        self.frames = 0
        self.last_shot = 0.0
        self.last_layer = None
        self.busy = False
        self.skip = 0
        os.makedirs(folder, exist_ok=True)


class Manager:
    """grab(source) → JPEG-bytes oder None. done(ergebnis) bekommt am Ende ein dict (aus einem Thread)."""

    def __init__(self, work_dir, out_dir, grab, ffmpeg, done=None, log=None):
        self.work_dir, self.out_dir = work_dir, out_dir
        self.grab, self.ffmpeg, self.done = grab, ffmpeg, done
        self.log = log or (lambda *a: None)
        self.sessions = {}
        self.lock = threading.Lock()

    # ---------------- während des Drucks ----------------
    def update(self, serial, name, summ, source, active, settings, now=None):
        """Bei jeder Druckermeldung aufrufen. active = soll für diesen Druck aufgenommen werden?"""
        now = now or time.time()
        st = summ.get("state") or ""
        s = self.sessions.get(serial)
        if s is None:
            if active and st in RUNNING and summ.get("connected", True):
                folder = os.path.join(self.work_dir, f"{serial}-{int(now)}")
                s = self.sessions[serial] = Session(serial, name, summ.get("file") or "", folder)
                self.log(f"Zeitraffer gestartet: {name}")
            else:
                return None
        if st in RUNNING:
            layer = summ.get("layer")
            due = False
            if isinstance(layer, int) and summ.get("layers"):
                due = layer != s.last_layer and layer > 0
            else:
                due = now - s.last_shot >= float(settings.get("interval", DEFAULTS["interval"]))
            if due and now - s.last_shot >= MIN_GAP and not s.busy:
                if s.frames >= MAX_FRAMES:
                    s.skip += 1
                    if s.skip % 2:
                        s.last_layer = layer
                        return s
                s.last_layer, s.last_shot = layer, now
                self._shoot(s, source, settings)
        elif st not in ACTIVE:  # fertig, fehlgeschlagen, abgebrochen, bereit
            self.finish(serial, st, settings)
        return s

    def _shoot(self, s, source, settings):
        s.busy = True

        def work():
            try:
                img = self.grab(source)
                if img:
                    s.frames += 1
                    with open(os.path.join(s.dir, f"bild_{s.frames:05d}.jpg"), "wb") as f:
                        f.write(img)
            except Exception as e:  # noqa: BLE001
                self.log(f"Zeitraffer-Bild fehlgeschlagen: {e}")
            finally:
                s.busy = False
        threading.Thread(target=work, daemon=True, name="Zeitraffer-Bild").start()

    def recording(self, serial):
        s = self.sessions.get(serial)
        return {"frames": s.frames, "since": s.started} if s else None

    # ---------------- am Ende ----------------
    def finish(self, serial, state, settings):
        s = self.sessions.pop(serial, None)
        if not s:
            return

        def work():
            for _ in range(60):  # letztes Bild noch abwarten
                if not s.busy:
                    break
                time.sleep(0.5)
            res = {"serial": serial, "name": s.name, "file": s.file, "state": state, "frames": s.frames}
            try:
                if s.frames < MIN_FRAMES:
                    res["skipped"] = f"nur {s.frames} Bilder – kein Video"
                else:
                    res.update(self.render(s, settings))
            except Exception as e:  # noqa: BLE001
                res["error"] = str(e)
            finally:
                shutil.rmtree(s.dir, ignore_errors=True)  # Einzelbilder weg – Speicher sparen
            try:
                res["removed"] = self.enforce_limits(settings)
            except OSError:
                pass
            if self.done:
                self.done(res)
        threading.Thread(target=work, daemon=True, name="Zeitraffer-Video").start()

    def render(self, s, settings):
        exe = self.ffmpeg()
        if not exe:
            raise RuntimeError("ffmpeg fehlt – wird für das Video gebraucht")
        os.makedirs(self.out_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H-%M", time.localtime(s.started))
        out = os.path.join(self.out_dir, f"{stamp} {safe(s.name)} – {safe(s.file)}.mp4")
        fps = int(settings.get("fps", DEFAULTS["fps"]))
        w = int(settings.get("width", DEFAULTS["width"]))
        tmp = out + ".part.mp4"
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
               "-i", os.path.join(s.dir, "bild_%05d.jpg"),
               "-vf", f"scale='min({w},iw)':-2,tpad=stop_mode=clone:stop_duration=1.5,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart", tmp]
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError("Video konnte nicht erstellt werden: " + r.stderr.decode(errors="replace")[-200:])
        os.replace(tmp, out)
        return {"video": out, "size": os.path.getsize(out), "seconds": round(s.frames / fps + 1.5, 1)}

    # ---------------- Speicher ----------------
    def videos(self):
        out = []
        try:
            names = os.listdir(self.out_dir)
        except OSError:
            return out
        for n in names:
            if n.endswith(".mp4") and not n.endswith(".part.mp4"):
                p = os.path.join(self.out_dir, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({"name": n, "path": p, "size": st.st_size, "ts": st.st_mtime})
        out.sort(key=lambda v: -v["ts"])
        return out

    def usage(self):
        return sum(v["size"] for v in self.videos())

    def enforce_limits(self, settings, now=None):
        """Älter als keep_days oder über max_gb → die ältesten Videos löschen."""
        now = now or time.time()
        vids = self.videos()
        keep_s = float(settings.get("keep_days", DEFAULTS["keep_days"]) or 0) * 86400
        max_b = float(settings.get("max_gb", DEFAULTS["max_gb"]) or 0) * 1e9
        removed, total = [], sum(v["size"] for v in vids)
        for i, v in enumerate(reversed(vids)):  # älteste zuerst
            newest = i == len(vids) - 1
            too_old = keep_s and now - v["ts"] > keep_s
            too_big = max_b and total > max_b and not newest  # das neueste Video bleibt immer
            if too_old or too_big:
                try:
                    os.remove(v["path"])
                    removed.append(v["name"])
                    total -= v["size"]
                except OSError:
                    pass
        return removed

    def delete(self, name):
        v = next((x for x in self.videos() if x["name"] == name), None)
        if not v:
            return False
        os.remove(v["path"])
        return True

    def cleanup_orphans(self):
        """Nach einem Absturz liegengebliebene Einzelbilder löschen."""
        try:
            for n in os.listdir(self.work_dir):
                if n.split("-")[0] not in self.sessions:
                    shutil.rmtree(os.path.join(self.work_dir, n), ignore_errors=True)
        except OSError:
            pass


def fmt_size(n):
    return f"{n / 1e9:.1f} GB".replace(".", ",") if n >= 1e9 else (f"{round(n / 1e6)} MB" if n >= 1e6 else ("0 MB" if n <= 0 else "< 1 MB"))
