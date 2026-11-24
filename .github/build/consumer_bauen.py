"""
consumer_bauen.py – baut auf GitHub (Actions) automatisch die signierte Consumer-ZIP aus diesem Repo.

Der geheime Signierschlüssel kommt aus dem Repository-Secret HAUSKERN_SIGNIERSCHLUESSEL
(Umgebungsvariable). Ohne Schlüssel bricht der Bau ab – unsignierte Pakete werden nie veröffentlicht.

Aufruf:  python3 -I .github/build/consumer_bauen.py --ausgabe out
Gibt am Ende die Version aus und schreibt release-notes.md (Abschnitt „Neu in Version …“ aus LIESMICH.md).
"""
import argparse
import io
import json
import os
import re
import sys
import zipfile

ap = argparse.ArgumentParser()
ap.add_argument("--ausgabe", default="out")
A = ap.parse_args()

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, SRC)
import signing  # noqa: E402
import updater  # noqa: E402
import edition  # noqa: E402

NEVER = {"meldeziel.json", signing.MANIFEST, "Hauskern-Umzug.zip", "privat.py"}
PRIVATE_MARKERS = [b"2cf9f5dfacdc01e6", b"hauskern_gs_bot"]
BLOCK_MARKERS = [b"@@FAMILIE", b"@@CONSUMER", b"@@ENDE"]


def fail(msg):
    print("::error::" + msg)
    raise SystemExit(1)


if edition.EDITION != "consumer":
    fail("edition.py steht nicht auf consumer – Abbruch")
if not edition.GITHUB_REPO:
    fail("edition.GITHUB_REPO ist leer – Abbruch")

key = os.environ.get("HAUSKERN_SIGNIERSCHLUESSEL", "").strip()
if not key:
    fail("Secret HAUSKERN_SIGNIERSCHLUESSEL fehlt (Settings → Secrets and variables → Actions)")
try:
    seed = bytes.fromhex(key.split()[-1])
except ValueError:
    fail("HAUSKERN_SIGNIERSCHLUESSEL ist kein gültiger Schlüssel")
if signing.public_from_seed(seed).hex() != signing.PUBLIC_KEY:
    fail("Signierschlüssel passt nicht zum PUBLIC_KEY in signing.py")

files = {}
for full, rel in updater._program_files(SRC):
    if rel.startswith(".github/") or rel.split("/")[-1] in NEVER or "__pycache__" in rel:
        continue
    with open(full, "rb") as f:
        data = f.read()
    for m in BLOCK_MARKERS:
        if re.search(rb"^\s*" + m + rb"\s*$", data, re.M):
            fail(f"{rel} enthält noch {m.decode()}")
    for m in PRIVATE_MARKERS:
        if m in data:
            fail(f"{rel} enthält Privates – Abbruch")
    if rel.endswith((".py", ".pyw")):
        compile(data, rel, "exec")
    files[rel] = data

version = re.search(rb'^VERSION\s*=\s*"([^"]+)"', files["Hauskern.pyw"], re.M).group(1).decode()
man = signing.make_manifest(seed, version, "consumer", files)
raw = json.dumps(man, indent=1, ensure_ascii=False).encode("utf-8")
if not signing.check_manifest(raw):
    fail("Signatur-Selbsttest fehlgeschlagen")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in sorted(files):
        z.writestr("Hauskern/" + rel, files[rel])
    z.writestr("Hauskern/" + signing.MANIFEST, raw)
os.makedirs(A.ausgabe, exist_ok=True)
name = f"Consumer-Hauskern-{version}.zip"
with open(os.path.join(A.ausgabe, name), "wb") as f:
    f.write(buf.getvalue())

# Release-Text: neuester Abschnitt „## Neu in Version …“ aus LIESMICH.md
txt = files["LIESMICH.md"].decode("utf-8")
m = re.search(r"^## Neu in Version.*?(?=^## |\Z)", txt, re.M | re.S)
neu = m.group(0).strip() if m else f"## Hauskern {version}"
notes = (neu + "\n\n---\n"
         f"**Installation:** `{name}` herunterladen, entpacken, `Hauskern-Setup.cmd` starten "
         "(Linux-Heimserver: siehe `ANLEITUNG-HEIMSERVER.txt`).\n\n"
         "**Update:** die ZIP *nicht entpackt* in den Download-Ordner legen und Hauskern starten. "
         "Jedes Update ist digital unterschrieben und wird vor der Installation geprüft.\n\n"
         "⚠️ Hauskern nie per Portfreigabe ins Internet stellen – von unterwegs immer über VPN. "
         "Lizenz: GPL-3.0-or-later, ohne jede Gewähr.\n")
with open(os.path.join(A.ausgabe, "release-notes.md"), "w", encoding="utf-8") as f:
    f.write(notes)
print(f"version={version}")
print(f"{name}: {len(files)} Dateien, {len(buf.getvalue()) // 1024} KB")
