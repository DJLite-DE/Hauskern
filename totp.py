# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
totp.py – Zwei-Faktor-Anmeldung mit 6-stelligem Code (TOTP, RFC 6238) und Notfallcodes. Nur Python-Bordmittel.

Passt zu jeder Authenticator-App: Apple „Passwörter“, Google Authenticator, Microsoft Authenticator, 2FAS, Aegis …

Felder am Nutzer (users.py):
    "totp"           geheimer Schlüssel (verschlüsselt wie andere Passwörter, siehe Hauskern.pyw protect)
    "totp_last"      Zeitfenster des zuletzt benutzten Codes – derselbe Code geht nicht zweimal
    "totp_backup"    Notfallcodes, nur als Hash; jeder geht genau einmal
    "totp_since"     seit wann an
    "totp_changed"   wann zuletzt ein- oder ausgeschaltet (für den Abgleich PC ↔ Heimserver)
    "totp_pending"   Schlüssel während der Einrichtung (noch nicht bestätigt)
    "totp_pending_ts"
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30            # Sekunden pro Code
DIGITS = 6
WINDOW = 1           # ein Zeitfenster davor/danach gilt auch (Handy-Uhr geht etwas falsch)
PENDING_MAX = 1800   # so lange bleibt ein angefangener QR-Code gültig
BACKUP_COUNT = 8
ISSUER = "Hauskern"
KEYS = ("totp", "totp_last", "totp_backup", "totp_since", "totp_changed", "totp_pending", "totp_pending_ts")
_ITER = 20_000


# ---------------- Codes ----------------
def new_secret():
    """160 Bit Zufall als Base32 ohne „=“ (so mögen es die Apps)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret, counter, digits=DIGITS):
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(num % 10 ** digits).zfill(digits)


def now_counter(t=None):
    return int((time.time() if t is None else t) // STEP)


def verify(secret, code, last=-1, t=None):
    """Gibt das passende Zeitfenster zurück (zum Merken gegen Wiederverwendung) oder None."""
    code = clean(code)
    if not secret or len(code) != DIGITS or not code.isdigit():
        return None
    c0 = now_counter(t)
    for c in range(c0 - WINDOW, c0 + WINDOW + 1):
        if c > last and hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def clean(code):
    return "".join(ch for ch in str(code or "") if ch.isalnum()).lower()


def uri(secret, account):
    """otpauth-Link – den versteht jede Authenticator-App (QR-Code oder antippen)."""
    label = quote(f"{ISSUER}:{account}", safe=":@")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def pretty(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------------- Notfallcodes ----------------
_ALPHA = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne 0/o, 1/l/i – gut abzuschreiben


def _hash_backup(code, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', clean(code).encode(), salt.encode(), _ITER).hex()}"


def new_backup_codes(n=BACKUP_COUNT):
    """→ (Codes zum Anzeigen, Hashes zum Speichern)."""
    codes = ["".join(secrets.choice(_ALPHA) for _ in range(4)) + "-" + "".join(secrets.choice(_ALPHA) for _ in range(4))
             for _ in range(n)]
    return codes, [_hash_backup(c) for c in codes]


def _backup_index(code, hashes):
    code = clean(code)
    if len(code) != 8:
        return None
    for i, h in enumerate(hashes or []):
        salt, _, _ = str(h).partition("$")
        if salt and hmac.compare_digest(_hash_backup(code, salt), h):
            return i
    return None


# ---------------- am Nutzer ----------------
def enabled(user):
    return bool((user or {}).get("totp"))


def backups_left(user):
    return len((user or {}).get("totp_backup") or [])


def check_user(user, code, unprotect):
    """Prüft Code oder Notfallcode und vermerkt die Benutzung direkt am Nutzer.
    → "code", "backup" oder None. unprotect: entschlüsselt den gespeicherten Schlüssel."""
    if not enabled(user) or not code:
        return None
    c = verify(unprotect(user["totp"]), code, int(user.get("totp_last", -1)))
    if c is not None:
        user["totp_last"] = c
        return "code"
    i = _backup_index(code, user.get("totp_backup"))
    if i is not None:
        user["totp_backup"] = [h for j, h in enumerate(user["totp_backup"]) if j != i]
        return "backup"
    return None


def start(user, protect, unprotect):
    """Einrichtung beginnen (oder die angefangene weiterführen) → Klartext-Schlüssel."""
    s = unprotect(user.get("totp_pending") or "")
    if s and time.time() - float(user.get("totp_pending_ts", 0)) < PENDING_MAX:
        return s
    s = new_secret()
    user["totp_pending"], user["totp_pending_ts"] = protect(s), int(time.time())
    return s


def confirm(user, code, unprotect):
    """Ersten Code prüfen → Liste der Notfallcodes (Klartext, nur jetzt sichtbar) oder None."""
    s = unprotect(user.get("totp_pending") or "")
    if not s or time.time() - float(user.get("totp_pending_ts", 0)) > PENDING_MAX:
        return None
    c = verify(s, code)
    if c is None:
        return None
    codes, hashes = new_backup_codes()
    user["totp"] = user.pop("totp_pending")
    user.pop("totp_pending_ts", None)
    user.update(totp_last=c, totp_backup=hashes, totp_since=int(time.time()), totp_changed=int(time.time()))
    return codes


def new_backups(user):
    codes, hashes = new_backup_codes()
    user["totp_backup"] = hashes
    return codes


def disable(user):
    for k in KEYS:
        user.pop(k, None)
    user["totp_changed"] = int(time.time())


def merge(incoming, existing):
    """Einstellungen vom PC kommen auf den Heimserver: Zwei-Faktor gehört zu dem Gerät, auf dem es zuletzt
    geändert wurde (eingerichtet wird es meist am Handy über den Heimserver)."""
    old = {u.get("id"): u for u in existing or []}
    out = []
    for u in incoming or []:
        u = dict(u)
        o = old.get(u.get("id"))
        if o and float(o.get("totp_changed", 0)) > float(u.get("totp_changed", 0)):
            for k in KEYS:
                u.pop(k, None)
                if k in o:
                    u[k] = o[k]
            u["totp_changed"] = o["totp_changed"]
        out.append(u)
    return out
