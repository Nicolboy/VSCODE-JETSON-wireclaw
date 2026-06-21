import asyncio
import json
import math
from google import genai
from google.genai import types
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from pymavlink import mavutil
from config import (GEMINI_API_KEY, GEMINI_MODEL,
                    MAVLINK_ADDRESS, MAVLINK_PYMAV,
                    BASE_LAT, BASE_LON)

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = f"""Tu es WireClaw, cerveau d'un drone d'inspection autonome.
Reponds UNIQUEMENT avec un objet JSON valide, sans markdown.
Le champ "message" est TOUJOURS en francais.
Format :
{{
  "action": "takeoff (decollage initial) | land (atterrissage) | rtl (retour base) | hover (maintien position) | goto (aller vers coordonnees) | orbit (tourner en rond) | status (etat general du drone) | position (position rapide lat/lon/alt) | altitude (changer la hauteur en vol) | position_gps (position GPS complete : horodatage HH:MM:SS + fix + satellites)",
  "altitude": <float metres AGL, defaut 15. Utilise pour altitude cible absolue : "monte a 10m" → 10>,
  "delta": <float|null. Variation relative : "monte de 5m" → 5, "descend de 3m" → -3, null sinon>,
  "lat": <float, pour goto>,
  "lon": <float, pour goto>,
  "radius": <float metres, pour orbit, defaut 10>,
  "speed": <float m/s, pour orbit, defaut 2>,
  "message": "<confirmation courte en francais>"
}}
Position de base : lat={BASE_LAT}, lon={BASE_LON}.
Pour goto, calcule des coordonnees proches (max 500m en test).
Nord = lat+, Sud = lat-, Est = lon+, Ouest = lon-
1 degre lat = 111320m, 1 degre lon = 111320*cos(lat)m
"""

ARDUCOPTER_MODES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO",
    4: "GUIDED", 5: "LOITER", 6: "RTL", 7: "CIRCLE",
    9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
    15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW",
    19: "AVOID_ADSB", 20: "GUIDED_NOGPS", 21: "SMART_RTL",
    22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG", 25: "SYSTEMID",
    26: "AUTOROTATE", 27: "AUTO_RTL"
}

drone_armed = False
current_pos = {"lat": BASE_LAT, "lon": BASE_LON, "alt": 0, "abs_alt": 584}
telemetry_cache = {"battery": -1.0, "mode": "UNKNOWN", "gps_fix": "UNKNOWN", "gps_sats": 0}
mav = None
drone = None

def init_pymavlink():
    global mav
    print("Connexion pymavlink sur 14552...")
    mav = mavutil.mavlink_connection(MAVLINK_PYMAV)
    # Envoie un heartbeat pour signaler l'adresse de retour à mavp2p (udps ne connaît pas le client tant qu'il n'a pas émis)
    mav.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
        0, 0, 0
    )
    print("Heartbeat GCS envoyé à mavp2p (enregistrement adresse retour)")
    mav.wait_heartbeat()
    print("pymavlink connecte !")

def send_orbit_cmd(radius, speed, lat, lon, abs_alt):
    mav.mav.command_long_send(
        mav.target_system, mav.target_component,
        34, 0, radius, speed, 0, float('nan'), lat, lon, abs_alt
    )

async def refresh_position(drone):
    async for pos in drone.telemetry.position():
        current_pos["lat"]     = pos.latitude_deg
        current_pos["lon"]     = pos.longitude_deg
        current_pos["alt"]     = pos.relative_altitude_m
        current_pos["abs_alt"] = pos.absolute_altitude_m
        break

async def monitor_armed(drone):
    global drone_armed
    async for is_armed in drone.telemetry.armed():
        drone_armed = is_armed
        await asyncio.sleep(0)  # cède le contrôle après chaque mise à jour

async def drain_pymavlink():
    """Draine pymavlink en boucle pour peupler mav.messages (dernier msg par type)."""
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, lambda: mav.recv_match(blocking=False))
        await asyncio.sleep(0.05)

