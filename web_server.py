import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import wireclaw_core


@asynccontextmanager
async def lifespan(app: FastAPI):
    await wireclaw_core.init()
    yield


app = FastAPI(lifespan=lifespan)


class CommandRequest(BaseModel):
    text: str


class ConfirmRequest(BaseModel):
    token: str


@app.post("/command")
async def command(req: CommandRequest):
    try:
        return await wireclaw_core.process_text(req.text)
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Erreur : {e}"})


@app.post("/command/confirm")
async def confirm(req: ConfirmRequest):
    try:
        return await wireclaw_core.confirm_pending(req.token)
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Erreur : {e}"})


@app.get("/status")
async def status():
    # Lecture pure depuis les caches mis à jour par les tâches de fond —
    # aucun appel bloquant, réponse instantanée
    north_m, east_m = wireclaw_core.local_offset_m()
    return {
        "lat":            wireclaw_core.current_pos["lat"],
        "lon":            wireclaw_core.current_pos["lon"],
        "alt":            round(wireclaw_core.current_pos["alt"], 2),
        "abs_alt":        round(wireclaw_core.current_pos["abs_alt"], 2),
        "battery":        round(wireclaw_core.telemetry_cache["battery"], 1),
        "mode":           wireclaw_core.telemetry_cache["mode"],
        "armed":          wireclaw_core.drone_armed,
        "heading":        round(wireclaw_core.current_pos["heading"], 1),
        "ground_speed":   round(wireclaw_core.current_pos["ground_speed"], 2),
        "vertical_speed": round(wireclaw_core.current_pos["vertical_speed"], 2),
        "north_m":        round(north_m, 1),
        "east_m":         round(east_m, 1),
    }


@app.get("/")
async def index():
    return HTMLResponse(HTML)


@app.get("/sim3d")
async def sim3d():
    return HTMLResponse(SIM3D_HTML)


HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WireClaw</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: monospace;
      background: #0d0d0d;
      color: #e0e0e0;
      padding: 24px;
      max-width: 900px;
      margin: auto;
    }
    h1 {
      color: #00ff88;
      font-size: 1.3rem;
      letter-spacing: 3px;
      margin-bottom: 20px;
    }

    /* --- Panneau de statut --- */
    .status-panel {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 10px;
      background: #141414;
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      padding: 16px;
      margin-bottom: 20px;
    }
    .stat-label {
      font-size: 0.68rem;
      color: #666;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .stat-value {
      font-size: 1rem;
      color: #00ff88;
      margin-top: 3px;
    }

    /* --- Zone de commande --- */
    .cmd-area { margin-bottom: 20px; }
    .input-row { display: flex; gap: 8px; margin-bottom: 8px; }
    input[type="text"] {
      flex: 1;
      padding: 10px 14px;
      background: #141414;
      border: 1px solid #333;
      border-radius: 4px;
      color: #e0e0e0;
      font-family: monospace;
      font-size: 0.95rem;
    }
    input[type="text"]:focus { outline: none; border-color: #00ff88; }
    input[type="text"]:disabled { opacity: 0.5; }
    button {
      padding: 10px 18px;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-family: monospace;
      font-size: 0.9rem;
      font-weight: bold;
      transition: background 0.15s;
    }
    .btn-send { background: #00ff88; color: #000; }
    .btn-send:hover { background: #00cc66; }
    .btn-send:disabled { opacity: 0.5; cursor: default; }
    .quick-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn-quick {
      background: #141414;
      color: #aaa;
      border: 1px solid #2a2a2a;
    }
    .btn-quick:hover { border-color: #00ff88; color: #00ff88; }
    .btn-confirm {
      background: #ff8800;
      color: #000;
      padding: 4px 12px;
      font-size: 0.78rem;
      margin-left: 10px;
      vertical-align: middle;
    }
    .btn-confirm:hover { background: #ffaa33; }
    .btn-confirm:disabled { opacity: 0.5; cursor: default; }

    /* --- Historique --- */
    .history {
      background: #141414;
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      padding: 16px;
      height: 340px;
      overflow-y: auto;
    }
    .entry { margin-bottom: 14px; }
    .entry-cmd { color: #4db8ff; }
    .entry-cmd::before { content: ">>> "; color: #444; }
    .entry-resp {
      color: #ccc;
      white-space: pre-wrap;
      padding-left: 22px;
      margin-top: 3px;
      line-height: 1.5;
    }
    .entry-resp.pending { color: #555; font-style: italic; }
    .entry-resp.waiting { color: #ff8800; font-style: normal; }
    .entry-resp.error   { color: #ff5555; }
    .entry-ts { font-size: 0.7rem; color: #444; padding-left: 22px; margin-top: 2px; }
  </style>
</head>
<body>
  <h1>WIRECLAW</h1>
  <div style="margin-bottom:16px;"><a href="/sim3d" style="color:#4db8ff; font-size:0.8rem;">→ Vue 3D</a></div>

  <div class="status-panel">
    <div>
      <div class="stat-label">Latitude</div>
      <div class="stat-value" id="s-lat">—</div>
    </div>
    <div>
      <div class="stat-label">Longitude</div>
      <div class="stat-value" id="s-lon">—</div>
    </div>
    <div>
      <div class="stat-label">Nord / base (m)</div>
      <div class="stat-value" id="s-north">—</div>
    </div>
    <div>
      <div class="stat-label">Est / base (m)</div>
      <div class="stat-value" id="s-east">—</div>
    </div>
    <div>
      <div class="stat-label">Altitude AGL</div>
      <div class="stat-value" id="s-alt">—</div>
    </div>
    <div>
      <div class="stat-label">Alt MSL</div>
      <div class="stat-value" id="s-abs">—</div>
    </div>
    <div>
      <div class="stat-label">Cap</div>
      <div class="stat-value" id="s-heading">—</div>
    </div>
    <div>
      <div class="stat-label">Vitesse sol</div>
      <div class="stat-value" id="s-gspeed">—</div>
    </div>
    <div>
      <div class="stat-label">Vitesse verticale</div>
      <div class="stat-value" id="s-vspeed">—</div>
    </div>
    <div>
      <div class="stat-label">Batterie</div>
      <div class="stat-value" id="s-bat">—</div>
    </div>
    <div>
      <div class="stat-label">Mode</div>
      <div class="stat-value" id="s-mode">—</div>
    </div>
    <div>
      <div class="stat-label">Arme</div>
      <div class="stat-value" id="s-armed">—</div>
    </div>
  </div>

  <div class="cmd-area">
    <div class="input-row">
      <input type="text" id="cmd-input" placeholder="Entrez une commande en langage naturel..." autofocus>
      <button class="btn-send" id="btn-send" onclick="sendCommand()">Envoyer</button>
    </div>
    <div class="quick-row">
      <button class="btn-quick" onclick="sendQuick('status')">Status</button>
      <button class="btn-quick" onclick="sendQuick('position')">Position</button>
      <button class="btn-quick" onclick="sendQuick('hover')">Hover</button>
      <button class="btn-quick" onclick="sendQuick('rtl')">RTL</button>
      <button class="btn-quick" onclick="sendQuick('atterris')">Land</button>
    </div>
  </div>

  <div class="history" id="history"></div>

  <script>
    const inputEl  = document.getElementById('cmd-input');
    const btnSend  = document.getElementById('btn-send');
    const historyEl = document.getElementById('history');

    inputEl.addEventListener('keydown', e => { if (e.key === 'Enter') sendCommand(); });

    function setLoading(on) {
      inputEl.disabled = on;
      btnSend.disabled = on;
    }

    async function sendCommand() {
      const text = inputEl.value.trim();
      if (!text) return;
      inputEl.value = '';
      await send(text);
    }

    async function sendQuick(text) {
      await send(text);
    }

    async function send(text) {
      const ts    = new Date().toLocaleTimeString('fr-FR');
      const entry = document.createElement('div');
      entry.className = 'entry';
      entry.innerHTML =
        `<div class="entry-cmd">${esc(text)}</div>` +
        `<div class="entry-resp pending">en cours...</div>` +
        `<div class="entry-ts">${ts}</div>`;
      historyEl.prepend(entry);
      const respEl = entry.querySelector('.entry-resp');

      setLoading(true);
      try {
        const r = await fetch('/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        const data = await r.json();
        renderResult(respEl, data);
      } catch (e) {
        respEl.textContent = 'Erreur réseau';
        respEl.className = 'entry-resp error';
      } finally {
        setLoading(false);
        inputEl.focus();
      }
    }

    function renderResult(respEl, data) {
      const text = data.message ?? data.detail ?? 'OK';
      if (data.status === 'pending') {
        respEl.className = 'entry-resp waiting';
        respEl.innerHTML = `${esc(text)} <button class="btn-confirm">Confirmer</button>`;
        respEl.querySelector('.btn-confirm').addEventListener('click', async (ev) => {
          const btn = ev.currentTarget;
          btn.disabled = true;
          btn.textContent = 'Envoi...';
          try {
            const r = await fetch('/command/confirm', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ token: data.token })
            });
            renderResult(respEl, await r.json());
          } catch (e) {
            respEl.textContent = 'Erreur réseau';
            respEl.className = 'entry-resp error';
          }
        });
      } else if (data.status === 'rejected' || data.status === 'error') {
        respEl.className = 'entry-resp error';
        respEl.textContent = text;
      } else {
        respEl.className = 'entry-resp';
        respEl.textContent = text;
      }
    }

    function esc(s) {
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    const CARDINALS = ['N','NE','E','SE','S','SO','O','NO'];
    function cardinal(deg) {
      return CARDINALS[Math.round(deg / 45) % 8];
    }

    async function refreshStatus() {
      try {
        const r = await fetch('/status');
        if (!r.ok) return;
        const d = await r.json();
        document.getElementById('s-lat').textContent    = d.lat.toFixed(6);
        document.getElementById('s-lon').textContent    = d.lon.toFixed(6);
        document.getElementById('s-north').textContent  = (d.north_m >= 0 ? '+' : '') + d.north_m.toFixed(1) + ' m';
        document.getElementById('s-east').textContent   = (d.east_m >= 0 ? '+' : '') + d.east_m.toFixed(1) + ' m';
        document.getElementById('s-alt').textContent    = d.alt.toFixed(1) + ' m';
        document.getElementById('s-abs').textContent    = d.abs_alt.toFixed(1) + ' m';
        document.getElementById('s-heading').textContent = d.heading.toFixed(0) + '° ' + cardinal(d.heading);
        document.getElementById('s-gspeed').textContent  = d.ground_speed.toFixed(1) + ' m/s';
        document.getElementById('s-vspeed').textContent  = (d.vertical_speed >= 0 ? '+' : '') + d.vertical_speed.toFixed(1) + ' m/s';
        document.getElementById('s-bat').textContent    = d.battery.toFixed(0) + ' %';
        document.getElementById('s-mode').textContent   = d.mode;
        const el = document.getElementById('s-armed');
        el.textContent   = d.armed ? 'OUI' : 'NON';
        el.style.color   = d.armed ? '#ff8800' : '#00ff88';
      } catch (_) {}
    }

    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""

SIM3D_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WireClaw — Simulation 3D</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #87ceeb; font-family: monospace; }
  #canvas-wrap { position: fixed; inset: 0; }
  #hud {
    position: fixed; top: 12px; left: 12px;
    background: rgba(13,13,13,0.78);
    border: 1px solid #2a2a2a;
    border-radius: 6px;
    padding: 12px 16px;
    color: #e0e0e0;
    font-size: 0.8rem;
    line-height: 1.6;
    pointer-events: none;
    min-width: 170px;
  }
  #hud h1 { color: #00ff88; font-size: 0.95rem; letter-spacing: 2px; margin-bottom: 8px; }
  .hud-label { color: #666; text-transform: uppercase; font-size: 0.62rem; letter-spacing: 1px; }
  .hud-value { color: #00ff88; }
  #status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: #ff5555; }
  #status-dot.ok { background: #00ff88; }
</style>
</head>
<body>
  <div id="canvas-wrap"></div>
  <button id="btn-fpv" style="position:fixed; top:12px; right:12px; z-index:2; background:#00ff88; color:#000; border:none; border-radius:4px; padding:10px 16px; font-family:monospace; font-weight:bold; cursor:pointer;">Vue subjective</button>
  <div id="compass-strip" style="position:fixed; top:12px; left:50%; transform:translateX(-50%); width:280px; height:30px; overflow:hidden; background:rgba(13,13,13,0.78); border:1px solid #2a2a2a; border-radius:4px; z-index:2;">
    <div id="compass-inner" style="position:absolute; top:0; left:0; height:100%;"></div>
    <div style="position:absolute; left:50%; top:0; bottom:0; width:2px; background:#ff8800; transform:translateX(-1px);"></div>
  </div>
  <div id="hud">
    <h1>WIRECLAW — SIM 3D</h1>
    <div><span id="status-dot"></span><span id="s-conn">connexion...</span></div>
    <div class="hud-label">Altitude</div>
    <div class="hud-value" id="s-alt">—</div>
    <div class="hud-label">Cap</div>
    <div class="hud-value" id="s-heading">—</div>
    <div class="hud-label">Position / base</div>
    <div class="hud-value" id="s-pos">—</div>
  </div>

<script>
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x87ceeb); // ciel bleu

const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 2000);
camera.position.set(-60, 45, 90);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(window.devicePixelRatio);
document.getElementById('canvas-wrap').appendChild(renderer.domElement);

const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.target.set(0, 5, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI / 2 - 0.02; // ne pas passer sous le sol
controls.update();

// --- Lumiere ---
scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const sun = new THREE.DirectionalLight(0xffffff, 0.8);
sun.position.set(80, 120, 60);
scene.add(sun);

// --- Sol 400x400m, vert ---
const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(400, 400),
  new THREE.MeshStandardMaterial({ color: 0x2e7d32 })
);
ground.rotation.x = -Math.PI / 2;
scene.add(ground);

// --- Repères tous les 25m (grille 400/16 divisions) ---
const grid = new THREE.GridHelper(400, 16, 0xffffff, 0xaad4aa);
grid.position.y = 0.01;
scene.add(grid);

// Points cardinaux a 195m du centre, pour se reperer avec le cap
function addLabel(text, x, z) {
  const canvas = document.createElement('canvas');
  canvas.width = 128; canvas.height = 64;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(13,13,13,0.7)';
  ctx.fillRect(0, 0, 128, 64);
  ctx.fillStyle = '#00ff88';
  ctx.font = 'bold 40px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 64, 32);
  const tex = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false }));
  sprite.scale.set(10, 5, 1);
  sprite.position.set(x, 3, z);
  scene.add(sprite);
}
addLabel('N', 0, -195);
addLabel('S', 0, 195);
addLabel('E', 195, 0);
addLabel('O', -195, 0);

// Decor statique (purement visuel, aucun lien avec la logique de vol qui
// suppose un sol plat — ne pas en deduire de relief ou de collision).
function addTree(x, z) {
  const trunk = new THREE.Mesh(
    new THREE.CylinderGeometry(0.3, 0.4, 3, 8),
    new THREE.MeshStandardMaterial({ color: 0x6d4c25 })
  );
  trunk.position.set(x, 1.5, z);
  scene.add(trunk);
  const foliage = new THREE.Mesh(
    new THREE.ConeGeometry(2.2, 5, 10),
    new THREE.MeshStandardMaterial({ color: 0x1b5e20 })
  );
  foliage.position.set(x, 5.5, z);
  scene.add(foliage);
}
[[25, 15], [100, 60], [-90, 120], [-130, -100], [60, 180], [180, 30],
 [-170, 40], [40, -160], [160, -60], [-60, 150], [120, 140], [-150, -170]]
  .forEach(([x, z]) => addTree(x, z));

const wall = new THREE.Mesh(
  new THREE.BoxGeometry(20, 3, 0.5),
  new THREE.MeshStandardMaterial({ color: 0x9e9e9e })
);
wall.position.set(130, 1.5, 170);
scene.add(wall);

// Pylone electrique (cible d'inspection typique) : fut effile en 4
// segments carres + deux bras transversaux pour les isolateurs, ~27m.
function addPylon(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  scene.add(group);
  const mat = new THREE.MeshStandardMaterial({ color: 0x555555 });
  const heights = [0, 8, 16, 24];
  const widths = [4, 2.6, 1.4, 0.6];
  for (let i = 0; i < heights.length - 1; i++) {
    const h = heights[i + 1] - heights[i];
    const seg = new THREE.Mesh(
      new THREE.CylinderGeometry(widths[i + 1] / 2, widths[i] / 2, h, 4),
      mat
    );
    seg.position.y = heights[i] + h / 2;
    seg.rotation.y = Math.PI / 4;
    group.add(seg);
  }
  [20, 26].forEach((y) => {
    const arm = new THREE.Mesh(new THREE.BoxGeometry(10, 0.4, 0.4), mat);
    arm.position.y = y;
    group.add(arm);
  });
}
// Ligne electrique : 3 pylones alignes (a 100m du bord) + cables entre les bras
[-150, 0, 150].forEach((z) => addPylon(-100, z));
[20, 26].forEach((armY) => {
  const pts = [-150, 0, 150].map((z) => new THREE.Vector3(-100, armY, z));
  const wireGeo = new THREE.BufferGeometry().setFromPoints(pts);
  scene.add(new THREE.Line(wireGeo, new THREE.LineBasicMaterial({ color: 0x222222 })));
});

// Hangar agricole : corps + toit a deux pans (deux panneaux inclines)
function addHangar(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  scene.add(group);
  const W = 14, D = 8, H = 5, roofH = 2.5;
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(W, H, D),
    new THREE.MeshStandardMaterial({ color: 0xb0bec5 })
  );
  body.position.y = H / 2;
  group.add(body);
  const slopeLen = Math.sqrt((D / 2) ** 2 + roofH ** 2);
  const angle = Math.atan2(roofH, D / 2);
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x7a2e2e });
  const slope1 = new THREE.Mesh(new THREE.BoxGeometry(W, 0.3, slopeLen), roofMat);
  slope1.position.set(0, H + roofH / 2, D / 4);
  slope1.rotation.x = angle;
  group.add(slope1);
  const slope2 = new THREE.Mesh(new THREE.BoxGeometry(W, 0.3, slopeLen), roofMat);
  slope2.position.set(0, H + roofH / 2, -D / 4);
  slope2.rotation.x = -angle;
  group.add(slope2);
}
addHangar(160, -120);

// Maisons : corps + toit pyramidal (cone a 4 faces)
function addHouse(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  scene.add(group);
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(6, 3, 6),
    new THREE.MeshStandardMaterial({ color: 0xd7ccc8 })
  );
  body.position.y = 1.5;
  group.add(body);
  const roof = new THREE.Mesh(
    new THREE.ConeGeometry(4.8, 2.5, 4),
    new THREE.MeshStandardMaterial({ color: 0x6d4c41 })
  );
  roof.position.y = 3 + 1.25;
  roof.rotation.y = Math.PI / 4;
  group.add(roof);
}
// Hameau de 10 maisons groupees au nord-est du centre (nord = -Z, est = +X)
[[-21, -14], [-7, -14], [7, -14],
 [-21, 0], [-7, 0], [7, 0], [21, 0],
 [-7, 14], [7, 14], [21, 14]]
  .forEach(([dx, dz]) => addHouse(90 + dx, -90 + dz));

// Chemin de terre reliant le hameau a la route
const path = new THREE.Mesh(
  new THREE.PlaneGeometry(3, 30),
  new THREE.MeshStandardMaterial({ color: 0x8d6e4f })
);
path.rotation.x = -Math.PI / 2;
path.position.set(90, 0.025, -75);
scene.add(path);

// Eglise : nef + toit a deux pans + clocher a fleche
function addChurch(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  scene.add(group);
  const Wd = 6, H = 6, Len = 14, roofH = 2.5;
  const nave = new THREE.Mesh(
    new THREE.BoxGeometry(Wd, H, Len),
    new THREE.MeshStandardMaterial({ color: 0xe8e0d0 })
  );
  nave.position.y = H / 2;
  group.add(nave);
  const slopeLen = Math.sqrt((Wd / 2) ** 2 + roofH ** 2);
  const angle = Math.atan2(roofH, Wd / 2);
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x4e342e });
  const slopeA = new THREE.Mesh(new THREE.BoxGeometry(slopeLen, 0.3, Len), roofMat);
  slopeA.position.set(Wd / 4, H + roofH / 2, 0);
  slopeA.rotation.z = -angle;
  group.add(slopeA);
  const slopeB = new THREE.Mesh(new THREE.BoxGeometry(slopeLen, 0.3, Len), roofMat);
  slopeB.position.set(-Wd / 4, H + roofH / 2, 0);
  slopeB.rotation.z = angle;
  group.add(slopeB);
  const tower = new THREE.Mesh(
    new THREE.BoxGeometry(4, 12, 4),
    new THREE.MeshStandardMaterial({ color: 0xcfc6b0 })
  );
  tower.position.set(0, 6, -Len / 2 - 2);
  group.add(tower);
  const spire = new THREE.Mesh(
    new THREE.ConeGeometry(3.2, 5, 4),
    new THREE.MeshStandardMaterial({ color: 0x37474f })
  );
  spire.position.set(0, 12 + 2.5, -Len / 2 - 2);
  spire.rotation.y = Math.PI / 4;
  group.add(spire);
}
addChurch(50, -90);

