#!/bin/bash
# =============================================================================
#  Hauskern – Installation / Update auf dem Heimserver
#  Läuft auf: Raspberry Pi (Raspberry Pi OS) und Mini-PCs/PCs mit Debian, Ubuntu oder Linux Mint.
#
#  1. Den Ordner "Hauskern" (und optional "Hauskern-Umzug.zip") auf den Heimserver kopieren
#  2. Im Ordner ausführen:   bash install.sh
#  Später für ein Update einfach die neuen Dateien drüberkopieren und erneut ausführen.
# =============================================================================
set -e
APPDIR=/opt/hauskern
SRC="$(cd "$(dirname "$0")" && pwd)"
RUNUSER="${SUDO_USER:-$USER}"
RUNHOME="$(getent passwd "$RUNUSER" | cut -d: -f6)"
CONF="$RUNHOME/.config/Hauskern"

if [ "$RUNUSER" = "root" ]; then
  echo "Bitte als normaler Benutzer starten (nicht als root):  bash install.sh"; exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Dieses Skript braucht ein Debian-artiges Linux (Raspberry Pi OS, Debian, Ubuntu, Linux Mint)."
  echo "Auf anderen Systemen bitte von Hand installieren: python3, python3-tk (tkinter), ffmpeg, xvfb – und dann"
  echo "  xvfb-run -a python3 Hauskern.pyw --headless   als Dienst starten."
  exit 1
fi
if [ -r /proc/device-tree/model ] && grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
  echo "==> Gerät: $(tr -d '\0' </proc/device-tree/model)"
else
  DEV="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null) $(cat /sys/class/dmi/id/product_version 2>/dev/null) $(cat /sys/class/dmi/id/product_name 2>/dev/null)"
  DEV="$(echo "$DEV" | sed -e 's/To Be Filled By O.E.M.//g' -e 's/Default string//g' -e 's/  */ /g' -e 's/^ //;s/ $//')"
  echo "==> Gerät: ${DEV:-Linux-PC} ($(uname -m))"
fi
. /etc/os-release 2>/dev/null && echo "==> System: ${PRETTY_NAME:-unbekannt}"

echo "==> Pakete installieren (Python, Tk, ffmpeg für Kameras und Zeitraffer, virtueller Bildschirm)"
sudo apt-get update -qq
sudo apt-get install -y python3 python3-tk ffmpeg xvfb xauth iputils-ping rsync >/dev/null
echo "==> Für die Kamera-Erkennung: OpenCV + NumPy (optional, ca. 100 MB)"
sudo apt-get install -y python3-opencv python3-numpy >/dev/null 2>&1 \
  || echo "    (nicht verfügbar – Kamera-Erkennung bleibt aus, alles andere läuft)"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' || {
  echo "⚠ Python $PYV ist zu alt – Hauskern braucht mindestens 3.10 (Debian 12 / Ubuntu 22.04 oder neuer)."; exit 1; }
echo "==> Python $PYV"

TZNOW="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unbekannt)"
case "$TZNOW" in
  Etc/UTC|UTC|Europe/London|GMT|Etc/GMT|Universal)
    echo
    echo "==> Zeitzone steht auf „$TZNOW“ – für Deutschland wäre „Europe/Berlin“ richtig"
    echo "    (sonst liegen Automationen, Heizzeiten und Anrufzeiten eine Stunde daneben)."
    read -r -p "    Jetzt auf Europe/Berlin umstellen? [J/n] " ANTW </dev/tty || ANTW="n"
    case "$ANTW" in
      n|N|nein|Nein) echo "    Okay, bleibt so. Später:  sudo timedatectl set-timezone Europe/Berlin" ;;
      *) sudo timedatectl set-timezone Europe/Berlin && echo "    ✅ Zeitzone: Europe/Berlin" ;;
    esac
    ;;
  *) echo "==> Zeitzone: $TZNOW" ;;
esac

