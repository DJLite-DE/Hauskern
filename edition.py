# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
edition.py – welche Hauskern-Variante das ist.

* "familie":  Pascals Familien-Version – „Problem melden“ geht direkt per Telegram an Pascal.
* "consumer": die öffentliche Version (GitHub) – „Problem melden“ öffnet eine Fehlermeldung auf der Projektseite.

Diese Datei wird beim Bau der Pakete geschrieben und ist Teil der Signatur.
"""
EDITION = "consumer"
GITHUB_REPO = "DJLite-DE/hauskern"          # z. B. "benutzername/hauskern" – Projektseite für Fehlermeldungen & Downloads
DONATE_URL = ""           # z. B. "https://github.com/sponsors/benutzername" – nur Consumer, leer = kein Spenden-Eintrag

LABEL = {"familie": "Familie", "consumer": ""}.get(EDITION, "")


def is_family():
    return EDITION == "familie"


def issues_url():
    return f"https://github.com/{GITHUB_REPO}/issues" if GITHUB_REPO else ""


def project_url():
    return f"https://github.com/{GITHUB_REPO}" if GITHUB_REPO else ""
