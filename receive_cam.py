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
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# Dernières stats Pi Zero reçues via UDP 5602 (thread-safe)
_stats_lock = threading.Lock()
_pi_stats: dict | None = None


def sideband_loop() -> None:
    """Écoute UDP 5602, stocke le dernier JSON Pi Zero dans _pi_stats."""
    global _pi_stats
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


def _probe_gstreamer(result: list) -> None:
    """Thread : tente une lecture GStreamer et stocke le cap si succès."""
    cap = cv2.VideoCapture(GSTREAMER_PIPELINE, cv2.CAP_GSTREAMER)
    if cap.isOpened():
        ret, _ = cap.read()   # bloque jusqu'à la première frame
        if ret:
            result.append(cap)
            return
        cap.release()


def _make_waiting_frame() -> np.ndarray:
    """Génère un frame noir 640x480 avec message d'attente."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(frame, "En attente flux Pi Zero...", (50, 220),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.putText(frame, "UDP 5600 non disponible", (50, 260),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 255), 2)
    return frame


def open_capture() -> tuple[cv2.VideoCapture | None, str]:
    """
    Priorité de source :
    1. Flux GStreamer UDP 5600 (Pi Zero)
    2. Frame synthétique (pas de hardware)
    """
    print("[CAM] Sonde flux GStreamer UDP 5600 (timeout 15 s)...")
    result: list = []
    t = threading.Thread(target=_probe_gstreamer, args=(result,), daemon=True)
    t.start()
    t.join(timeout=15)

    if result:
        print("[CAM] Flux GStreamer UDP reçu.")
        return result[0], "gstreamer"

    print("[CAM] Flux UDP indisponible — mode synthétique (attente Pi Zero)")
    return None, "synthetic"


def capture_loop(model: YOLO) -> None:
    """Lit les frames, passe dans YOLOv8, stocke le résultat annoté."""
    global _current_frame, _running

    cap, source = open_capture()
    print(f"[CAM] Source active : {source}")

    # Frame synthétique réutilisée en boucle (pas d'inférence : frame vide)
    synthetic_frame = _make_waiting_frame() if source == "synthetic" else None

    while _running:
        if source == "synthetic":
            frame = synthetic_frame
            time.sleep(1 / 10)  # 10 fps synthétiques
        else:
            ret, frame = cap.read()
            if not ret:
                if source == "gstreamer":
                    print("[CAM] Flux GStreamer perdu, nouvelle tentative...")
                    time.sleep(1)
                    continue
                print("[CAM] Webcam perdue, arrêt.")
                _running = False
                break

        # Inférence YOLOv8 — skip en mode synthétique (frame vide sans objet)
        if model is not None and source != "synthetic":
            results = model(frame, verbose=False)
            annotated = results[0].plot()
        else:
            annotated = frame

        # Overlay stats Pi Zero (ne modifie pas annotated en place)
        annotated = draw_overlay(annotated)

        with _frame_lock:
            _current_frame = annotated

    if cap is not None:
        cap.release()
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

    # Thread de capture + inférence
    t = threading.Thread(target=capture_loop, args=(model,), daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", MJPEG_PORT), MJPEGHandler)
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
