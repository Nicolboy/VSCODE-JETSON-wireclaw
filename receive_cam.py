#!/usr/bin/env python3
"""
Réception flux H.264 UDP depuis Pi Zero (port 5600) via GStreamer.
Inférence YOLOv8n sur chaque frame, exposition MJPEG annoté sur port 5601.
Thread sideband UDP 5602 : reçoit JSON Pi Zero (résolution, fps, temp, CPU...)
et affiche un overlay semi-transparent sur chaque frame.

Fallback automatique vers webcam locale (cv2.VideoCapture(0)) si le flux
UDP n'est pas disponible, pour tester sans le Pi Zero.
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import cv2
from ultralytics import YOLO

MJPEG_PORT    = 5601
SIDEBAND_PORT = 5602
MJPEG_QUALITY = 70
MODEL_PATH    = Path(__file__).parent / "yolov8n.pt"

# Pipeline GStreamer : RTP H.264 UDP → BGR frames
# Deux udpsrc (IPv4 0.0.0.0 + IPv6 ::) fusionnés par funnel : un socket IPv6 "::"
# seul ne reçoit pas les paquets IPv4 sur ce Jetson (IPV6_V6ONLY actif, pas de
# dual-stack), donc on écoute les deux familles en parallèle sur le même port.
# IPv4 : réseau local Freebox (dev) — IPv6 : Mycelium (prod, 4G).
GSTREAMER_PIPELINE = (
    "funnel name=f "
    "! application/x-rtp,payload=96 "
    "! rtph264depay "
    "! avdec_h264 "
    "! videoconvert "
    "! video/x-raw,format=BGR "
    "! appsink drop=true max-buffers=1 "
    "udpsrc address=0.0.0.0 port=5600 ! f. "
    "udpsrc address=:: port=5600 ! f."
)

# Frame annotée partagée entre le thread de capture et le serveur MJPEG
_frame_lock    = threading.Lock()
_current_frame = None
_running       = True

# Source active partagée entre le watcher GStreamer et la boucle de capture :
# "synthetic" par défaut, bascule vers "gstreamer" dès qu'une frame réelle arrive.
_source_lock    = threading.Lock()
_active_cap: cv2.VideoCapture | None = None
_active_source  = "synthetic"

# Dernières stats Pi Zero reçues via UDP 5602 (thread-safe)
_stats_lock = threading.Lock()
_pi_stats: dict | None = None

# RSSI du flux vidéo dédié WFB-NG reçu via API JSON 127.0.0.1:8103
_rssi_lock = threading.Lock()
_rssi_data: dict | None = None  # {"rssi_avg": -63, "snr_avg": 17}
RSSI_API_PORT = 8103


def sideband_loop() -> None:
    """Écoute UDP 5602, stocke le dernier JSON Pi Zero dans _pi_stats."""
    global _pi_stats
    # Liaison point à point exclusivement IPv4 (tunnel WFB-NG) — plus de
    # dual-stack IPv6/Mycelium, qui ne recevait pas fiablement le trafic
    # provenant de l'interface tunnel point-à-point.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind(("0.0.0.0", SIDEBAND_PORT))
    print(f"[SIDEBAND] Écoute UDP sur port {SIDEBAND_PORT}")
    while _running:
        try:
            data, _ = sock.recvfrom(4096)
            payload = json.loads(data.decode("utf-8"))
            with _stats_lock:
                _pi_stats = payload
        except socket.timeout:
            continue
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[SIDEBAND] JSON invalide : {e}")
    sock.close()
    print("[SIDEBAND] Arrêt.")


def rssi_loop() -> None:
    """Écoute l'API JSON wfb-ng locale (127.0.0.1:8103), extrait le RSSI
    du flux vidéo dédié (id="video rx") et le stocke pour l'overlay.
    Reconnexion automatique en cas de coupure (wfb-server-gs redémarré)."""
    global _rssi_data
    buf = ""
    while _running:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", RSSI_API_PORT))
            print(f"[RSSI] Connecté à l'API wfb-ng (port {RSSI_API_PORT})")
            while _running:
                chunk = sock.recv(8192).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("id") == "video rx":
                        ants = d.get("rx_ant_stats") or []
                        if ants:
                            rssi_avg = sum(a.get("rssi_avg", -80) for a in ants) / len(ants)
                            snr_avg = sum(a.get("snr_avg", 0) for a in ants) / len(ants)
                            with _rssi_lock:
                                _rssi_data = {"rssi_avg": rssi_avg, "snr_avg": snr_avg}
            sock.close()
        except (ConnectionRefusedError, OSError, socket.timeout):
            with _rssi_lock:
                _rssi_data = None
            time.sleep(2)


def draw_rssi_pyramid(frame: np.ndarray) -> np.ndarray:
    """Pyramide inversée (base large en haut, pointe en bas), 10 segments,
    remplissage proportionnel au RSSI, code couleur vert/jaune/rouge.
    Positionnée en haut à droite du frame."""
    with _rssi_lock:
        rssi = _rssi_data

    h, w = frame.shape[:2]
    N = 6
    pyr_w = 70
    pyr_h = 54
    gap = 2
    margin_top = 6
    margin_right = 10
    x_center = w - margin_right - pyr_w // 2
    y_top = margin_top

    out = frame

    if rssi is None:
        fill_count = 0
        color = (128, 128, 128)
    else:
        rssi_val = rssi["rssi_avg"]
        fill_ratio = max(0.0, min(1.0, (rssi_val + 75) / 25))
        fill_count = round(fill_ratio * N)
        if rssi_val >= -60:
            color = (0, 200, 0)
        elif rssi_val >= -70:
            color = (0, 220, 220)
        else:
            color = (0, 0, 220)

    row_h = (pyr_h - gap * (N - 1)) / N

    for i in range(N):
        y0 = y_top + i * (row_h + gap)
        y1 = y0 + row_h
        top_frac = 1 - i / N
        bot_frac = 1 - (i + 1) / N
        top_half_w = (pyr_w / 2) * top_frac
        bot_half_w = (pyr_w / 2) * bot_frac

        pts = np.array([
            [x_center - top_half_w, y0],
            [x_center + top_half_w, y0],
            [x_center + bot_half_w, y1],
            [x_center - bot_half_w, y1],
        ], dtype=np.int32)

        filled = i < fill_count
        seg_color = color if filled else (60, 60, 60)
        cv2.fillConvexPoly(out, pts, seg_color, cv2.LINE_AA)
        cv2.polylines(out, [pts], True, (20, 20, 20), 1, cv2.LINE_AA)

    if rssi is not None:
        label = f"{rssi['rssi_avg']:.0f}dBm"
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), _ = cv2.getTextSize(label, font, 0.45, 1)
        tx = x_center - tw // 2
        ty = y_top + pyr_h + th + 4
        cv2.putText(out, label, (tx, ty), font, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

    return out


def draw_overlay(frame: np.ndarray) -> np.ndarray:
    """Dessine l'overlay stats Pi Zero (fond noir semi-transparent) sur le frame."""
    with _stats_lock:
        stats = _pi_stats
    if stats is None:
        return frame

    w_res   = stats.get("width", "?")
    h_res   = stats.get("height", "?")
    fps     = stats.get("fps", "?")
    enc     = stats.get("encoder", "?")
    temp    = stats.get("temp_c", "?")
    cpu     = stats.get("cpu_pct", "?")
    throttled = stats.get("throttled", False)
    throttle_str = "THROTTLED" if throttled else "ok"

    line1 = f"{w_res}x{h_res} | {fps}fps | {enc} | {temp}C"
    line2 = f"CPU {cpu}% | {throttle_str}"

    font      = cv2.FONT_HERSHEY_SIMPLEX
    scale     = 0.55
    thickness = 1
    margin    = 6

    (tw1, th1), _ = cv2.getTextSize(line1, font, scale, thickness)
    (tw2, th2), _ = cv2.getTextSize(line2, font, scale, thickness)
    box_w = max(tw1, tw2) + margin * 2
    box_h = th1 + th2 + margin * 3

    # Copie pour ne pas altérer le frame original (réutilisé en mode synthétique)
    out = frame.copy()
    bg  = out.copy()
    cv2.rectangle(bg, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(bg, 0.55, out, 0.45, 0, out)

    cv2.putText(out, line1, (margin, margin + th1),
                font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    cv2.putText(out, line2, (margin, margin * 2 + th1 + th2),
                font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

    return out


def gstreamer_watcher() -> None:
    """
    Thread de fond permanent : tant que la source active n'est pas "gstreamer",
    tente d'ouvrir le pipeline et de lire une première frame (le read() bloque
    naturellement jusqu'à ce que des paquets valides arrivent, ce qui remplace
    l'ancien timeout fixe). Bascule la source active dès qu'une frame arrive,
    y compris après un démarrage en mode synthétique ou une perte de flux.
    """
    global _active_cap, _active_source

    while _running:
        with _source_lock:
            already_connected = _active_source == "gstreamer"
        if already_connected:
            time.sleep(1)
            continue

        cap = cv2.VideoCapture(GSTREAMER_PIPELINE, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            cap.release()
            time.sleep(2)
            continue

        ret, _ = cap.read()  # bloque jusqu'à la première frame valide
        if not ret:
            cap.release()
            time.sleep(2)
            continue

        print("[CAM] Flux GStreamer UDP reçu.")
        with _source_lock:
            _active_cap    = cap
            _active_source = "gstreamer"


def _make_waiting_frame() -> np.ndarray:
    """Génère un frame noir 640x480 avec message d'attente."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(frame, "En attente flux Pi Zero...", (50, 220),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.putText(frame, "UDP 5600 non disponible", (50, 260),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 255), 2)
    return frame


def capture_loop(model: YOLO) -> None:
    """
    Lit les frames de la source active (gstreamer ou synthétique), passe dans
    YOLOv8, stocke le résultat annoté. La source est mise à jour en tâche de
    fond par gstreamer_watcher() : ce thread n'a pas besoin d'attendre le flux
    réel au démarrage, il bascule dessus dès qu'il devient disponible.
    """
    global _current_frame, _running, _active_cap, _active_source

    print("[CAM] Démarrage en mode synthétique — connexion GStreamer en tâche de fond.")
    synthetic_frame = _make_waiting_frame()

    while _running:
        with _source_lock:
            cap, source = _active_cap, _active_source

        if source == "synthetic":
            frame = synthetic_frame
            time.sleep(1 / 10)  # 10 fps synthétiques
        else:
            ret, frame = cap.read()
            if not ret:
                print("[CAM] Flux GStreamer perdu, retour en mode synthétique.")
                cap.release()
                with _source_lock:
                    _active_cap    = None
                    _active_source = "synthetic"
                continue

        # Inférence YOLOv8 — skip en mode synthétique (frame vide sans objet)
        if model is not None and source != "synthetic":
            results = model(frame, verbose=False)
            annotated = results[0].plot()
        else:
            annotated = frame

        # Overlay stats Pi Zero (ne modifie pas annotated en place)
        annotated = draw_overlay(annotated)

        # Overlay RSSI pyramide inversée
        annotated = draw_rssi_pyramid(annotated)

        with _frame_lock:
            _current_frame = annotated

    with _source_lock:
        if _active_cap is not None:
            _active_cap.release()
    print("[CAM] Capture terminée.")


class MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silencieux pour ne pas polluer les logs WireClaw

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        while _running:
            with _frame_lock:
                frame = _current_frame

            if frame is None:
                time.sleep(0.05)
                continue

            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY])
            if not ok:
                continue
            data = jpeg.tobytes()

            try:
                self.wfile.write(
                    f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                    + data
                    + b"\r\n"
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

            time.sleep(1 / 30)


def main():
    global _running

    parser = argparse.ArgumentParser(description="WireClaw — réception caméra + YOLOv8")
    parser.add_argument("--no-yolo", action="store_true", help="Désactiver l'inférence YOLOv8 (flux brut)")
    args = parser.parse_args()

    print(f"[CAM] Chargement modèle YOLOv8 : {MODEL_PATH}")
    model = YOLO(str(MODEL_PATH))

    if args.no_yolo:
        # Mode flux brut : remplace l'inférence par une copie directe
        model = None
        print("[CAM] Mode flux brut (YOLOv8 désactivé)")

    # Thread sideband : stats Pi Zero via UDP 5602
    ts = threading.Thread(target=sideband_loop, daemon=True)
    ts.start()

    # Thread RSSI : lecture API JSON wfb-ng pour l'overlay pyramide inversée
    tr = threading.Thread(target=rssi_loop, daemon=True)
    tr.start()

    # Thread de fond : connexion/reconnexion continue au flux GStreamer réel
    tg = threading.Thread(target=gstreamer_watcher, daemon=True)
    tg.start()

    # Thread de capture + inférence (démarre immédiatement en mode synthétique)
    t = threading.Thread(target=capture_loop, args=(model,), daemon=True)
    t.start()

    server = ThreadingHTTPServer(("0.0.0.0", MJPEG_PORT), MJPEGHandler)
    print(f"[CAM] MJPEG stream sur http://0.0.0.0:{MJPEG_PORT}/")
    print(f"[CAM] Ouvrir dans un navigateur ou VLC : http://192.168.1.84:{MJPEG_PORT}/")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _running = False
        server.shutdown()
        print("[CAM] Arrêt.")


if __name__ == "__main__":
    main()
