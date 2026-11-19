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
MAX_MOVE_DISTANCE_M = 50.0
MAX_YAW_ANGLE_DEG   = 180.0
CONFIRM_TTL_S       = 30

# Altitude par defaut quand aucune n'est precisee : 2m pour un premier envol
# seul ("decolle"/"decollage" sans valeur — tests au banc, pas de saut a
# 15m par defaut), 15m pour les autres usages (goto, orbit, changement
# d'altitude en vol).
DEFAULT_TAKEOFF_ALTITUDE_M = 2.0
DEFAULT_ALTITUDE_M         = 15.0
DEFAULT_MOVE_DISTANCE_M    = 2.0
DEFAULT_YAW_ANGLE_DEG      = 90.0

# Actions qui deplacent/arment/posent le drone : necessitent une confirmation
# explicite de l'operateur avant execution. Le reste (lecture d'etat) s'execute
# immediatement.
CRITICAL_ACTIONS = {"takeoff", "land", "rtl", "goto", "orbit", "altitude",
                     "forward", "backward", "left", "right", "uturn", "heading"}
KNOWN_ACTIONS = CRITICAL_ACTIONS | {"hover", "status", "position", "position_gps"}

SYSTEM_PROMPT = f"""Tu es WireClaw, cerveau d'un drone d'inspection autonome.
Reponds UNIQUEMENT avec un objet JSON valide, sans markdown.
Le champ "message" est TOUJOURS en francais.
Format :
{{
  "action": "takeoff | land | rtl | hover | goto | orbit | status | position | altitude | position_gps | forward | backward | left | right | uturn | heading",
  "altitude": <float metres AGL, defaut 15. Utilise pour altitude cible absolue : "monte a 10m" → 10>,
  "delta": <float|null. Variation verticale relative : "monte de 5m" → 5, "descend de 3m" → -3, null sinon>,
  "distance": <float metres, pour forward/backward uniquement : "avance de 10m" → 10, null si non precise>,
  "angle": <float degres, pour left/right uniquement (rotation RELATIVE au cap actuel) : "tourne a gauche de 45 degres" → 45, null si non precise>,
  "heading": <float degres 0-360, pour l'action "heading" uniquement (cap ABSOLU, boussole) : nord=0, est=90, sud=180, ouest=270>,
  "lat": <float, pour goto>,
  "lon": <float, pour goto>,
  "radius": <float metres, pour orbit, defaut 10>,
  "speed": <float m/s, pour orbit/forward/backward, defaut 2>,
  "message": "<confirmation courte en francais>"
}}
Position de base : lat={BASE_LAT}, lon={BASE_LON}.
Pour goto, calcule des coordonnees proches (max 500m en test).
Nord = lat+, Sud = lat-, Est = lon+, Ouest = lon-
1 degre lat = 111320m, 1 degre lon = 111320*cos(lat)m
"decolle" / "decolle a Xm" (premier envol depuis le sol) -> action="takeoff" TOUJOURS, jamais "altitude".
"monte de X metres" ou "descend de X metres" (variation verticale en vol, pas un premier decollage) -> action="altitude", jamais "rtl", "goto" ni "takeoff".
Pour "monte"/"descend"/"altitude", verifie d'abord si le mot "de" est present juste avant le nombre :
- SANS "de" juste avant le nombre (ex. "monte a 10m", "monte 10m", "altitude 10m") -> cible ABSOLUE : altitude=X, delta=null OBLIGATOIREMENT.
- AVEC "de" juste avant le nombre (ex. "monte de 10m", "descend de 3m") -> variation RELATIVE : delta=X (positif pour monte, negatif pour descend), altitude=null OBLIGATOIREMENT.
Ne jamais remplir altitude et delta en meme temps : un seul des deux est non-null, selon la presence ou non de "de".
Exemple a ne pas confondre : "monte a 5m" -> altitude=5, delta=null. "monte de 5m" -> delta=5, altitude=null (PAS altitude=5).
"retour a la base" / "rentre" / "rtl" -> action="rtl" (vol retour puis atterrissage auto), distinct de "atterris"/"land" (pose immediate sur place).
"maintiens la position" / "tiens la position" / "reste la" -> action="hover" (arret sur place en vol), jamais "position" (qui ne fait qu'afficher la position actuelle, action differente).
"avance" / "avance de Xm" (deplacement horizontal vers l'avant, relatif au cap actuel) -> action="forward", distance=X si precise sinon null. Jamais "takeoff" ni "altitude".
"recule" / "recule de Xm" (deplacement horizontal vers l'arriere) -> action="backward", distance=X si precise sinon null. Jamais "rtl".
"avance/recule a X metres par seconde" (unite "par seconde" = vitesse) -> speed=X, PAS distance. "distance" ne recoit que des metres sans "par seconde" (ex. "avance de 10 metres" -> distance=10). Si les deux sont precises ("avance de 10 metres a 4 metres par seconde") -> distance=10 ET speed=4, deux valeurs distinctes.
"gauche" / "tourne a gauche [de X degres]" -> action="left", angle=X si precise sinon null.
"droite" / "tourne a droite [de X degres]" -> action="right", angle=X si precise sinon null.
"demi-tour" -> action="uturn" (toujours 180 degres, pas de direction a choisir).
"oriente-toi au nord/sud/est/ouest" ou "cap X degres" ou "oriente-toi vers X degres" (orientation ABSOLUE, pas "a gauche"/"a droite") -> action="heading", heading=X (nord=0, est=90, sud=180, ouest=270 si direction cardinale nommee).
Les nombres peuvent etre dictes en toutes lettres (ex. "dix" = 10, "cinq" = 5, "huit" = 8) : les convertir systematiquement en chiffre dans "altitude"/"delta"/"distance"/"angle"/"radius"/"speed", ne jamais les laisser a leur valeur par defaut quand un nombre a ete prononce.
"orbite"/"orbit" seul, sans rayon ni vitesse precises -> radius=10, speed=2 EXACTEMENT (les valeurs par defaut ci-dessus), jamais d'autre nombre invente.
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
current_pos = {"lat": BASE_LAT, "lon": BASE_LON, "alt": 0, "abs_alt": 584, "heading": 0.0,
               "ground_speed": 0.0, "vertical_speed": 0.0}
telemetry_cache = {"battery": -1.0, "mode": "UNKNOWN", "gps_fix": "UNKNOWN", "gps_sats": 0}
# Position de decollage reelle (home ArduPilot, fixee a l'armement) — permet
# d'afficher la position courante en metres nord/est plutot qu'en GPS brut,
# bien plus lisible pour un operateur au sol.
home_pos = {"lat": None, "lon": None}
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

def send_yaw_cmd(angle_deg, direction, relative=1):
    """MAV_CMD_CONDITION_YAW (115).
    relative=1 : angle_deg est une rotation relative au cap actuel (gauche/droite/demi-tour).
    relative=0 : angle_deg est un cap absolu a atteindre (verrouillage de cap avant un
    deplacement forward/backward — direction=0 laisse ArduPilot choisir le sens le plus court).
    direction : -1 = sens anti-horaire, 1 = sens horaire, 0 = le plus court (relative=0 seulement)."""
    mav.mav.command_long_send(
        mav.target_system, mav.target_component,
        115, 0,
        angle_deg, 0, direction, relative,  # param2=vitesse yaw (0=defaut), param4=relatif/absolu
        0, 0, 0
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

async def monitor_heading(drone):
    """Rafraichit le cap en continu — necessaire pour forward/backward
    (deplacement relatif au cap actuel) et pour verifier le geofence en
    validation avant meme d'executer le deplacement."""
    while True:
        try:
            async for h in drone.telemetry.heading():
                current_pos["heading"] = h.heading_deg
                break
        except Exception:
            pass
        await asyncio.sleep(1)

