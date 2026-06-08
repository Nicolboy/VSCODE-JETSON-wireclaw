#!/usr/bin/env python3
"""
Réception flux H.264 UDP depuis Pi Zero (port 5600) via GStreamer.
Exposition MJPEG sur port 5601 pour consommation par YOLOv8 et l'interface web.
"""

import cv2
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Pipeline GStreamer : UDP RTP H.264 → frames décodées
GSTREAMER_PIPELINE = (
    "udpsrc port=5600 "
    "! application/x-rtp,payload=96 "
    "! rtph264depay "
    "! avdec_h264 "
    "! videoconvert "
    "! video/x-raw,format=BGR "
    "! appsink drop=true max-buffers=1"
)

MJPEG_PORT = 5601
MJPEG_QUALITY = 70  # Compromis qualité/débit pour le réseau local

# Frame partagée entre le thread GStreamer et le serveur MJPEG
_frame_lock = threading.Lock()
_current_frame = None
_running = True


def capture_loop():
    """Lit les frames depuis GStreamer et met à jour _current_frame."""
    global _current_frame, _running

    cap = cv2.VideoCapture(GSTREAMER_PIPELINE, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        print("[CAM] Impossible d'ouvrir le pipeline GStreamer — Pi Zero connecté ?")
        _running = False
        return

    print("[CAM] Pipeline GStreamer ouvert, réception flux...")
    while _running:
        ret, frame = cap.read()
        if not ret:
            print("[CAM] Flux perdu, nouvelle tentative...")
            time.sleep(1)
            continue
        with _frame_lock:
            _current_frame = frame

    cap.release()
    print("[CAM] Pipeline fermé.")


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

            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY])
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

            time.sleep(1 / 30)  # ~30 fps max


def main():
    global _running

    # Thread de capture GStreamer
    t = threading.Thread(target=capture_loop, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", MJPEG_PORT), MJPEGHandler)
    print(f"[CAM] MJPEG stream sur http://0.0.0.0:{MJPEG_PORT}/")

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
