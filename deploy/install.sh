#!/usr/bin/env bash
# StudiRadar auf dem Raspberry Pi installieren.
#   ./deploy/install.sh          → ILIAS, Kalender, eigene Aufgaben, Push
#   ./deploy/install.sh --klips  → zusätzlich Chromium + Playwright für KLIPS (64-bit OS nötig)
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
WITH_KLIPS=0
[[ "${1:-}" == "--klips" ]] && WITH_KLIPS=1

echo "==> Systempakete"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

echo "==> Python-Umgebung (.venv)"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [[ $WITH_KLIPS == 1 ]]; then
  if [[ "$(uname -m)" != "aarch64" && "$(uname -m)" != "x86_64" ]]; then
    echo "!! KLIPS braucht ein 64-bit-Betriebssystem (uname -m = $(uname -m))."
    echo "   Raspberry Pi OS (64-bit) installieren oder KLIPS über den Kalender-Abo-Link nutzen."
    exit 1
  fi
  echo "==> Chromium + Playwright für KLIPS"
  sudo apt-get install -y -qq chromium || sudo apt-get install -y -qq chromium-browser
  PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 .venv/bin/pip install -q -r requirements-klips.txt
  CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"
  echo "   Chromium: ${CHROMIUM:-nicht gefunden} → in config.toml als chromium_path eintragen"
fi

if [[ ! -f config.toml ]]; then
  echo "==> config.toml anlegen"
  cp config.example.toml config.toml
  chmod 600 config.toml
  echo "   Bitte jetzt Zugangsdaten eintragen:  nano $APP_DIR/config.toml"
fi

echo "==> systemd-Dienst"
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$APP_DIR|g" deploy/studiradar.service \
  | sudo tee /etc/systemd/system/studiradar.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable studiradar >/dev/null

cat <<MSG

Fertig! Nächste Schritte:
  1. nano config.toml                     (Passwörter eintragen)
  2. .venv/bin/python -m studiradar login ilias   (ILIAS-Login testen)
  3. sudo systemctl start studiradar      (Dienst starten)
  4. Tailscale einrichten (siehe README) für HTTPS + Zugriff vom Handy
Logs ansehen: journalctl -u studiradar -f
MSG
