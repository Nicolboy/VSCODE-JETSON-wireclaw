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
Format :
{{
  "action": "takeoff|land|rtl|hover|goto|orbit|status|position",
  "altitude": <float metres, defaut 15>,
  "lat": <float, pour goto>,
  "lon": <float, pour goto>,
  "radius": <float metres, pour orbit, defaut 10>,
  "speed": <float m/s, pour orbit, defaut 2>,
  "message": "<confirmation courte>"
}}
Position de base : lat={BASE_LAT}, lon={BASE_LON}.
Pour goto, calcule des coordonnees proches (max 500m en test).
Nord = lat+, Sud = lat-, Est = lon+, Ouest = lon-
1 degre lat = 111320m, 1 degre lon = 111320*cos(lat)m
"""

drone_armed = False
current_pos = {"lat": BASE_LAT, "lon": BASE_LON, "alt": 0, "abs_alt": 584}
mav = None
drone = None

def init_pymavlink():
    global mav
    print("Connexion pymavlink sur 14552...")
    mav = mavutil.mavlink_connection(MAVLINK_PYMAV)
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
    alt    = float(cmd.get("altitude", 15))
    msg    = cmd.get("message", "OK")

    if action == "position":
        await refresh_position(drone)
        return (f"\nGPS       : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f} m (AGL)\n"
                f"Alt abs   : {current_pos['abs_alt']:.1f} m (MSL)")

    elif action == "status":
        await refresh_position(drone)
        async for bat in drone.telemetry.battery():
            raw = bat.remaining_percent
            pct = abs(raw) * 100 if abs(raw) <= 1 else abs(raw)
            break
        async for fm in drone.telemetry.flight_mode():
            mode = str(fm)
            break
        return (f"\nPosition : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f}m (AGL)\n"
                f"Abs alt   : {current_pos['abs_alt']:.1f}m\n"
                f"Batterie  : {pct:.0f}%\n"
                f"Mode      : {mode}\n"
                f"Arme      : {drone_armed}")

    elif action == "takeoff":
        await refresh_position(drone)
        if current_pos["alt"] >= 2.0:
            # Déjà en vol : changer l'altitude en conservant la position
            new_abs_alt = current_pos["abs_alt"] - current_pos["alt"] + alt
            await drone.action.goto_location(
                current_pos["lat"], current_pos["lon"], new_abs_alt, float('nan')
            )
            await asyncio.sleep(6)
            await refresh_position(drone)
            return f"OK {msg} — altitude {current_pos['alt']:.1f}m"
        await drone.action.set_takeoff_altitude(alt)
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
    """Initialise pymavlink, MAVSDK et démarre monitor_armed en tâche de fond."""
    global drone, drone_armed
    init_pymavlink()
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
            asyncio.ensure_future(monitor_armed(drone))
            break

async def main():
    await init()
    print("\nWireClaw pret")
    print("Commandes : takeoff, goto, orbit, status, position, hover, rtl, land\n")

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