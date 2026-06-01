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


async def process(text: str) -> str:
    """Point d'entrée générique : texte libre → commande interprétée → exécutée."""
    cmd = await wireclaw_core.interpret_command(text)
    return await wireclaw_core.execute_command(wireclaw_core.drone, cmd)


class CommandRequest(BaseModel):
    text: str


@app.post("/command")
async def command(req: CommandRequest):
    response = await process(req.text)
    return {"response": response}


@app.get("/status")
async def status():
    await wireclaw_core.refresh_position(wireclaw_core.drone)
    async for bat in wireclaw_core.drone.telemetry.battery():
        raw = bat.remaining_percent
        pct = abs(raw) * 100 if abs(raw) <= 1 else abs(raw)
        break
    async for fm in wireclaw_core.drone.telemetry.flight_mode():
        mode = str(fm)
        break
    return {
        "lat":     wireclaw_core.current_pos["lat"],
        "lon":     wireclaw_core.current_pos["lon"],
        "alt":     round(wireclaw_core.current_pos["alt"], 2),
        "abs_alt": round(wireclaw_core.current_pos["abs_alt"], 2),
        "battery": round(pct, 1),
        "mode":    mode,
        "armed":   wireclaw_core.drone_armed,
    }


@app.get("/")
async def index():
    return HTMLResponse(HTML)


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
    .entry-resp.error   { color: #ff5555; }
    .entry-ts { font-size: 0.7rem; color: #444; padding-left: 22px; margin-top: 2px; }
  </style>
</head>
<body>
  <h1>WIRECLAW</h1>

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
      <div class="stat-label">Altitude AGL</div>
      <div class="stat-value" id="s-alt">—</div>
    </div>
    <div>
      <div class="stat-label">Alt MSL</div>
      <div class="stat-value" id="s-abs">—</div>
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
        `<div class="entry-resp pending" id="r-${Date.now()}">en cours...</div>` +
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
        respEl.textContent = data.response ?? data.detail ?? 'OK';
        respEl.classList.remove('pending');
      } catch (e) {
        respEl.textContent = 'Erreur réseau';
        respEl.classList.replace('pending', 'error');
      } finally {
        setLoading(false);
        inputEl.focus();
      }
    }

    function esc(s) {
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    async function refreshStatus() {
      try {
        const r = await fetch('/status');
        if (!r.ok) return;
        const d = await r.json();
        document.getElementById('s-lat').textContent  = d.lat.toFixed(6);
        document.getElementById('s-lon').textContent  = d.lon.toFixed(6);
        document.getElementById('s-alt').textContent  = d.alt.toFixed(1) + ' m';
        document.getElementById('s-abs').textContent  = d.abs_alt.toFixed(1) + ' m';
        document.getElementById('s-bat').textContent  = d.battery.toFixed(0) + ' %';
        document.getElementById('s-mode').textContent = d.mode;
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