echo "==> Programm nach $APPDIR kopieren"
sudo mkdir -p "$APPDIR"
sudo rsync -a --delete --exclude "Hauskern-Umzug.zip" --exclude "__pycache__" "$SRC/" "$APPDIR/" 2>/dev/null \
  || { sudo rm -rf "$APPDIR"/*; sudo cp -r "$SRC"/. "$APPDIR"/; sudo rm -f "$APPDIR/Hauskern-Umzug.zip"; }
sudo rm -rf "$APPDIR/__pycache__"
sudo chown -R "$RUNUSER": "$APPDIR"

mkdir -p "$CONF"
for ZIP in "$SRC/Hauskern-Umzug.zip" "$RUNHOME/Hauskern-Umzug.zip" "$SRC/../Hauskern-Umzug.zip"; do
  if [ -f "$ZIP" ]; then
    if [ -f "$CONF/config.json" ]; then
      echo "==> Umzugspaket gefunden – vorhandene Einstellungen werden gesichert (config.json.bak)"
      cp "$CONF/config.json" "$CONF/config.json.bak"
    fi
    echo "==> Umzugspaket übernehmen: $ZIP"
    python3 -m zipfile -e "$ZIP" "$CONF"
    chmod 600 "$CONF/config.json"
    chown -R "$RUNUSER": "$CONF"
    mv "$ZIP" "$ZIP.importiert"   # damit ein späteres Update die Einstellungen nicht erneut überschreibt
    echo "    !!! Bitte $ZIP.importiert löschen – die Datei enthält deine Passwörter im Klartext."
    break
  fi
done

echo "==> Dienst einrichten (startet automatisch beim Booten)"
sudo tee /etc/systemd/system/hauskern.service >/dev/null <<EOF
[Unit]
Description=Hauskern – dein Zuhause, lokal
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
User=$RUNUSER
WorkingDirectory=$APPDIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/xvfb-run -a /usr/bin/python3 $APPDIR/Hauskern.pyw --headless
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "==> SSD-Prüfung einrichten (alle 6 Stunden, liest die Selbstdiagnose der SSD)"
sudo apt-get install -y smartmontools >/dev/null 2>&1 || echo "   (smartmontools nicht installierbar – SSD-Prüfung bleibt aus)"
sudo mkdir -p /usr/local/lib/hauskern /var/lib/hauskern
sudo tee /usr/local/lib/hauskern/ssd-check.sh >/dev/null <<'SSDSCRIPT'
#!/bin/sh
# Schreibt die SMART-Werte jedes Laufwerks als JSON nach /var/lib/hauskern – Hauskern liest sie nur.
command -v smartctl >/dev/null 2>&1 || exit 0
for d in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}'); do
  smartctl -j -H -A -i "/dev/$d" > "/var/lib/hauskern/smart-$d.json.tmp" 2>/dev/null
  mv -f "/var/lib/hauskern/smart-$d.json.tmp" "/var/lib/hauskern/smart-$d.json"
  chmod 644 "/var/lib/hauskern/smart-$d.json"
done
exit 0
SSDSCRIPT
sudo chmod 755 /usr/local/lib/hauskern/ssd-check.sh
sudo tee /etc/systemd/system/hauskern-ssd.service >/dev/null <<'SSDUNIT'
[Unit]
Description=Hauskern – SSD-Zustand auslesen

[Service]
Type=oneshot
ExecStart=/usr/local/lib/hauskern/ssd-check.sh
SSDUNIT
sudo tee /etc/systemd/system/hauskern-ssd.timer >/dev/null <<'SSDTIMER'
[Unit]
Description=Hauskern – SSD-Zustand alle 6 Stunden

[Timer]
OnBootSec=3min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
SSDTIMER

sudo systemctl daemon-reload
sudo systemctl enable hauskern >/dev/null 2>&1
sudo systemctl enable --now hauskern-ssd.timer >/dev/null 2>&1 || true
sudo systemctl start hauskern-ssd.service >/dev/null 2>&1 || true
sudo systemctl restart hauskern

PORTFW=$(python3 -c "import json;print(json.load(open('$CONF/config.json')).get('web_port',8770))" 2>/dev/null || echo 8770)
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "==> Firewall (ufw) aktiv – Port $PORTFW fürs Heimnetz freigeben"
  for NET in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
    sudo ufw allow from "$NET" to any port "$PORTFW" proto tcp >/dev/null
  done
fi
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  echo "==> Firewall (firewalld) aktiv – Port $PORTFW freigeben"
  sudo firewall-cmd --permanent --add-port="$PORTFW/tcp" >/dev/null && sudo firewall-cmd --reload >/dev/null
fi

sleep 4
IP=$(hostname -I | awk '{print $1}')
PORT=$(python3 -c "import json;print(json.load(open('$CONF/config.json')).get('web_port',8770))" 2>/dev/null || echo 8770)
echo
if systemctl is-active --quiet hauskern; then
  echo "✅ Hauskern läuft!"
else
  echo "⚠ Hauskern läuft noch nicht – Details:  journalctl -u hauskern -n 50"
fi
echo
echo "   Handy/PC-Browser:   http://$IP:$PORT   (oder http://$(hostname):$PORT)"
echo "   Status:             systemctl status hauskern"
echo "   Neustart:           sudo systemctl restart hauskern"
echo "   Fehlerprotokoll:    $CONF/fehlerprotokoll.log"
if [ ! -f "$CONF/config.json" ]; then
  echo
  echo "   Noch keine Einstellungen – zwei Wege:"
  echo "   • Ohne PC: http://$IP:$PORT im Handy öffnen – Hauskern führt dich durch die Einrichtung."
  echo "   • Vom PC: in Hauskern → ⋯ Mehr → „Heimserver“ → Umzugspaket erstellen, neben install.sh legen"
  echo "     und install.sh erneut ausführen."
fi
