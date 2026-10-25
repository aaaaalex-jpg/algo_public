#!/bin/bash
# Supprime l'exécution automatique (les données et les rapports sont conservés).
LABEL="com.algo.recommandations"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ Planification supprimée."
