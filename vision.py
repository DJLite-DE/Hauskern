# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
vision.py – Kamera-Erkennung: Was ist auf dem Bild? (Person, Katze, Hund, Fahrzeug, Vogel)

Läuft komplett lokal mit OpenCV (cv2.dnn) und dem frei verfügbaren YOLOX-Modell aus dem OpenCV-Modellzoo
(Apache-2.0). Das Modell (≈ 36 MB) wird erst geladen, wenn man die Funktion einschaltet, und vor der Benutzung
gegen die offizielle SHA-256-Prüfsumme geprüft.

Braucht: python3-opencv + python3-numpy (auf dem Heimserver richtet install.sh das ein). Fehlt das, ist die
Funktion einfach nicht verfügbar – Hauskern läuft normal weiter.
"""
import hashlib
import os
import threading
import urllib.request

MODEL_FILE = "object_detection_yolox_2022nov.onnx"
MODEL_URL = ("https://huggingface.co/opencv/opencv_zoo/resolve/main/models/object_detection_yolox/"
             "object_detection_yolox_2022nov.onnx")
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_MB = 36
SIZE = 640
STRIDES = (8, 16, 32)

COCO = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
        "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
        "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
        "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
        "teddy bear", "hair drier", "toothbrush")
# Was Hauskern meldet: COCO-Klasse → (Schlüssel, deutscher Name)
WANT = {"person": ("person", "Person"), "cat": ("cat", "Katze"), "dog": ("dog", "Hund"), "bird": ("bird", "Vogel"),
        "car": ("vehicle", "Fahrzeug"), "truck": ("vehicle", "Fahrzeug"), "bus": ("vehicle", "Fahrzeug"),
        "motorcycle": ("vehicle", "Fahrzeug"), "bicycle": ("bicycle", "Fahrrad")}
LABELS = {"person": "Person", "cat": "Katze", "dog": "Hund", "animal": "Tier (Katze, Hund, Vogel)",
          "vehicle": "Fahrzeug", "bicycle": "Fahrrad", "bird": "Vogel"}
ANIMALS = {"cat", "dog", "bird"}
SENS = {"niedrig": 0.65, "mittel": 0.5, "hoch": 0.38}  # Mindest-Sicherheit


def available():
    """→ (True, "") oder (False, Grund)."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False, ("OpenCV fehlt – läuft auf dem Heimserver (install.sh richtet es ein). Unter Windows: "
                       "„pip install opencv-python-headless“.")
    try:
        import cv2
        cv2.dnn.readNet  # noqa: B018
    except AttributeError:
        return False, "Diese OpenCV-Version kann keine KI-Modelle laden."
    return True, ""


def model_path(folder):
    return os.path.join(folder, MODEL_FILE)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_ok(folder):
    p = model_path(folder)
    return os.path.isfile(p) and os.path.getsize(p) > 1_000_000