async def monitor_telemetry(drone):
    """Sonde batterie, mode et GPS par polling — pause 1s entre chaque cycle
    pour ne pas saturer l'event loop asyncio avec des streams gRPC continus."""
    while True:
        try:
            async for bat in drone.telemetry.battery():
                raw = bat.remaining_percent
                telemetry_cache["battery"] = abs(raw) * 100 if abs(raw) <= 1 else abs(raw)
                break
            hb = mav.messages.get('HEARTBEAT')
            if hb:
                telemetry_cache["mode"] = ARDUCOPTER_MODES.get(hb.custom_mode, f"UNKNOWN({hb.custom_mode})")
            async for gps in drone.telemetry.gps_info():
                telemetry_cache["gps_fix"]  = str(gps.fix_type)
                telemetry_cache["gps_sats"] = gps.num_satellites
                break
        except Exception:
            pass
        await asyncio.sleep(1)

async def interpret_command(text):
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
        )
    )
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

async def execute_command(drone, cmd):
    global drone_armed
    action = cmd.get("action")
    alt    = float(cmd.get("altitude") or 15)
    msg    = cmd.get("message", "OK")

    if action == "position":
        await refresh_position(drone)
        return (f"\nGPS       : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f} m (AGL)\n"
                f"Alt abs   : {current_pos['abs_alt']:.1f} m (MSL)")

    elif action == "status":
        await refresh_position(drone)
        return (f"\nPosition : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f}m (AGL)\n"
                f"Abs alt   : {current_pos['abs_alt']:.1f}m\n"
                f"Batterie  : {telemetry_cache['battery']:.0f}%\n"
                f"Mode      : {telemetry_cache['mode']}\n"
                f"Arme      : {drone_armed}")

    elif action == "takeoff":
        await refresh_position(drone)
        delta = cmd.get("delta")
        target_alt = current_pos["alt"] + float(delta) if delta is not None else alt
        if current_pos["alt"] >= 2.0:
            new_abs_alt = current_pos["abs_alt"] - current_pos["alt"] + target_alt
            await drone.action.goto_location(
                current_pos["lat"], current_pos["lon"], new_abs_alt, float('nan')
            )
            await asyncio.sleep(6)
            await refresh_position(drone)
            return f"OK {msg} — altitude {current_pos['alt']:.1f}m"
        await drone.action.set_takeoff_altitude(target_alt)
        await drone.action.arm()
        await drone.action.takeoff()
        await asyncio.sleep(8)
        await refresh_position(drone)
        return f"OK {msg} — altitude {current_pos['alt']:.1f}m"

    elif action == "goto":
        if current_pos["alt"] < 2.0:
            await drone.action.set_takeoff_altitude(alt)
            await drone.action.arm()
            await drone.action.takeoff()
            await asyncio.sleep(8)
        lat = float(cmd.get("lat", current_pos["lat"]))
        lon = float(cmd.get("lon", current_pos["lon"]))
        items = [MissionItem(
            lat, lon, alt, 5, True,
            float('nan'), float('nan'),
            MissionItem.CameraAction.NONE,
            float('nan'), float('nan'),
            float('nan'), float('nan'),
            float('nan'),
            MissionItem.VehicleAction.NONE
        )]
        await drone.mission.upload_mission(MissionPlan(items))
        await drone.mission.start_mission()
        return f"OK {msg} → ({lat:.5f}, {lon:.5f}) alt {alt}m"

    elif action == "orbit":
        if current_pos["alt"] < 2.0:
            await drone.action.set_takeoff_altitude(alt)
            await drone.action.arm()
            await drone.action.takeoff()
            await asyncio.sleep(8)
            await refresh_position(drone)
        await refresh_position(drone)
        radius = float(cmd.get("radius", 10))
        speed  = float(cmd.get("speed", 2))
        send_orbit_cmd(radius, speed,
                       current_pos["lat"],
                       current_pos["lon"],
                       current_pos["abs_alt"])
        ack = mav.recv_match(type='COMMAND_ACK', blocking=True, timeout=2)
        if ack is None or ack.result != 0:
            steps = 16
            items = []
            lat0 = current_pos["lat"]
            lon0 = current_pos["lon"]
            for i in range(steps + 1):
                angle = 2 * math.pi * i / steps
                dlat = (radius / 111320) * math.cos(angle)
                dlon = (radius / (111320 * math.cos(math.radians(lat0)))) * math.sin(angle)
                items.append(MissionItem(
                    lat0 + dlat, lon0 + dlon, alt, speed, True,
                    float('nan'), float('nan'),
                    MissionItem.CameraAction.NONE,
                    float('nan'), float('nan'),
                    float('nan'), float('nan'),
                    float('nan'),
                    MissionItem.VehicleAction.NONE
                ))
            await drone.mission.upload_mission(MissionPlan(items))
            await drone.mission.start_mission()
            return (f"OK {msg} — orbite waypoints {radius}m "
                    f"a {speed}m/s ({steps} points)\n"
                    f"Centre : ({lat0:.6f}, {lon0:.6f})")
        return f"OK {msg} — orbite native {radius}m a {speed}m/s"

    elif action == "altitude":
        await refresh_position(drone)
        delta = cmd.get("delta")
        target_alt = current_pos["alt"] + float(delta) if delta is not None else alt
        if current_pos["alt"] < 2.0:
            await drone.action.set_takeoff_altitude(target_alt)
            await drone.action.arm()
            await drone.action.takeoff()
            await asyncio.sleep(8)
            await refresh_position(drone)
            return f"OK {msg} — altitude {current_pos['alt']:.1f}m"
        new_abs_alt = current_pos["abs_alt"] - current_pos["alt"] + target_alt
        await drone.action.goto_location(
            current_pos["lat"], current_pos["lon"], new_abs_alt, float('nan')
        )
        await asyncio.sleep(6)
        await refresh_position(drone)
        return f"OK {msg} — altitude {current_pos['alt']:.1f}m (cible {target_alt:.1f}m)"

    elif action == "position_gps":
        import datetime
        await refresh_position(drone)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        return (f"\nHorodatage : {ts}\n"
                f"Latitude   : {current_pos['lat']:.7f}\n"
                f"Longitude  : {current_pos['lon']:.7f}\n"
                f"Alt AGL    : {current_pos['alt']:.2f} m\n"
                f"Alt MSL    : {current_pos['abs_alt']:.2f} m\n"
                f"Fix GPS    : {telemetry_cache['gps_fix']}\n"
                f"Satellites : {telemetry_cache['gps_sats']}")

    elif action == "hover":
        await drone.action.hold()
        return "OK " + msg

    elif action == "land":
        await drone.action.land()
        return "OK " + msg

    elif action == "rtl":
        await drone.action.return_to_launch()
        return "OK " + msg

    return "Action inconnue : " + action