// Petite foret (50 arbres disperses) avec clairiere au nord-ouest
(function addForest(cx, cz, radius, count) {
  let placed = 0, guard = 0;
  while (placed < count && guard < 3000) {
    guard++;
    const dx = (Math.random() * 2 - 1) * radius;
    const dz = (Math.random() * 2 - 1) * radius;
    if (dx * dx + dz * dz > radius * radius) continue;
    if (dx < 0 && dz < 0) continue; // clairiere nord-ouest
    addTree(cx + dx, cz + dz);
    placed++;
  }
})(130, 130, 55, 50);

// Route (bande d'asphalte + ligne centrale pointillee)
const road = new THREE.Mesh(
  new THREE.PlaneGeometry(380, 6),
  new THREE.MeshStandardMaterial({ color: 0x333333 })
);
road.rotation.x = -Math.PI / 2;
road.position.set(0, 0.02, -60);
scene.add(road);
for (let i = -185; i < 185; i += 10) {
  const seg = new THREE.Mesh(
    new THREE.PlaneGeometry(4, 0.3),
    new THREE.MeshStandardMaterial({ color: 0xcccc66 })
  );
  seg.rotation.x = -Math.PI / 2;
  seg.position.set(i, 0.03, -60);
  scene.add(seg);
}

// Anneau blanc au sol marquant la position de la base (10m de diametre)
const basePad = new THREE.Mesh(
  new THREE.RingGeometry(4, 5, 48),
  new THREE.MeshStandardMaterial({ color: 0xffffff, side: THREE.DoubleSide })
);
basePad.rotation.x = -Math.PI / 2;
basePad.position.set(0, 0.015, 0);
scene.add(basePad);