def download(folder, progress=None, url=MODEL_URL, expect=MODEL_SHA256, timeout=60):
    """Modell laden und prüfen. progress(prozent). Wirft RuntimeError mit verständlicher Meldung."""
    os.makedirs(folder, exist_ok=True)
    tmp = model_path(folder) + ".laden"
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hauskern"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:  # noqa: S310 – feste https-Adresse
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -1
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and total:
                    pct = int(done * 100 / total)
                    if pct != last:
                        last = pct
                        progress(pct)
    except OSError as e:
        _rm(tmp)
        raise RuntimeError(f"Download fehlgeschlagen: {e}") from None
    if expect and h.hexdigest() != expect:
        _rm(tmp)
        raise RuntimeError("Prüfsumme stimmt nicht – Datei verworfen (beschädigt oder verändert).")
    os.replace(tmp, model_path(folder))
    return model_path(folder)


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Vor- und Nachbearbeitung (wie im OpenCV-Modellzoo, YOLOX 2022nov)
# --------------------------------------------------------------------------- #
def letterbox(img, size=SIZE):
    """RGB-Bild → 640×640 mit grauem Rand (114), Seitenverhältnis bleibt. → (bild_float32, faktor)"""
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(h * r), int(w * r)
    out = np.full((size, size, 3), 114.0, dtype=np.float32)
    out[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    return out, r


_GRID = {}


def grids(size=SIZE):
    import numpy as np
    if size not in _GRID:
        g, s = [], []
        for stride in STRIDES:
            n = size // stride
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            g.append(grid)
            s.append(np.full((*grid.shape[:2], 1), stride))
        _GRID[size] = (np.concatenate(g, 1), np.concatenate(s, 1))
    return _GRID[size]


def decode(out, ratio, conf=0.5, nms=0.5, size=SIZE):
    """Modellausgabe (1, 8400, 85) → [(coco_klasse, sicherheit, (x1, y1, x2, y2) im Originalbild)]"""
    import cv2
    import numpy as np
    dets = np.array(out, dtype=np.float32).reshape(-1, 85).copy()
    g, s = grids(size)
    dets[:, :2] = (dets[:, :2] + g[0]) * s[0]
    dets[:, 2:4] = np.exp(dets[:, 2:4]) * s[0]
    scores = dets[:, 4:5] * dets[:, 5:]
    cls = scores.argmax(1)
    best = scores[np.arange(len(scores)), cls]
    keep = best >= conf
    if not keep.any():
        return []
    b, best, cls = dets[keep, :4], best[keep], cls[keep]
    xywh = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 2], b[:, 3]], 1)  # NMS will x,y,b,h
    idx = cv2.dnn.NMSBoxesBatched(xywh.tolist(), best.tolist(), cls.tolist(), conf, nms)
    res = []
    for i in np.array(idx).reshape(-1):
        x, y, w, h = (xywh[i] / ratio).tolist()
        res.append((COCO[int(cls[i])], float(best[i]), (x, y, x + w, y + h)))
    res.sort(key=lambda d: -d[1])
    return res


def summarize(dets, min_area=0.0, img_area=None):
    """Erkannte Objekte → {schlüssel: beste_sicherheit} für das, was Hauskern interessiert."""
    out = {}
    for name, score, (x1, y1, x2, y2) in dets:
        hit = WANT.get(name)
        if not hit:
            continue
        if img_area and min_area and (x2 - x1) * (y2 - y1) < min_area * img_area:
            continue  # winzig (weit weg/Fehlalarm)
        key = hit[0]
        out[key] = max(out.get(key, 0.0), score)
    return out


def describe(found):
    """{"person": .9, "cat": .7} → „Person, Katze“"""
    order = ("person", "cat", "dog", "bird", "vehicle", "bicycle")
    return ", ".join(LABELS.get(k, k).split(" (")[0] for k in order if k in found)


def matches(what, found):
    if what in (None, "", "any"):
        return bool(found)
    if what == "animal":
        return any(k in found for k in ANIMALS)
    return what in found


class Detector:
    """Lädt das Modell einmal und erkennt Objekte auf JPEG-Bildern. Threadsicher (eine Erkennung gleichzeitig)."""

    def __init__(self, folder):
        self.folder = folder
        self.net = None
        self.lock = threading.Lock()
        self.error = ""

    def ready(self):
        return self.net is not None

    def load(self):
        ok, why = available()
        if not ok:
            raise RuntimeError(why)
        if not model_ok(self.folder):
            raise RuntimeError("Das Modell ist noch nicht geladen")
        import cv2
        with self.lock:
            if self.net is None:
                self.net = cv2.dnn.readNet(model_path(self.folder))
        return self

    def detect_jpeg(self, data, conf=0.5):
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Bild nicht lesbar")
        return self.detect(img, conf), img.shape[0] * img.shape[1]

    def detect(self, bgr, conf=0.5):
        import cv2
        import numpy as np
        if self.net is None:
            self.load()
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        lb, r = letterbox(rgb)
        blob = np.transpose(lb, (2, 0, 1))[np.newaxis].astype(np.float32)
        with self.lock:
            self.net.setInput(blob)
            out = self.net.forward(self.net.getUnconnectedOutLayersNames())
        return decode(out[0], r, conf)