async def init():
    """Initialise pymavlink, MAVSDK et démarre les tâches de fond de télémétrie."""
    global drone, drone_armed
    # init_pymavlink est synchrone et bloquant — run_in_executor évite de geler l'event loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, init_pymavlink)
    drone = System()
    await drone.connect(system_address=MAVLINK_ADDRESS)
    print("Connexion MAVSDK...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            print("SITL connecte !")
            async for is_armed in drone.telemetry.armed():
                drone_armed = is_armed
                print(f"Etat initial : {'arme' if drone_armed else 'desarme'}")
                break
            asyncio.create_task(drain_pymavlink())
            asyncio.create_task(monitor_armed(drone))
            asyncio.create_task(monitor_telemetry(drone))
            break

async def main():
    await init()
    print("\nWireClaw pret")
    print("Commandes : takeoff, altitude, goto, orbit, status, position, position_gps, hover, rtl, land\n")

    while True:
        try:
            text = input(">>> ").strip()
            if text.lower() in ("quit", "exit", "q"):
                break
            if not text:
                continue
            cmd = await interpret_command(text)
            print(json.dumps(cmd, ensure_ascii=False))
            result = await execute_command(drone, cmd)
            print(result + "\n")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Erreur : {e}\n")

if __name__ == "__main__":
    asyncio.run(main())