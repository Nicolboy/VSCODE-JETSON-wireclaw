# WireClaw — Drone d'Inspection Thermique Autonome

## Contexte du projet
WireClaw est un système de pilotage de drone en langage naturel.
L'opérateur donne des instructions en français (texte ou voix),
un LLM les interprète et génère des commandes MAVLink vers ArduPilot.

## Architecture
- **Jetson Orin Nano 8Go** : station sol, héberge WireClaw + SITL
- **ESP32 DevKitV4** : relais WiFi↔UART (firmware PlatformIO sur Windows)
- **Pi Zero 2W** : à venir — flux vidéo caméra + relais série vers Matek H743
- **Matek H743** : à venir — contrôleur de vol ArduPilot réel

## Stack logicielle
- Python 3.10, asyncio
- MAVSDK Python (udp://:14551)
- pymavlink (udpin:0.0.0.0:14552)
- Gemini 2.0 Flash API (clé dans .env, jamais en dur)
- MAVProxy en daemon (ports TCP:5760, UDP:14550/14551/14552)
- ArduPilot SITL (binaire ARM64 ~/ardupilot/build/sitl/bin/arducopter)

## Fichiers principaux
- `wireclaw_core.py` : cerveau principal, boucle de commande
- `config.py` : configuration (lit .env via python-dotenv)
- `.env` : secrets — NE JAMAIS MODIFIER NI COMMITTER
- `start.sh` : lance ArduCopter + MAVProxy + WireClaw automatiquement
- `sitl_params.parm` : paramètres SITL (batterie désactivée)

## Ports réseau
- 5760 TCP : ArduCopter ↔ MAVProxy
- 14550 UDP : MAVProxy output standard + ESP32 source
- 14551 UDP : MAVSDK (WireClaw) + heartbeats ESP32
- 14552 UDP : pymavlink (WireClaw)

## Réseau local
- Jetson Ethernet : 192.168.1.84
- Jetson WiFi : 192.168.1.143 (utilisé pour ESP32)
- ESP32 WiFi : 192.168.1.122

## Règles importantes
- Ne jamais modifier `.env` ni `credentials.h` (secrets WiFi/API)
- Toujours tester via `~/wireclaw/start.sh` avant de commiter
- Les commandes MAVLink passent toutes par `execute_command()` dans wireclaw_core.py
- L'état armement est synchronisé en temps réel via `monitor_armed()` (tâche de fond)
- `MAV_CMD_DO_ORBIT` (commande 34) non supportée en SITL → fallback waypoints circulaires 16 points
- La batterie SITL est désactivée (BATT_MONITOR=0) pour éviter les failsafes en simulation

## Commandes de session
```bash
# Démarrer tout
~/wireclaw/start.sh

# Vérifier SITL actif
ps aux | grep -E "arducopter|mavproxy" | grep -v grep

# Vérifier WiFi Jetson actif
nmcli dev status | grep wifi

# Tuer Hermes si actif (occupe port 14550)
pkill -f hermes

# Git commit + push
git add . && git commit -m "message" && git push origin master
```

## Commandes WireClaw validées
- `takeoff Xm` → décollage altitude X mètres
- `land` / `atterris` → atterrissage
- `rtl` / `rentre` → retour à la base
- `hover` → maintien de position
- `goto` → navigation vers coordonnées (calcul depuis description naturelle)
- `status` → position, altitude, batterie, mode, armement
- `orbit` → orbite native (fallback waypoints en SITL)

## Prochaines étapes
1. Réception Pi Zero 2W + caméra OV5647 (en attente livraison)
2. Câblage UART ESP32 ↔ Pi Zero (GPIO17→GPIO15, GPIO16←GPIO14, GND)
3. Flux vidéo GStreamer CSI → UDP → Jetson
4. Interface web FastAPI + HTML (flux vidéo + commandes)
5. Matek H743 + ArduPilot réel (remplace SITL)
6. SkyDroid C12 (caméra thermique + gimbal 3 axes, 117g)
7. YOLOv8 détection anomalies thermiques
8. Whisper local (transcription vocale)

## Gitea
- WireClaw Python : git.syoul.fr/nicoboy/wireclaw
- Firmware ESP32 : git.syoul.fr/nicoboy/wireclaw-esp32
