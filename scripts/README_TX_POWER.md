# WireClaw TX Power Control Scripts

## Vue d'ensemble

Deux scripts bash pour contrôler la puissance d'émission (TX) du système WireClaw :

- **`set_TX_power_base.sh`** — Base (Jetson)
- **`set_TX_power_drone.sh`** — Drone (Pi Zero)

## Utilisation

### Base (Jetson)

```bash
./set_TX_power_base.sh <index>
```

**Exemples :**
```bash
./set_TX_power_base.sh 10    # Puissance faible (~3 dBm)
./set_TX_power_base.sh 31    # Puissance moyenne (~10 dBm)
./set_TX_power_base.sh 52    # Puissance haute (~20 dBm)
./set_TX_power_base.sh 63    # Puissance maximale (~23 dBm)
```

**Interface :** `wlx54c9ff02f26e`  
**Méthode :** `iw dev ... set txpower fixed`  
**Requis :** `sudo`

---

### Drone (Pi Zero)

```bash
./set_TX_power_drone.sh <index>
```

**Exemples :**
```bash
./set_TX_power_drone.sh 10    # Puissance faible (~3 dBm)
./set_TX_power_drone.sh 31    # Puissance moyenne (~10 dBm)
./set_TX_power_drone.sh 52    # Puissance haute (~20 dBm)
./set_TX_power_drone.sh 63    # Puissance maximale (~23 dBm)
```

**Interface :** `wlan-drone` (via SSH)  
**Méthode :** `modprobe -r rtl88xxau_wfb && modprobe rtl88xxau_wfb rtw_tx_pwr_idx_override=<index>`  
**Requis :** SSH alias `jetson-pi` + `sudo` sur Pi Zero

---

## Table de conversion Index → dBm

| Index | dBm approximatif |
|-------|------------------|
| 0     | 0 dBm            |
| 10    | ~3 dBm           |
| 20    | ~6 dBm           |
| 31    | ~10 dBm          |
| 42    | ~15 dBm          |
| 52    | ~20 dBm          |
| 63    | ~23 dBm (max)    |

## Prérequis

### Jetson (Base)
- Commande `iw` disponible (`/usr/sbin/iw`)
- Sudo sans mot de passe configuré
- Interface `wlx54c9ff02f26e` active

### Pi Zero (Drone)
- SSH alias `jetson-pi` fonctionnel dans `~/.ssh/config`
- Session sudo "chaude" lancée sur Pi Zero
- Module driver `rtl88xxau_wfb` chargé
- Interface `wlan-drone` active

## Configuration de la session sudo "chaude"

Sur Pi Zero, au démarrage de session :
```bash
sudo -v
```

Cela cache le mot de passe pendant 15 minutes (~5 min après dernière commande sudo).

## Aide

```bash
./set_TX_power_base.sh --help
./set_TX_power_drone.sh --help
```

## Version

- **Base script :** 1.0.0
- **Drone script :** 1.0.0

## Auteur

WireClaw TX Power Control System

## Notes

- La conversion Index → dBm est approximative et dépend du chipset/driver
- Modifier la puissance affecte immédiatement le trafic en cours (peut déconnecter)
- Les limites régionales (FCC/ETSI) peuvent être appliquées par le driver
- Le Pi Zero demande un rechargement du module (interruption de service ~2s)

---

## Exemples d'utilisation

### Configuration complète (Base + Drone)

```bash
# Tous deux à puissance moyenne (~10 dBm)
./set_TX_power_base.sh 31
./set_TX_power_drone.sh 31
```

### Configuration asymétrique

```bash
# Base en haute puissance, Drone en faible puissance
./set_TX_power_base.sh 52
./set_TX_power_drone.sh 10
```

### Reset (minimum)

```bash
./set_TX_power_base.sh 0
./set_TX_power_drone.sh 0
```

---

## Troubleshooting

### Erreur : "sudo requis"
Sur Jetson : lancez `sudo -v` pour activer une session sudo "chaude"

### Erreur : "SSH non disponible"
Vérifiez que l'alias `jetson-pi` est configuré dans `~/.ssh/config`

### Erreur : "Module not found"
Vérifiez que le driver `rtl88xxau_wfb` est compilé et chargé sur Pi Zero

### Changement de puissance non visible
Vérifiez avec : `iw dev <iface> info | grep txpower`

