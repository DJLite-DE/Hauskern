# Hauskern-Erweiterungen

Eine Erweiterung ist eine `.py`-Datei im Ordner **erweiterungen** (im Hauskern-Datenordner:
Windows `%APPDATA%\Hauskern\erweiterungen`, Linux `~/.config/Hauskern/erweiterungen`).
Neue oder geänderte Dateien laufen erst, wenn sie freigegeben wurden (⋯ Mehr → 🧩 Erweiterungen).

```python
NAME = "Mein Ding"
DESCRIPTION = "Was es tut – steht in der Liste"
VERSION = "1.0"

def setup(hk):
    hk.tile("k", "🧩 Titel", "Wert", "Untertitel", on_tap=lambda: "Antwort als Toast")
    hk.every(300, lambda: ...)          # alle 5 Minuten (mind. 10 s)
    hk.on("print_finished", lambda ev: hk.notify("Fertig", ev["printer"]))
    hk.siri(["wort1", "wort2"], lambda text: "Antwort für Siri")
```

## Was `hk` kann
| Aufruf | Bedeutung |
|---|---|
| `hk.tile(key, titel, wert, unter, icon, on_tap, color)` | Kachel anlegen/aktualisieren (PC + Handy, Bereich „Erweiterungen“) |
| `hk.remove_tile(key)` | Kachel entfernen |
| `hk.every(sekunden, fn)` | regelmäßig ausführen (eigener Thread) |
| `hk.on(ereignis, fn)` | auf Ereignisse reagieren – `fn(ev)` bekommt ein Wörterbuch |
| `hk.siri(wörter, fn)` | eigener Siri-Befehl, alle Wörter müssen vorkommen – `fn(text)` gibt die Antwort zurück |
| `hk.notify(titel, text)` | Push-Nachricht (Art „Erweiterungen“, abschaltbar) |
| `hk.log(text)` | Eintrag im Logbuch |
| `hk.devices()` / `hk.persons()` / `hk.printers()` | aktueller Stand lesen |
| `hk.prints(tage)` | Druckverlauf |
| `hk.location()` | (Breite, Länge) aus den Wetter-Einstellungen |
| `hk.switch(name, an)` / `hk.heat(name, grad)` | Steckdose schalten / Heizung stellen |
| `hk.get(key)` / `hk.set(key, wert)` | kleines Gedächtnis, bleibt über Neustarts |
| `teardown()` (optional, in der Datei) | wird beim Ausschalten aufgerufen |

## Ereignisse
| Ereignis | Wann |
|---|---|
| `person_home` | Person kommt nach Hause (ev: name) |
| `person_away` | Person geht weg (ev: name) |
| `print_started` | Druck startet (ev: printer, file) |
| `print_finished` | Druck fertig (ev: printer, file) |
| `print_failed` | Druck fehlgeschlagen/abgebrochen (ev: printer, file) |
| `print_problem` | KI im Drucker meldet möglichen Fehldruck (ev: printer, paused) |
| `internet_down` | Internet weg |
| `internet_up` | Internet wieder da |
| `switch_changed` | Steckdose an/aus (ev: name, on) |
| `new_device` | Neues Gerät im Netzwerk (ev: name, ip, mac) |
| `call_missed` | Verpasster Anruf (ev: name, number) |

Fehler in einer Erweiterung landen im Logbuch und in der Erweiterungs-Liste – Hauskern selbst läuft weiter.
Erweiterungen laufen nur dort, wo die Automationen laufen (Heimserver oder PC), damit nichts doppelt passiert.
