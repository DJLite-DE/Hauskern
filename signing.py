# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
signing.py – digitale Unterschrift für Hauskern-Updates (Ed25519, RFC 8032, nur Python-Bordmittel).

Jedes offizielle Paket enthält SIGNATUR.json: eine Liste aller Programmdateien mit Prüfsumme (SHA-256),
unterschrieben mit Pascals geheimem Schlüssel. Hauskern kennt nur den öffentlichen Schlüssel und prüft damit,
dass ein Update wirklich aus der offiziellen Quelle stammt und keine Datei verändert wurde.
Den geheimen Schlüssel gibt es nur beim Bau der Pakete – er ist NICHT in Hauskern enthalten.
"""
import hashlib
import json

# öffentlicher Schlüssel der offiziellen Hauskern-Pakete
PUBLIC_KEY = "b3785aaa491c900061778dbee1259494de93af311c5084c3b588052dcaf15aee"
MANIFEST = "SIGNATUR.json"

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _inv(x):
    return pow(x, _p - 2, _p)


def _xrecover(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _I % _p
    if (x * x - x2) % _p:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _xrecover(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _p
    b = (y1 + x1) * (y2 + x2) % _p
    c = t1 * 2 * _d * t2 % _p
    dd = z1 * 2 * z2 % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _p, g * h % _p, f * g % _p, e * h % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _xrecover(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _h(*parts):
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def public_from_seed(seed):
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    return _compress(_mul(a, _G))


def sign(seed, msg):
    """Nur fürs Bauen der Pakete (seed = 32 Byte geheimer Schlüssel)."""
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    A = _compress(_mul(a, _G))
    r = _h(h[32:], msg) % _q
    R = _compress(_mul(r, _G))
    s = (r + _h(R, A, msg) * a) % _q
    return R + int.to_bytes(s, 32, "little")


def verify(pub, msg, sig):
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        A, R = _decompress(pub), _decompress(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if A is None or R is None or s >= _q:
            return False
        k = _h(sig[:32], pub, msg) % _q
        return _equal(_mul(s, _G), _add(R, _mul(k, A)))
    except (TypeError, ValueError):
        return False


# ---------------- Manifest ----------------
def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _canon(man):
    body = {k: v for k, v in man.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def make_manifest(seed, version, edition, files):
    """files: {relativer Pfad: Bytes} → Manifest-Dict mit Unterschrift."""
    man = {"app": "Hauskern", "version": version, "edition": edition,
           "files": {k: sha256(v) for k, v in sorted(files.items())}}
    man["sig"] = sign(seed, _canon(man)).hex()
    return man


def check_manifest(raw, pub_hex=None):
    """Bytes von SIGNATUR.json → Manifest-Dict, wenn die Unterschrift stimmt, sonst None."""
    pub_hex = pub_hex or PUBLIC_KEY
    try:
        man = json.loads(raw.decode("utf-8"))
        if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
            return None
        ok = verify(bytes.fromhex(pub_hex), _canon(man), bytes.fromhex(str(man.get("sig", ""))))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return man if ok else None
