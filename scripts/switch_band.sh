#!/usr/bin/env bash
#
# switch_band.sh — Bascule entre les deux profils radio complets et
# testés (5GHz canal 149 / 2.4GHz canal 6), Jetson + Pi Zero synchronisés.
#
# Les deux profils vivent en dur dans les repos (config/wifibroadcast_gs_*.cfg
# ici, config/wifibroadcast_drone_*.cfg dans wireclaw-pizero, cloné
# localement sur le Jetson) — ce script les copie tels quels, il ne
# patche rien. Pour modifier un profil, édite le fichier de référence
# correspondant dans le repo concerné, commit/push, puis relance ce
# script pour le déployer.
#
# 5GHz = bande opérationnelle par défaut (cf v17 §6 — 2.4GHz sujet à
# interférence domestique confirmée, usage diagnostic/comparatif
# uniquement).
#
# USAGE : ./switch_band.sh 5ghz | 2.4ghz

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIZERO_REPO="${HOME}/wireclaw-pizero"
CFG_JETSON="/etc/wifibroadcast.cfg"
SSH_PI="jetson-pi"

usage() {
    echo "Usage: $0 {5ghz|2.4ghz}"
    exit 1
}
[ $# -eq 1 ] || usage

case "$1" in
    5ghz)
        GS_PROFILE="${REPO_DIR}/config/wifibroadcast_gs_5ghz.cfg"
        DRONE_PROFILE="${PIZERO_REPO}/config/wifibroadcast_drone_5ghz.cfg"
        BAND_LABEL="5GHz (canal 149) — bande opérationnelle"
        ;;
    2.4ghz)
        GS_PROFILE="${REPO_DIR}/config/wifibroadcast_gs_24ghz.cfg"
        DRONE_PROFILE="${PIZERO_REPO}/config/wifibroadcast_drone_24ghz.cfg"
        BAND_LABEL="2.4GHz (canal 6) — ⚠️ interférence domestique possible, usage diagnostic uniquement"
        ;;
    *)
        usage
        ;;
esac

[ -f "$GS_PROFILE" ] || { echo "Profil introuvable : $GS_PROFILE"; exit 1; }
[ -f "$DRONE_PROFILE" ] || { echo "Profil introuvable : $DRONE_PROFILE (pense à faire git pull dans wireclaw-pizero)"; exit 1; }

echo "=== Bascule vers $BAND_LABEL ==="
echo
echo "--- Diff profil Jetson (système actuel vs cible) ---"
diff -u "$CFG_JETSON" "$GS_PROFILE" || true
echo
read -r -p "Appliquer ce profil sur les deux machines ? [y/N] " confirm
[[ "$confirm" =~ ^[yY]([eE][sS])?$ ]] || { echo "Annulé."; exit 0; }

# Backup unique (écrasé à chaque bascule, pas d'accumulation de .bak)
sudo cp "$CFG_JETSON" "${CFG_JETSON}.bak_last"
ssh "$SSH_PI" "sudo cp /etc/wifibroadcast.cfg /etc/wifibroadcast.cfg.bak_last"

# Application — copie directe du profil complet, pas de patch
sudo cp "$GS_PROFILE" "$CFG_JETSON"
scp "$DRONE_PROFILE" "${SSH_PI}:/tmp/wifibroadcast_new.cfg"
ssh "$SSH_PI" "sudo cp /tmp/wifibroadcast_new.cfg /etc/wifibroadcast.cfg && rm /tmp/wifibroadcast_new.cfg"

echo
echo "Redémarrage des services..."
sudo systemctl restart wfb-server-gs.service
ssh "$SSH_PI" "sudo systemctl restart wfb-server-drone.service"
sleep 5

echo
echo "--- Vérification PHY (Jetson) ---"
iw dev wlx54c9ff02f26e info | grep -E "channel|width" || echo "⚠️ Lecture PHY échouée"

echo
echo "--- Vérification config chargée (API JSON sol) ---"
timeout 2 nc 127.0.0.1 8103 | head -1 | python3 -m json.tool | grep -A5 '"common"' || echo "⚠️ API 8103 non accessible"

echo
echo "✅ Bascule vers $BAND_LABEL terminée."
