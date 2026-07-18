# WireClaw — Drone d'Inspection Thermique Autonome

## Contexte du projet
WireClaw est un système de pilotage de drone en langage naturel.
L'opérateur donne des instructions en français (texte ou voix),
un LLM les interprète et génère des commandes MAVLink vers ArduPilot.

## Architecture
- **Jetson Orin Nano 8Go** : station sol, héberge WireClaw + SITL
- **ESP32 DevKitV4** : relais WiFi↔UART (firmware PlatformIO sur Windows)
- **Pi Zero 2W** : IP 192.168.1.132 — flux vidéo H.264 UDP → Jetson (câblé, arrêté pour l'instant)
- **Matek H743** : à venir — contrôleur de vol ArduPilot réel

## Stack logicielle
- Python 3.10, asyncio
- MAVSDK Python (udp://:14551)
- pymavlink (udpin:0.0.0.0:14552)
- Gemini 2.0 Flash API (clé dans .env, jamais en dur)
- MAVProxy en daemon (ports TCP:5760, UDP:14550/14551/14552)
- ArduPilot SITL (binaire ARM64 ~/ardupilot/build/sitl/bin/arducopter)
- PyTorch 2.8.0 + CUDA 12.6 (wheel Jetson : pypi.jetson-ai-lab.io/jp6/cu126, GPU Orin)
- ultralytics 8.4.62 (YOLOv8), numpy 1.26.4 (≥2 incompatible avec torch 2.8), cv2 4.5.4 système

## Fichiers principaux
- `wireclaw_core.py` : cerveau principal, boucle de commande
- `config.py` : configuration (lit .env via python-dotenv)
- `.env` : secrets — NE JAMAIS MODIFIER NI COMMITTER
- `start.sh` : lance ArduCopter + MAVProxy + WireClaw automatiquement
- `sitl_params.parm` : paramètres SITL (batterie désactivée)
- `receive_cam.py` : réception H.264 UDP port 5600 → inférence YOLOv8n → MJPEG annoté port 5601

## Ports réseau
- 5760 TCP : ArduCopter ↔ MAVProxy
- 14550 UDP : MAVProxy output standard + ESP32 source
- 14551 UDP : MAVSDK (WireClaw) + heartbeats ESP32
- 14552 UDP : pymavlink (WireClaw)
- 5600 UDP : flux H.264 RTP entrant depuis Pi Zero (caméra OV5647) — ouvert via `ufw allow 5600/udp` (2026-07-18, précaution). Fonctionne aujourd'hui via un relais loopback (`wfb-ng`/`wfb_rx` réinjecte le flux décodé sur `127.0.0.1:5600`), qui contourne de fait le filtrage sur l'interface externe : dépendance fragile, à surveiller si le pipeline de réception change (ex. flux direct depuis `gs-wfb` sans relais loopback)
- 5601 TCP : MJPEG annoté YOLOv8 exposé par receive_cam.py — ouvert via `ufw allow` (règle `5601/tcp`)
- 5602 UDP : sideband stats Pi Zero (résolution, fps, temp, CPU) vers receive_cam.py, via tunnel WFB-NG (`10.5.0.2` → `10.5.0.1` sur l'interface `gs-wfb`) — ouvert via `ufw allow 5602/udp` (2026-07-18). **Ce port était resté fermé par défaut (policy DROP d'ufw) alors que le code sideband existait depuis un moment** : l'overlay stats semblait "figé" ou absent car les paquets réels du Pi Zero étaient droppés par le pare-feu avant d'atteindre le socket applicatif, malgré un socket/threading fonctionnels par ailleurs (`tcpdump` voit toujours le trafic sur l'interface, quel que soit son sort côté netfilter — ne pas se fier à `tcpdump` seul pour valider qu'un flux atteint l'appli)

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

# Lancer la réception caméra Pi Zero (quand Pi Zero actif)
python3 ~/wireclaw/receive_cam.py

# Vérifier MJPEG stream caméra
curl -s --max-time 2 http://localhost:5601/ | head -1

# Pipeline GStreamer brute (debug)
gst-launch-1.0 udpsrc port=5600 ! application/x-rtp,payload=96 ! rtph264depay ! avdec_h264 ! videoconvert ! fakesink

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
1. ~~Réception Pi Zero 2W + caméra OV5647~~ ✅ Pi Zero IP 192.168.1.132, reçu et testé
2. Câblage UART ESP32 ↔ Pi Zero (GPIO17→GPIO15, GPIO16←GPIO14, GND)
3. ~~Préparation réception flux H.264 UDP Jetson~~ ✅ receive_cam.py opérationnel, MJPEG 5601 accessible
4. ~~YOLOv8 intégré dans receive_cam.py~~ ✅ yolov8n.pt, CUDA Orin, mode synthétique actif en attendant Pi Zero
5. **Prochaine étape : Pi Zero émet GStreamer UDP H.264 vers 192.168.1.84:5600**
6. Intégrer MJPEG 5601 dans l'interface web (flux vidéo + commandes)
7. Matek H743 + ArduPilot réel (remplace SITL)
8. SkyDroid C12 (caméra thermique + gimbal 3 axes, 117g)
9. Whisper local (transcription vocale)

## Gitea
- WireClaw Python : git.syoul.fr/nicoboy/wireclaw
- Firmware ESP32 : git.syoul.fr/nicoboy/wireclaw-esp32
