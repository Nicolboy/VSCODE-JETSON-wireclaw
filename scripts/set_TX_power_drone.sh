#!/bin/bash

# WireClaw TX Power Control - Drone (Pi Zero)
# Version: 1.0.0
# Description: Modifie la puissance d'émission (TX) du Pi Zero via modprobe rtw_tx_pwr_idx_override
# Usage: ./set_TX_power_drone.sh <power_index>
# Example: ./set_TX_power_drone.sh 10

set -e

VERSION="1.0.0"
SCRIPT_NAME=$(basename "$0")

# Configuration
PIZERO_SSH_ALIAS="jetson-pi"
PIZERO_MODULE="rtl88xxau_wfb"
PIZERO_IFACE="wlan-drone"
IW_CMD="/usr/sbin/iw"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Fonctions
print_header() {
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  WireClaw TX Power Control - DRONE (Pi Zero) v${VERSION}${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

show_help() {
    cat << EOF
${BLUE}Usage:${NC}
  $SCRIPT_NAME <power_index>

${BLUE}Arguments:${NC}
  power_index    Index de puissance TX (0-63)
                 0   = 0 dBm (minimum)
                 10  = ~3 dBm
                 20  = ~6 dBm
                 31  = ~10 dBm
                 42  = ~15 dBm
                 52  = ~20 dBm
                 63  = ~23 dBm (maximum)

${BLUE}Exemples:${NC}
  $SCRIPT_NAME 10    # Puissance faible
  $SCRIPT_NAME 31    # Puissance moyenne
  $SCRIPT_NAME 52    # Puissance haute
  $SCRIPT_NAME 63    # Puissance maximale

${BLUE}Interface:${NC}
  ${PIZERO_IFACE} (via SSH: ${PIZERO_SSH_ALIAS})

${BLUE}Méthode:${NC}
  ssh jetson-pi "sudo modprobe -r ${PIZERO_MODULE} && sudo modprobe ${PIZERO_MODULE} rtw_tx_pwr_idx_override=<index>"

${BLUE}Notes:${NC}
  - Recharge le module du driver
  - Nécessite sudo sur Pi Zero
  - Session sudo "chaude" requise

EOF
}

# Validation
validate_input() {
    if [ -z "$1" ]; then
        print_error "Aucun index spécifié"
        echo ""
        show_help
        exit 1
    fi

    if ! [[ "$1" =~ ^[0-9]+$ ]]; then
        print_error "L'index doit être un nombre entier"
        exit 1
    fi

    if [ "$1" -lt 0 ] || [ "$1" -gt 63 ]; then
        print_error "L'index doit être entre 0 et 63 (reçu: $1)"
        exit 1
    fi
}

# Obtenir la puissance actuelle
get_current_power() {
    ssh "$PIZERO_SSH_ALIAS" "$IW_CMD dev $PIZERO_IFACE info 2>/dev/null | grep txpower | awk '{print \$2, \$3}'" 2>/dev/null || echo "unknown"
}

# Modifier la puissance
set_power() {
    local index=$1

    print_info "Modification du Pi Zero (Drone)..."
    echo -n "  Puissance actuelle: "
    get_current_power

    print_info "Recharge du module ${PIZERO_MODULE}..."
    print_info "  Commande: sudo modprobe -r ${PIZERO_MODULE} && sudo modprobe ${PIZERO_MODULE} rtw_tx_pwr_idx_override=$index"

    if ssh "$PIZERO_SSH_ALIAS" "sudo -n modprobe -r $PIZERO_MODULE && sudo -n modprobe $PIZERO_MODULE rtw_tx_pwr_idx_override=$index" 2>/dev/null; then
        sleep 2  # Attendre le rechargement complet du module et la reconnexion
        echo -n "  Puissance nouvelle:  "
        get_current_power
        print_success "Pi Zero configuré (index=$index)"
        return 0
    else
        print_error "Impossible de recharger le module"
        print_error "Vérifiez que:"
        print_error "  1. SSH vers ${PIZERO_SSH_ALIAS} fonctionne"
        print_error "  2. Une session sudo 'chaude' est active sur Pi Zero"
        print_error "  3. Le module ${PIZERO_MODULE} existe"
        return 1
    fi
}

# Main
main() {
    print_header

    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -v|--version)
            echo "WireClaw TX Power Control - Drone v${VERSION}"
            exit 0
            ;;
    esac

    validate_input "$1"

    echo ""
    set_power "$1"
    STATUS=$?

    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"

    if [ $STATUS -eq 0 ]; then
        print_success "Puissance TX Drone configurée (index=$1)"
        exit 0
    else
        print_error "Impossible de configurer la puissance TX"
        exit 1
    fi
}

main "$@"
