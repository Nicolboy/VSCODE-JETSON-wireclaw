import asyncio
import json
import math
import secrets
import time
import httpx
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from pymavlink import mavutil
from config import (OLLAMA_URL, LFM3_MODEL,
                    MAVLINK_ADDRESS, MAVLINK_PYMAV,
                    BASE_LAT, BASE_LON)

# Bornes de securite en dur : independantes de ce que le LLM produit, pour
# qu'une hallucination d'action/coordonnees/altitude ne puisse jamais partir
# directement vers MAVSDK (meme logique que le TMS320 cote ESP32 : le logiciel
# ne valide pas lui-meme, il n'execute que dans des bornes verifiees en dur).
MAX_ALTITUDE_M      = 50.0
GEOFENCE_RADIUS_M   = 500.0
MAX_ORBIT_RADIUS_M  = 100.0
CONFIRM_TTL_S       = 30

# Altitude par defaut quand aucune n'est precisee : 1m pour un premier envol
# seul ("decolle"/"decollage" sans valeur — tests au banc, pas de saut a
# 15m par defaut), 15m pour les autres usages (goto, orbit, changement
# d'altitude en vol).
DEFAULT_TAKEOFF_ALTITUDE_M = 1.0
DEFAULT_ALTITUDE_M         = 15.0

# Actions qui deplacent/arment/posent le drone : necessitent une confirmation
# explicite de l'operateur avant execution. Le reste (lecture d'etat) s'execute
# immediatement.
CRITICAL_ACTIONS = {"takeoff", "land", "rtl", "goto", "orbit", "altitude"}
KNOWN_ACTIONS = CRITICAL_ACTIONS | {"hover", "status", "position", "position_gps"}

SYSTEM_PROMPT = f"""Tu es WireClaw, cerveau d'un drone d'inspection autonome.
Reponds UNIQUEMENT avec un objet JSON valide, sans markdown.
Le champ "message" est TOUJOURS en francais.
Format :
{{
  "action": "takeoff | land | rtl | hover | goto | orbit | status | position | altitude | position_gps",
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
"decolle" / "decolle a Xm" (premier envol depuis le sol) -> action="takeoff" TOUJOURS, jamais "altitude".
"monte de X metres" ou "descend de X metres" (variation verticale en vol, pas un premier decollage) -> action="altitude", jamais "rtl", "goto" ni "takeoff".
"retour a la base" / "rentre" / "rtl" -> action="rtl" (vol retour puis atterrissage auto), distinct de "atterris"/"land" (pose immediate sur place).
"maintiens la position" / "tiens la position" / "reste la" -> action="hover" (arret sur place en vol), jamais "position" (qui ne fait qu'afficher la position actuelle, action differente).
Les nombres peuvent etre dictes en toutes lettres (ex. "dix" = 10, "cinq" = 5, "huit" = 8) : les convertir systematiquement en chiffre dans "altitude"/"delta"/"radius"/"speed", ne jamais les laisser a leur valeur par defaut quand un nombre a ete prononce.
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

async def monitor_position(drone):
    """Rafraichit lat/lon/alt en continu — sans cette tache, current_pos ne
    bouge que lorsqu'une commande (status/goto/...) appelle refresh_position()
    explicitement, et /status affiche alors une position figee entre deux
    commandes."""
    while True:
        try:
            await refresh_position(drone)
        except Exception:
            pass
        await asyncio.sleep(1)

async def monitor_telemetry(drone):
    """Sonde batterie, mode et GPS par polling — pause 1s entre chaque cycle
    pour ne pas saturer l'event loop asyncio avec des streams gRPC continus."""
    while True:
        try:
            async for bat in drone.telemetry.battery():
                raw = bat.remaining_percent
                telemetry_cache["battery"] = abs(raw) * 100 if abs(raw) <= 1 else abs(raw)
                break
            hb = None
            while True:
                msg = mav.recv_match(blocking=False)
                if msg is None:
                    break
                if msg.get_type() == 'HEARTBEAT':
                    hb = msg
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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LFM3_MODEL,
                "system": SYSTEM_PROMPT,
                "prompt": text,
                "think": False,
                "format": "json",
                "stream": False,
            },
        )
    data = r.json()
    return json.loads(data["response"])