async def monitor_velocity(drone):
    """Vitesse sol et vitesse verticale (instruments de navigation de base,
    comme un avion : cap, vitesse, variomètre)."""
    while True:
        try:
            async for v in drone.telemetry.velocity_ned():
                current_pos["ground_speed"] = math.hypot(v.north_m_s, v.east_m_s)
                current_pos["vertical_speed"] = -v.down_m_s  # positif = monte
                break
        except Exception:
            pass
        await asyncio.sleep(1)

async def monitor_home(drone):
    """Capture la position de decollage reelle (home ArduPilot, fixee a
    l'armement) — sert a convertir la position GPS courante en metres
    nord/est par rapport au point de depart, plus lisible qu'un lat/lon
    brut pour un operateur au sol."""
    async for home in drone.telemetry.home():
        home_pos["lat"] = home.latitude_deg
        home_pos["lon"] = home.longitude_deg

def local_offset_m():
    """Position actuelle en metres (nord, est) par rapport a home_pos."""
    if home_pos["lat"] is None:
        return 0.0, 0.0
    north = (current_pos["lat"] - home_pos["lat"]) * 111320
    east = (current_pos["lon"] - home_pos["lon"]) * 111320 * math.cos(math.radians(home_pos["lat"]))
    return north, east

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

def _target_altitude(cmd, default_alt=DEFAULT_ALTITUDE_M, is_takeoff=False):
    """Calcule l'altitude cible pour takeoff/altitude — seule source de
    verite, utilisee a la fois par validate_command() et execute_command()
    pour qu'elles ne puissent jamais diverger.
    Au sol (premier envol), delta n'a pas de sens : rien n'a encore decolle
    pour lui servir de reference — on l'ignore donc normalement, seule
    l'altitude absolue compte (defaut selon default_alt).
    Exception pour is_takeoff=True : LFM3 met la valeur annoncee tantot
    dans "altitude" tantot dans "delta" pour "decolle a Xm" (observe de
    facon imprevisible mais toujours dans l'un des deux), et avant un
    premier envol delta n'a de toute facon aucun sens relatif — les deux
    champs sont donc acceptes comme source de la cible absolue.
    En vol, delta s'applique normalement comme variation relative."""
    raw_alt = cmd.get("altitude")
    if not raw_alt and is_takeoff:
        raw_alt = cmd.get("delta")
    alt = abs(float(raw_alt)) if raw_alt else default_alt
    if not drone_armed:
        return alt
    delta = cmd.get("delta")
    return current_pos["alt"] + float(delta) if delta is not None else alt

