# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
motion.py – Bewegungserkennung für IP-Kameras.

Zwei Wege, automatisch gewählt:
  1. „Kamera“: die Kamera erkennt selbst und meldet es per ONVIF-Ereignis (spart Rechenleistung, z. B. Tapo).
     Die Empfindlichkeit stellst du dann in der Hersteller-App ein (Tapo-App → Kamera → Erkennung).
  2. „Bildvergleich“: Hauskern vergleicht selbst kleine Graustufenbilder (2 pro Sekunde, 64×36 Pixel) aus dem
     kleinen Stream – klappt mit jeder Kamera, braucht ffmpeg.

MotionWatcher(cam_id, name, get_onvif, get_url, mode, sensitivity, on_change).start()
  on_change(cam_id, aktiv: bool, quelle: str) – wird aus einem Hintergrund-Thread aufgerufen.
"""
import os
import subprocess
import threading
import time

from camera import find_ffmpeg, input_args
from onvif import OnvifError, motion_value

W, H = 64, 36
HOLD_S = 15          # so lange gilt „Bewegung“ nach dem letzten Treffer
SENS = {"niedrig": 0.08, "mittel": 0.04, "hoch": 0.02}  # Anteil geänderter Bildpunkte


class FrameDiff:
    """Erkennt Bewegung in einer Folge kleiner Graustufenbilder (bytes, W*H)."""

    def __init__(self, sensitivity="mittel", pixel_delta=25):
        self.min_share = SENS.get(sensitivity, 0.04)
        self.pixel_delta = pixel_delta
        self.bg = None
        self.hits = 0

    def feed(self, frame):
        """Gibt True zurück, wenn in diesem Bild Bewegung ist (mind. 2 Bilder hintereinander)."""
        if self.bg is None:
            self.bg = list(frame)
            return False
        bg, d = self.bg, self.pixel_delta
        changed = 0
        for i, v in enumerate(frame):
            b = bg[i]
            if v - b > d or b - v > d:
                changed += 1
            bg[i] = (b * 7 + v) >> 3  # Hintergrund langsam nachführen (Licht, Schatten)
        share = changed / len(frame)
        if share > 0.6:  # fast alles anders: Nachtsicht/Licht an – keine Bewegung, Hintergrund neu
            self.bg, self.hits = list(frame), 0
            return False
        self.hits = self.hits + 1 if share >= self.min_share else 0
        return self.hits >= 2


class MotionWatcher:
    def __init__(self, cam_id, name, get_onvif, get_url, mode="auto", sensitivity="mittel", on_change=None, log=None):
        self.cam_id, self.name = cam_id, name
        self.get_onvif, self.get_url = get_onvif, get_url
        self.mode, self.sensitivity = mode, sensitivity
        self.on_change, self.log = on_change, log or (lambda *a: None)
        self.active = False
        self.source = ""       # "Kamera" oder "Bildvergleich"
        self.error = ""
        self.last_motion = 0.0
        self._stop = threading.Event()
        self._proc = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"Bewegung {self.name}").start()
        return self

    def stop(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def _set(self, on):
        now = time.time()
        if on:
            self.last_motion = now
        if on != self.active:
            if not on and now - self.last_motion < HOLD_S:
                return
            self.active = on
            if self.on_change:
                self.on_change(self.cam_id, on, self.source)

    def _run(self):
        backoff = 5
        while not self._stop.is_set():
            used_camera = False
            if self.mode in ("auto", "camera"):
                cam = self.get_onvif()
                if cam is not None and cam.has_events:
                    used_camera = True
                    try:
                        self._onvif_loop(cam)
                        backoff = 5
                    except OnvifError as e:
                        self.error = str(e)
                        self.log("Bewegung %s: Kamera-Ereignisse gehen nicht (%s)", self.name, e)
                        if self.mode == "auto":
                            self.mode = "image"  # dann eben selbst vergleichen
                            continue
                elif self.mode == "camera":
                    self.error = "Kamera meldet keine Ereignisse"
            if not used_camera and self.mode in ("auto", "image"):
                try:
                    self._frame_loop()
                    backoff = 5
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)
            if self.active:
                self.last_motion = 0
                self._set(False)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 120)

    def _onvif_loop(self, cam):
        pp = cam.pullpoint()
        self.source, self.error = "Kamera", ""
        try:
            while not self._stop.is_set():
                got = None
                for topic, items in pp.pull(10):
                    v = motion_value(topic, items)
                    if v is not None:
                        got = v
                if got is True:
                    self._set(True)
                elif got is False or (self.active and time.time() - self.last_motion >= HOLD_S):
                    self._set(False)
        finally:
            pp.close()

    def _frame_loop(self):
        exe = find_ffmpeg()
        if not exe:
            raise RuntimeError("ffmpeg fehlt")
        url = self.get_url()
        if not url:
            raise RuntimeError("keine Stream-Adresse")
        flags = 0x08000000 if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            [exe, "-hide_banner", "-loglevel", "error"] + input_args(url) +
            ["-an", "-vf", f"fps=2,scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
        self.source, self.error = "Bildvergleich", ""
        fd = FrameDiff(self.sensitivity)
        size = W * H
        try:
            while not self._stop.is_set():
                buf = self._proc.stdout.read(size)
                if len(buf) < size:
                    raise RuntimeError("Stream beendet")
                hit = fd.feed(buf)
                if hit:
                    self._set(True)
                elif self.active and time.time() - self.last_motion >= HOLD_S:
                    self._set(False)
        finally:
            if self._proc.poll() is None:
                self._proc.kill()
