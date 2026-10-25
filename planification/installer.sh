#!/bin/bash
# ---------------------------------------------------------------------------
# Installe l'exécution automatique de l'algorithme sur ton Mac (launchd).
# Par défaut : du lundi au vendredi à 6 h 30.
#
#   bash planification/installer.sh            -> 6 h 30
#   bash planification/installer.sh 7 15       -> 7 h 15
#
# Si le Mac est en veille à l'heure prévue, macOS lance le traitement au réveil.
# ---------------------------------------------------------------------------
set -e

ALGO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$HOME/miniforge3/envs/trading/bin/python"
LABEL="com.algo.recommandations"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HEURE="${1:-6}"
MINUTE="${2:-30}"

if [ ! -x "$PYTHON" ]; then
  echo "❌ Python de l'environnement 'trading' introuvable : $PYTHON"
  echo "   Vérifie avec : conda activate trading && which python"
  exit 1
fi

mkdir -p "$ALGO_DIR/logs" "$HOME/Library/LaunchAgents"

JOURS=""
for J in 1 2 3 4 5; do
  JOURS="$JOURS
    <dict><key>Weekday</key><integer>$J</integer><key>Hour</key><integer>$HEURE</integer><key>Minute</key><integer>$MINUTE</integer></dict>"
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ALGO_DIR/main.py</string>
    <string>tout</string>
  </array>
  <key>WorkingDirectory</key><string>$ALGO_DIR</string>
  <key>StartCalendarInterval</key>
  <array>$JOURS
  </array>
  <key>StandardOutPath</key><string>$ALGO_DIR/logs/planification.log</string>
  <key>StandardErrorPath</key><string>$ALGO_DIR/logs/planification.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf "✅ Planification installée : du lundi au vendredi à %dh%02d\n" "$HEURE" "$MINUTE"
echo "   Rapport du jour : $ALGO_DIR/rapports/dernier_rapport.html"
echo "   Journal         : $ALGO_DIR/logs/planification.log"
echo "   Tester maintenant : launchctl kickstart gui/$(id -u)/$LABEL"