def _move_distance(cmd):
    """Distance absolue (toujours positive) pour forward/backward. Le sens
    vient de l'action elle-meme (forward ou backward), jamais du signe d'un
    champ numerique — meme piege que delta=0 pour takeoff : un champ que le
    LLM remplit de façon peu fiable ne doit jamais etre la seule source du
    sens du mouvement."""
    d = cmd.get("distance")
    return abs(float(d)) if d else DEFAULT_MOVE_DISTANCE_M

def _yaw_angle(cmd):
    """Angle de rotation (toujours positif) pour left/right. uturn ignore
    ce champ et utilise toujours 180°, gere a part dans les appelants."""
    a = cmd.get("angle")
    return abs(float(a)) if a else DEFAULT_YAW_ANGLE_DEG

def _offset_position(lat, lon, heading_deg, distance_m):
    """Point a distance_m metres de (lat, lon) dans la direction heading_deg
    (0=Nord, sens horaire) — utilisee a la fois pour predire la position en
    validation (verification geofence) et pour l'execution reelle, pour que
    les deux ne puissent jamais diverger."""
    rad = math.radians(heading_deg)
    dlat = (distance_m * math.cos(rad)) / 111320
    dlon = (distance_m * math.sin(rad)) / (111320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon

def validate_command(cmd):
    """Verifie les bornes de securite en dur. Leve ValueError pour rejeter
    la commande en entier — jamais d'execution partielle sur une commande
    suspecte."""
    action = cmd.get("action")
    if action not in KNOWN_ACTIONS:
        raise ValueError(f"action inconnue : {action!r}")

    if action in ("takeoff", "altitude"):
        if action == "altitude" and not drone_armed:
            delta = cmd.get("delta")
            if delta is not None and float(delta) < 0:
                raise ValueError("impossible de descendre : deja au sol")
        default_alt = DEFAULT_TAKEOFF_ALTITUDE_M if action == "takeoff" else DEFAULT_ALTITUDE_M
        target = _target_altitude(cmd, default_alt, is_takeoff=(action == "takeoff"))
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

    elif action in ("forward", "backward"):
        distance = _move_distance(cmd)
        if not (0.0 < distance <= MAX_MOVE_DISTANCE_M):
            raise ValueError(f"distance {distance:.0f}m hors limite (0, {MAX_MOVE_DISTANCE_M:.0f}]m")
        signed = distance if action == "forward" else -distance
        target_lat, target_lon = _offset_position(
            current_pos["lat"], current_pos["lon"], current_pos["heading"], signed
        )
        dist = _distance_m(BASE_LAT, BASE_LON, target_lat, target_lon)
        if dist > GEOFENCE_RADIUS_M:
            raise ValueError(
                f"deplacement amenerait a {dist:.0f}m de la base, hors geofence ({GEOFENCE_RADIUS_M:.0f}m)"
            )

    elif action in ("left", "right", "uturn"):
        angle = 180.0 if action == "uturn" else _yaw_angle(cmd)
        if not (0.0 < angle <= MAX_YAW_ANGLE_DEG):
            raise ValueError(f"angle {angle:.0f}° hors limite (0, {MAX_YAW_ANGLE_DEG:.0f}]°")

    elif action == "heading":
        heading = cmd.get("heading")
        if heading is None:
            raise ValueError("heading sans cap precise")
        if not (0.0 <= float(heading) < 360.0):
            raise ValueError(f"cap {float(heading):.0f}° hors limite [0, 360)°")

def describe_command(cmd):
    """Resume factuel derive des champs numeriques valides — pas du champ
    "message" (texte libre genere par le LLM, donc pas fiable pour une
    decision de confirmation)."""
    action = cmd.get("action")
    if action == "takeoff":
        return f"takeoff → altitude cible {_target_altitude(cmd, DEFAULT_TAKEOFF_ALTITUDE_M, is_takeoff=True):.1f}m"
    if action == "altitude":
        return f"altitude → altitude cible {_target_altitude(cmd):.1f}m"
    if action == "goto":
        return (f"goto → ({float(cmd['lat']):.6f}, {float(cmd['lon']):.6f}), "
                f"alt {float(cmd.get('altitude') or 15):.1f}m")
    if action == "orbit":
        return (f"orbit → rayon {float(cmd.get('radius', 10)):.0f}m, "
                f"vitesse {float(cmd.get('speed', 2)):.1f}m/s")
    if action in ("forward", "backward"):
        verb = "avance" if action == "forward" else "recule"
        return f"{verb} → {_move_distance(cmd):.1f}m"
    if action in ("left", "right", "uturn"):
        angle = 180.0 if action == "uturn" else _yaw_angle(cmd)
        verb = {"left": "tourne a gauche", "right": "tourne a droite", "uturn": "demi-tour"}[action]
        return f"{verb} → {angle:.0f}°"
    if action == "heading":
        return f"heading → cap {float(cmd.get('heading', 0)):.0f}°"
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
        target_alt = _target_altitude(cmd, DEFAULT_TAKEOFF_ALTITUDE_M, is_takeoff=True)
        if drone_armed:
            new_abs_alt = current_pos["abs_alt"] - current_pos["alt"] + target_alt
            await drone.action.goto_location(
                current_pos["lat"], current_pos["lon"], new_abs_alt, float('nan')
            )
            await asyncio.sleep(6)
            await refresh_position(drone)
            return f"OK {msg} — altitude {current_pos['alt']:.1f}m (cible {target_alt:.1f}m)"
        await drone.action.set_takeoff_altitude(target_alt)
        await drone.action.arm()
        await drone.action.takeoff()
        await asyncio.sleep(8)
        await refresh_position(drone)
        return f"OK {msg} — altitude {current_pos['alt']:.1f}m (cible {target_alt:.1f}m)"

    elif action == "goto":
        if not drone_armed:
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
        if not drone_armed:
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
        if not drone_armed:
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

    elif action in ("forward", "backward"):
        if not drone_armed:
            await drone.action.set_takeoff_altitude(alt)
            await drone.action.arm()
            await drone.action.takeoff()
            await asyncio.sleep(8)
        await refresh_position(drone)
        distance = _move_distance(cmd)
        heading = current_pos["heading"]
        signed = distance if action == "forward" else -distance
        target_lat, target_lon = _offset_position(
            current_pos["lat"], current_pos["lon"], heading, signed
        )
        # WP_YAW_BEHAVIOR=0 (sitl_params.parm) empeche ArduCopter de tourner
        # pour faire face au point cible pendant un goto_location — sans ca,
        # "recule" (cible derriere le drone) finissait par un demi-tour
        # complet au lieu de garder le cap d'origine (teste : un verrouillage
        # via MAV_CMD_CONDITION_YAW juste avant n'etait pas suffisant, le
        # goto_location semble reprendre la main sur le yaw aussitot apres).
        await drone.action.goto_location(target_lat, target_lon, current_pos["abs_alt"], float('nan'))
        await asyncio.sleep(4)
        await refresh_position(drone)
        return f"OK {msg} — ({current_pos['lat']:.6f}, {current_pos['lon']:.6f})"

    elif action in ("left", "right", "uturn"):
        angle = 180.0 if action == "uturn" else _yaw_angle(cmd)
        direction = -1 if action == "left" else 1
        send_yaw_cmd(angle, direction)
        return "OK " + msg

    elif action == "heading":
        target_heading = float(cmd["heading"]) % 360
        send_yaw_cmd(target_heading, 0, relative=0)
        return f"OK {msg} — cap cible {target_heading:.0f}°"

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
    immediatement. La confirmation prealable des actions critiques a ete
    retiree a la demande de l'operateur (ralentissait les tests) — la
    validation en dur (bornes altitude/geofence/distance/angle, action
    connue) reste le seul filet et s'applique toujours avant toute
    execution, inchangee."""
    cmd = await interpret_command(text)
    try:
        validate_command(cmd)
    except ValueError as e:
        return {"status": "rejected", "message": f"Commande rejetee : {e}"}

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
            asyncio.create_task(monitor_heading(drone))
            asyncio.create_task(monitor_velocity(drone))
            asyncio.create_task(monitor_home(drone))
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