// --- Drone : mat (ligne verticale jusqu'au sol) + corps + nez (cap) ---
const droneGroup = new THREE.Group();
scene.add(droneGroup);

const body = new THREE.Mesh(
  new THREE.CylinderGeometry(0.9, 0.9, 0.4, 16),
  new THREE.MeshStandardMaterial({ color: 0xff8800 })
);
droneGroup.add(body);

const nose = new THREE.Mesh(
  new THREE.ConeGeometry(0.4, 1.4, 12),
  new THREE.MeshStandardMaterial({ color: 0x00ff88 })
);
nose.rotation.x = -Math.PI / 2; // apex vers l'exterieur (cap), pas vers le centre du corps
nose.position.set(0, 0, -1.3);
droneGroup.add(nose);

const mastMat = new THREE.LineBasicMaterial({ color: 0x444444 });
let mastGeom = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0,0,0), new THREE.Vector3(0,0,0)]);
const mast = new THREE.Line(mastGeom, mastMat);
scene.add(mast);

// Vue subjective : camera embarquee sur le drone, FOV 50°, inclinee 45°
// vers le bas (camera du gimbal d'inspection). Enfant de droneGroup pour
// heriter automatiquement de sa position et de son cap (yaw).
const fpvCamera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 2000);
fpvCamera.position.set(0, 0.5, 0);
fpvCamera.rotation.x = -Math.PI / 4; // 45° vers le bas
droneGroup.add(fpvCamera);

