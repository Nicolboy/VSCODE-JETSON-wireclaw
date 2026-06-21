from dotenv import load_dotenv
import os

load_dotenv()

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL     = "gemini-2.5-flash"
MAVLINK_ADDRESS  = "udp://127.0.0.1:14551"
MAVLINK_PYMAV    = "udpout:127.0.0.1:14552"
NATS_SERVER      = "nats://localhost:4222"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_LAT         = -35.363262
BASE_LON         = 149.165237
