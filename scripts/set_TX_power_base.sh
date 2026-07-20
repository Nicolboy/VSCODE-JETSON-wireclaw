#!/bin/bash

# WireClaw TX Power Control - Base (Jetson)
# Version: 1.0.0
# Description: Modifie la puissance d'émission (TX) du Jetson via iw
# Usage: ./set_TX_power_base.sh <power_index>
# Example: ./set_TX_power_base.sh 10

set -e

VERSION="1.0.0"
SCRIPT_NAME=$(basename "$0")

# Configuration
JETSON_IFACE="wlx54c9ff02f26e"
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
    echo -e "${BLUE}  WireClaw TX Power Control - BASE (Jetson) v${VERSION}${NC}"
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
  ${JETSON_IFACE}

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

# Conversion index (0-63) -> dBm (0-23)
index_to_dbm() {
    echo $(( ($1 * 23) / 63 ))
}

# Obtenir la puissance actuelle
get_current_power() {
    $IW_CMD dev "$JETSON_IFACE" info 2>/dev/null | grep "txpower" | awk '{print $2, $3}' || echo "unknown"
}

# Modifier la puissance
set_power() {
    local index=$1
    local dbm=$(index_to_dbm "$index")
    local mbm=$(( dbm * 100 ))

    print_info "Modification du Jetson (Base)..."
    echo -n "  Puissance actuelle: "
    get_current_power

    if sudo -n $IW_CMD dev "$JETSON_IFACE" set txpower fixed "$mbm" 2>/dev/null; then
        sleep 0.5
        echo -n "  Puissance nouvelle:  "
        get_current_power
        print_success "Jetson configuré (index=$index, ~$dbm dBm)"
        return 0
    else
        print_error "Impossible de configurer (sudo requis)"
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
            echo "WireClaw TX Power Control - Base v${VERSION}"
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
        print_success "Puissance TX Base configurée (index=$1)"
        exit 0
    else
        print_error "Impossible de configurer la puissance TX"
        exit 1
    fi
}

main "$@"