let fpvMode = false;
const btnFpv = document.getElementById('btn-fpv');
btnFpv.addEventListener('click', () => {
  fpvMode = !fpvMode;
  controls.enabled = !fpvMode;
  body.visible = !fpvMode;
  nose.visible = !fpvMode;
  btnFpv.textContent = fpvMode ? 'Vue orbite' : 'Vue subjective';
});

// --- Boucle de rendu ---
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, fpvMode ? fpvCamera : camera);
}
animate();

window.addEventListener('resize', () => {
  const aspect = window.innerWidth / window.innerHeight;
  camera.aspect = aspect;
  camera.updateProjectionMatrix();
  fpvCamera.aspect = aspect;
  fpvCamera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// Bandeau boussole (type OSD FPV) : bande graduee tous les 15°, labels
// cardinaux tous les 45°, qui defile pour que le cap actuel reste toujours
// centre sous le repere orange fixe.
const COMPASS_PX_PER_DEG = 3;
const COMPASS_WIDTH = 280;
const compassInner = document.getElementById('compass-inner');
(function buildCompass() {
  const names = { 0: 'N', 45: 'NE', 90: 'E', 135: 'SE', 180: 'S', 225: 'SO', 270: 'O', 315: 'NO' };
  let html = '';
  for (let deg = -360; deg <= 720; deg += 15) {
    const norm = ((deg % 360) + 360) % 360;
    const label = names[norm];
    const xpos = (deg + 360) * COMPASS_PX_PER_DEG;
    html += `<div style="position:absolute; left:${xpos}px; top:2px; width:1px; height:${label ? 14 : 8}px; background:#555;"></div>`;
    if (label) {
      html += `<div style="position:absolute; left:${xpos - 10}px; top:16px; width:20px; text-align:center; font-family:monospace; font-size:0.7rem; color:#00ff88;">${label}</div>`;
    }
  }
  compassInner.innerHTML = html;
  compassInner.style.width = (1080 * COMPASS_PX_PER_DEG) + 'px';
})();
function updateCompass(heading) {
  const offsetPx = (heading + 360) * COMPASS_PX_PER_DEG - COMPASS_WIDTH / 2;
  compassInner.style.transform = `translateX(${-offsetPx}px)`;
}

// --- Telemetrie en direct ---
async function refreshStatus() {
  const dot = document.getElementById('status-dot');
  const conn = document.getElementById('s-conn');
  try {
    const r = await fetch('/status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();

    const x = d.east_m;
    const z = -d.north_m;
    const y = Math.max(d.alt, 0.2);

    droneGroup.position.set(x, y, z);
    droneGroup.rotation.y = -THREE.MathUtils.degToRad(d.heading);

    mastGeom.setFromPoints([new THREE.Vector3(x, 0, z), new THREE.Vector3(x, y, z)]);
    updateCompass(d.heading);

    document.getElementById('s-alt').textContent = d.alt.toFixed(1) + ' m';
    document.getElementById('s-heading').textContent = d.heading.toFixed(0) + '°';
    document.getElementById('s-pos').textContent =
      (d.north_m >= 0 ? '+' : '') + d.north_m.toFixed(1) + 'N, ' +
      (d.east_m >= 0 ? '+' : '') + d.east_m.toFixed(1) + 'E';

    dot.className = 'ok';
    conn.textContent = d.armed ? 'en vol' : 'au sol';
  } catch (e) {
    dot.className = '';
    conn.textContent = 'deconnecte';
  }
}
refreshStatus();
setInterval(refreshStatus, 500);
</script>
</body>
</html>
"""
