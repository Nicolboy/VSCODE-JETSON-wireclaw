from dotenv import load_dotenv
import os

load_dotenv()

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL     = "gemini-2.5-flash"
MAVLINK_ADDRESS  = "udp://:14551"
MAVLINK_PYMAV    = "udpin:0.0.0.0:14552"
NATS_SERVER      = "nats://localhost:4222"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_LAT         = -35.363262
BASE_LON         = 149.165237