def _distance_m(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111320
    dlon = (lon2 - lon1) * 111320 * math.cos(math.radians(lat1))
    return math.hypot(dlat, dlon)

def _target_altitude(cmd, default_alt=DEFAULT_ALTITUDE_M):
    """Calcule l'altitude cible pour takeoff/altitude — seule source de
    verite, utilisee a la fois par validate_command() et execute_command()
    pour qu'elles ne puissent jamais diverger.
    Au sol (premier envol), delta n'a pas de sens : rien n'a encore decolle
    pour lui servir de reference. LFM3 met souvent delta=0 (au lieu de null)
    meme sans variation demandee, ce qui donnait une cible ≈0m — on l'ignore
    donc totalement au sol, seule l'altitude absolue compte (defaut selon
    default_alt — 1m pour un takeoff seul, 15m sinon).
    En vol, delta s'applique normalement comme variation relative."""
    alt = float(cmd.get("altitude") or default_alt)
    if current_pos["alt"] < 2.0:
        return alt
    delta = cmd.get("delta")
    return current_pos["alt"] + float(delta) if delta is not None else alt

def validate_command(cmd):
    """Verifie les bornes de securite en dur. Leve ValueError pour rejeter
    la commande en entier — jamais d'execution partielle sur une commande
    suspecte."""
    action = cmd.get("action")
    if action not in KNOWN_ACTIONS:
        raise ValueError(f"action inconnue : {action!r}")

    if action in ("takeoff", "altitude"):
        default_alt = DEFAULT_TAKEOFF_ALTITUDE_M if action == "takeoff" else DEFAULT_ALTITUDE_M
        target = _target_altitude(cmd, default_alt)
        if not (0.0 <= target <= MAX_ALTITUDE_M):
            raise ValueError(
                f"altitude cible {target:.1f}m hors limite [0, {MAX_ALTITUDE_M:.0f}]m"
            )

    elif action == "goto":
        lat, lon = cmd.get("lat"), cmd.get("lon")
        if lat is None or lon is None:
            raise ValueError("goto sans coordonnees")
        dist = _distance_m(BASE_LAT, BASE_LON, float(lat), float(lon))
        if dist > GEOFENCE_RADIUS_M:
            raise ValueError(
                f"cible a {dist:.0f}m de la base, hors geofence ({GEOFENCE_RADIUS_M:.0f}m)"
            )
        alt = float(cmd.get("altitude") or DEFAULT_ALTITUDE_M)
        if not (0.0 <= alt <= MAX_ALTITUDE_M):
            raise ValueError(f"altitude {alt:.1f}m hors limite [0, {MAX_ALTITUDE_M:.0f}]m")

    elif action == "orbit":
        dist = _distance_m(BASE_LAT, BASE_LON, current_pos["lat"], current_pos["lon"])
        if dist > GEOFENCE_RADIUS_M:
            raise ValueError(
                f"position actuelle a {dist:.0f}m de la base, hors geofence ({GEOFENCE_RADIUS_M:.0f}m)"
            )
        radius = float(cmd.get("radius", 10))
        if not (0.0 < radius <= MAX_ORBIT_RADIUS_M):
            raise ValueError(f"rayon d'orbite {radius:.0f}m hors limite (0, {MAX_ORBIT_RADIUS_M:.0f}]m")
        alt = float(cmd.get("altitude") or DEFAULT_ALTITUDE_M)
        if not (0.0 <= alt <= MAX_ALTITUDE_M):
            raise ValueError(f"altitude {alt:.1f}m hors limite [0, {MAX_ALTITUDE_M:.0f}]m")

def describe_command(cmd):
    """Resume factuel derive des champs numeriques valides — pas du champ
    "message" (texte libre genere par le LLM, donc pas fiable pour une
    decision de confirmation)."""
    action = cmd.get("action")
    if action == "takeoff":
        return f"takeoff → altitude cible {_target_altitude(cmd, DEFAULT_TAKEOFF_ALTITUDE_M):.1f}m"
    if action == "altitude":
        return f"altitude → altitude cible {_target_altitude(cmd):.1f}m"
    if action == "goto":
        return (f"goto → ({float(cmd['lat']):.6f}, {float(cmd['lon']):.6f}), "
                f"alt {float(cmd.get('altitude') or 15):.1f}m")
    if action == "orbit":
        return (f"orbit → rayon {float(cmd.get('radius', 10)):.0f}m, "
                f"vitesse {float(cmd.get('speed', 2)):.1f}m/s")
    return action

async def execute_command(drone, cmd):
    global drone_armed
    action = cmd.get("action")
    alt    = float(cmd.get("altitude") or DEFAULT_ALTITUDE_M)
    msg    = cmd.get("message", "OK")

    if action == "position":
        await refresh_position(drone)
        return (f"\nGPS       : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f} m (AGL)\n"
                f"Alt abs   : {current_pos['abs_alt']:.1f} m (MSL)")

    elif action == "status":
        await refresh_position(drone)
        hb = None
        while True:
            m = mav.recv_match(blocking=False)
            if m is None:
                break
            if m.get_type() == 'HEARTBEAT':
                hb = m
        if hb:
            telemetry_cache["mode"] = ARDUCOPTER_MODES.get(hb.custom_mode, f"UNKNOWN({hb.custom_mode})")
        return (f"\nPosition : {current_pos['lat']:.6f}, {current_pos['lon']:.6f}\n"
                f"Altitude  : {current_pos['alt']:.1f}m (AGL)\n"
                f"Abs alt   : {current_pos['abs_alt']:.1f}m\n"
                f"Batterie  : {telemetry_cache['battery']:.0f}%\n"
                f"Mode      : {telemetry_cache['mode']}\n"
                f"Arme      : {drone_armed}")

    elif action == "takeoff":
        await refresh_position(drone)
        target_alt = _target_altitude(cmd, DEFAULT_TAKEOFF_ALTITUDE_M)
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
        target_alt = _target_altitude(cmd)
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

# Une seule commande critique en attente de confirmation a la fois : une
# nouvelle commande (confirmee ou non) remplace/efface la precedente, pour
# qu'un operateur ne puisse jamais confirmer par erreur une commande perimee.
_pending = {}

def stage_command(cmd):
    token = secrets.token_hex(4)
    _pending.clear()
    _pending[token] = {"cmd": cmd, "expires": time.monotonic() + CONFIRM_TTL_S}
    return token

def pop_confirmed(token):
    entry = _pending.pop(token, None)
    if entry is None:
        raise ValueError("aucune commande en attente pour ce jeton")
    if time.monotonic() > entry["expires"]:
        raise ValueError("confirmation expiree (> 30s) — renvoyez la commande")
    return entry["cmd"]

async def process_text(text):
    """Texte -> commande interpretee par LFM3, validee en dur, puis executee
    immediatement (action non critique) ou mise en attente de confirmation
    (action critique)."""
    cmd = await interpret_command(text)
    try:
        validate_command(cmd)
    except ValueError as e:
        return {"status": "rejected", "message": f"Commande rejetee : {e}"}

    if cmd["action"] in CRITICAL_ACTIONS:
        token = stage_command(cmd)
        return {
            "status": "pending",
            "token": token,
            "message": f"Confirmation requise — {describe_command(cmd)} ({cmd.get('message', '')})",
        }

    result = await execute_command(drone, cmd)
    return {"status": "done", "message": result}

async def confirm_pending(token):
    """Execute la commande critique mise en attente par process_text, apres
    confirmation explicite de l'operateur."""
    try:
        cmd = pop_confirmed(token)
    except ValueError as e:
        return {"status": "rejected", "message": str(e)}
    result = await execute_command(drone, cmd)
    return {"status": "done", "message": result}

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
            asyncio.create_task(monitor_armed(drone))
            asyncio.create_task(monitor_telemetry(drone))
            asyncio.create_task(monitor_position(drone))
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
            result = await process_text(text)
            if result["status"] == "pending":
                print(result["message"])
                if input("Confirmer ? (o/n) ").strip().lower() in ("o", "oui", "y", "yes"):
                    result = await confirm_pending(result["token"])
                else:
                    print("Annule.\n")
                    continue
            print(result["message"] + "\n")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Erreur : {e}\n")

if __name__ == "__main__":
    asyncio.run(main())
