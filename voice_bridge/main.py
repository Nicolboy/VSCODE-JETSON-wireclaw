"""
Backend de test pour la chaine voix/texte -> commande WireClaw.

Objectif : valider micro->transcription (lfm3-audio) et texte->commande JSON
(lfm3, meme schema que wireclaw_core.py) independamment de Gemini (API key
invalide actuellement) et sans dependre du reste du pipeline de production.
"""
import json
import os
import re
import subprocess
import tempfile

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

OLLAMA_URL = "http://127.0.0.1:11434"
WIRECLAW_STATUS_URL = "http://127.0.0.1:8765/status"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Meme schema de commande que wireclaw_core.py (SYSTEM_PROMPT), adapte pour LFM3.
SYSTEM_PROMPT = """Tu es WireClaw, cerveau d'un drone d'inspection autonome.
Reponds UNIQUEMENT avec un objet JSON valide, sans markdown.
Le champ "message" est TOUJOURS en francais.
Format :
{
  "action": "takeoff | land | rtl | hover | goto | orbit | status | position | altitude | position_gps",
  "altitude": <float metres AGL, defaut 15. "monte a 10m" -> 10>,
  "delta": <float ou null. "monte de 5m" -> 5, "descend de 3m" -> -3, sinon null>,
  "lat": <float ou null, pour goto>,
  "lon": <float ou null, pour goto>,
  "radius": <float metres, pour orbit, defaut 10>,
  "speed": <float m/s, pour orbit, defaut 2>,
  "message": "<confirmation courte en francais>"
}
Nord = lat+, Sud = lat-, Est = lon+, Ouest = lon-
"decolle" / "decolle a Xm" (premier envol depuis le sol) -> action="takeoff" TOUJOURS, jamais "altitude" ni "hover".
"altitude X metres" ou "monte a X metres" (consigne de hauteur en vol, deja en l'air) -> action="altitude", jamais "hover" ni "takeoff".
"monte de X metres" ou "descend de X metres" (variation verticale en vol) -> action="altitude", jamais "rtl", "goto" ni "takeoff".
"retour a la base" / "rentre" / "rtl" -> action="rtl" (vol retour puis atterrissage auto), distinct de "atterris"/"land" (pose immediate sur place).
Le champ "message" doit TOUJOURS etre en francais, jamais en anglais.
"""


class InterpretRequest(BaseModel):
    text: str


@app.post("/api/interpret")
async def interpret(req: InterpretRequest):
    """Texte libre -> commande JSON structuree, via LFM3 local (format force)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": "lfm3",
                "system": SYSTEM_PROMPT,
                "prompt": req.text,
                "think": False,
                "format": "json",
                "stream": False,
            },
        )
    data = r.json()
    try:
        command = json.loads(data["response"])
    except (KeyError, json.JSONDecodeError) as e:
        return {"error": f"Reponse LFM3 invalide : {e}", "raw": data.get("response")}
    return {"command": command}


def clean_ollama_cli_output(raw: str) -> str:
    """Retire les sequences ANSI (curseur, couleur) et le bloc 'Thinking...'
    laisses par `ollama run` en sortie CLI — il n'y a pas d'equivalent
    --think=false pour la sortie audio via CLI, contrairement a l'API HTTP."""
    text = raw.replace("\x1b", "")
    text = re.sub(r"\[\?\d+[hl]", "", text)
    text = re.sub(r"\[[0-9]*[GKA]", "", text)
    text = re.sub(r"^Added \w+ '.*'$", "", text, flags=re.MULTILINE)
    if "...done thinking." in text:
        text = text.split("...done thinking.", 1)[1]
    # Le modele echo parfois la consigne en fin de transcription — on la retire si presente.
    text = re.sub(r"Transcri[bs]e? cet audio en fran[cç]ais,?\s*rien d'autre\.?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n+", " ", text).strip()
    return text


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Audio (webm/ogg/wav, peu importe) -> conversion 16kHz mono -> transcription lfm3-audio."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = os.path.join(tmp, "input")
        wav_path = os.path.join(tmp, "audio16k.wav")
        raw_bytes = await file.read()
        with open(raw_path, "wb") as f:
            f.write(raw_bytes)
        # Copie de debug persistante (hors du tempdir, qui est efface a la sortie
        # du `with`) pour diagnostiquer les echecs de conversion sans redemander
        # le fichier a l'operateur.
        with open("/tmp/voice_bridge_last_upload.webm", "wb") as f:
            f.write(raw_bytes)

        conv = subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, text=True,
        )
        if conv.returncode != 0:
            return {"error": f"Conversion audio echouee ({len(raw_bytes)} octets recus) : {conv.stderr.strip()}"}

        result = subprocess.run(
            ["ollama", "run", "lfm3-audio", "Transcris cet audio en francais, rien d'autre.", wav_path],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            return {"error": f"Transcription echouee : {result.stderr.strip()}"}

    return {"text": clean_ollama_cli_output(result.stdout)}


@app.get("/api/status")
async def status():
    """Proxy vers l'API WireClaw reelle (variables de navigation en direct)."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(WIRECLAW_STATUS_URL)
            return r.json()
        except httpx.RequestError as e:
            return {"error": f"WireClaw inaccessible : {e}"}
