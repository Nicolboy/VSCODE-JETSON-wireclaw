#!/usr/bin/env python3
"""
Réception flux H.264 UDP depuis Pi Zero (port 5600) via GStreamer.
Inférence YOLOv8n sur chaque frame, exposition MJPEG annoté sur port 5601.

Fallback automatique vers webcam locale (cv2.VideoCapture(0)) si le flux
UDP n'est pas disponible, pour tester sans le Pi Zero.
"""

import argparse
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import cv2
from ultralytics import YOLO

MJPEG_PORT  = 5601
MJPEG_QUALITY = 70
MODEL_PATH  = Path(__file__).parent / "yolov8n.pt"

# Pipeline GStreamer : RTP H.264 UDP → BGR frames
GSTREAMER_PIPELINE = (
    "udpsrc port=5600 "
    "! application/x-rtp,payload=96 "
    "! rtph264depay "
    "! avdec_h264 "
    "! videoconvert "
    "! video/x-raw,format=BGR "
    "! appsink drop=true max-buffers=1"
)

# Frame annotée partagée entre le thread de capture et le serveur MJPEG
_frame_lock   = threading.Lock()
_current_frame = None
_running       = True


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
    print("[CAM] Sonde flux GStreamer UDP 5600 (timeout 4 s)...")
    result: list = []
    t = threading.Thread(target=_probe_gstreamer, args=(result,), daemon=True)
    t.start()
    t.join(timeout=4)

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
