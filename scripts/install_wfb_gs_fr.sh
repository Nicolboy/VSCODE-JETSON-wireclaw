#!/usr/bin/env bash
#
# install_wfb_gs_fr.sh — Installation/reconstruction de /etc/wifibroadcast.cfg
# (ground station, Jetson)
#
# ─────────────────────────────────────────────────────────────────────────
# POURQUOI CE SCRIPT EXISTE (18 juillet 2026)
# ─────────────────────────────────────────────────────────────────────────
# Le script officiel wfb-ng (install_gs.sh, lancé habituellement via
#   curl -s https://raw.githubusercontent.com/svpcom/wfb-ng/master/scripts/install_gs.sh | sudo bash
# ) écrase INCONDITIONNELLEMENT /etc/wifibroadcast.cfg à chaque exécution,
# sans vérifier s'il existe déjà. Son template fixe wifi_region = 'BO' par
# défaut (commentaire du script original : "use BO or GY if you want max
# tx power"), ce qui ignore la réglementation française (limite 13dBm sur
# le canal 149).
#
# Ce one-liner a été relancé chez nous plusieurs fois en réflexe de
# dépannage (ex: après reconstruction du driver RTL8812AU, ou un
# `apt install wfb-ng` qui semblait cassé) — à chaque fois, wifi_region
# repassait silencieusement à 'BO', sans qu'on comprenne pourquoi la
# correction manuelle "revenait toute seule". Cause identifiée le
# 18/07/2026 (voir 20260718_wireclaw_methode_fable_contention_radio.md).
#
# ⚠️  NE PLUS JAMAIS relancer le curl officiel directement sur cette
#     machine. Utiliser CE script à la place pour toute réinstallation
#     complète de la configuration wfb-ng côté ground station.
#
# ⚠️  AUTRE SOURCE DE RÉGION, FICHIER SÉPARÉ — le même script officiel
#     pousse aussi /etc/modprobe.d/wfb.conf avec
#     "options cfg80211 ieee80211_regdom=RU" (régulation kernel cfg80211,
#     PAS wfb-ng applicatif). Une 3e valeur de région, différente de BO et
#     de FR. Ce script-ci NE TOUCHE PAS à ce fichier (portée volontairement
#     limitée à wifibroadcast.cfg). Vérifier séparément si besoin :
#       cat /etc/modprobe.d/wfb.conf
#
# ─────────────────────────────────────────────────────────────────────────
# QUAND UTILISER CE SCRIPT
# ─────────────────────────────────────────────────────────────────────────
# - Installation initiale de wfb-ng côté Jetson (ground station)
# - Reconstruction complète après un wfb-ng cassé / réinstallation du paquet
#
# QUAND NE PAS L'UTILISER
# ─────────────────────────────────────────────────────────────────────────
# Pour modifier UN SEUL paramètre (ex: juste la région ou juste le canal)
# sur une configuration qui fonctionne déjà, éditez directement le fichier
# existant plutôt que de relancer ce script :
#
#   sudo nano /etc/wifibroadcast.cfg
#   sudo systemctl restart wfb-server-gs.service
#
# ou en une ligne pour la région seule :
#
#   sudo sed -i "s/wifi_region = '.*'/wifi_region = 'FR'/" /etc/wifibroadcast.cfg
#   sudo systemctl restart wfb-server-gs.service
#
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

CFG_PATH="/etc/wifibroadcast.cfg"

echo "=== install_wfb_gs_fr.sh — configuration wfb-ng (ground station) ==="
echo

# ─────────────────────────────────────────────────────────────────────────
# Confirmation d'écrasement si le fichier existe déjà
# ─────────────────────────────────────────────────────────────────────────
if [ -f "$CFG_PATH" ]; then
    echo "⚠️  $CFG_PATH existe déjà. Contenu actuel (wifi_region / wifi_channel) :"
    grep -E "wifi_region|wifi_channel" "$CFG_PATH" || echo "  (paramètres non trouvés dans le fichier existant)"
    echo
    read -r -p "Écraser ce fichier et repartir d'une config neuve ? [y/N] " confirm
    case "$confirm" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "Annulé. Aucune modification effectuée."
            echo "Pour une modification ponctuelle, voir les commentaires en tête de ce script."
            exit 0
            ;;
    esac
    echo
fi

# ─────────────────────────────────────────────────────────────────────────
# Paramètres interactifs, avec défauts sûrs
# ─────────────────────────────────────────────────────────────────────────
read -r -p "Région wifi_region (réglementation CRDA) [FR]: " WIFI_REGION
WIFI_REGION="${WIFI_REGION:-FR}"

echo
echo "  149 = canal 5GHz (DFS/UNII, longue portée — config actuelle du projet)"
echo "  1   = canal 2.4GHz (compatibilité plus large, plus de bruit ambiant)"
read -r -p "Canal wifi_channel [149]: " WIFI_CHANNEL
WIFI_CHANNEL="${WIFI_CHANNEL:-149}"

echo
echo "Configuration retenue : wifi_region='$WIFI_REGION', wifi_channel=$WIFI_CHANNEL"
read -r -p "Confirmer et écrire $CFG_PATH ? [y/N] " confirm2
case "$confirm2" in
    [yY][eE][sS]|[yY]) ;;
    *)
        echo "Annulé. Aucune modification effectuée."
        exit 0
        ;;
esac

# ─────────────────────────────────────────────────────────────────────────
# Écriture de la config
#
# NOTE : structure reprise du template officiel wfb-ng
# (~/wfb-ng/scripts/install_gs.sh, copie datée du 14/07/2026), avec
# wifi_region forcé à une valeur choisie (FR par défaut) plutôt que 'BO'.
# Si le template upstream évolue, comparer avant de mettre à jour ce script.
# ─────────────────────────────────────────────────────────────────────────
sudo tee "$CFG_PATH" > /dev/null <<EOF
[common]
wifi_channel = $WIFI_CHANNEL   # 149 = 5GHz (DFS/UNII) | 1 = 2.4GHz
wifi_region = '$WIFI_REGION'   # Réglementation CRDA — FR par défaut ici,
                                # PAS 'BO'/'GY' (max tx power, hors cadre FR)

[gs_mavlink]
peer = 'connect://127.0.0.1:14550'  # outgoing connection

[gs_video]
peer = 'connect://127.0.0.1:5600'   # outgoing connection
EOF

echo
echo "✅ $CFG_PATH écrit."
echo "Redémarrage du service pour appliquer :"
echo "  sudo systemctl restart wfb-server-gs.service"
echo
echo "⚠️  RAPPEL — vérifier aussi /etc/modprobe.d/wfb.conf :"
echo "   ce fichier peut contenir 'ieee80211_regdom=RU' (poussé par le même"
echo "   script officiel wfb-ng), une 3e région différente de FR/BO, au niveau"
echo "   du driver kernel cette fois. Ce script NE LE MODIFIE PAS"
echo "   volontairement (fichier plus sensible, hors scope de wifibroadcast.cfg)."
echo "   Vérifier : cat /etc/modprobe.d/wfb.conf"