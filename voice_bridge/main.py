"""
Pont voix/texte -> commande WireClaw, pilotage reel du drone.

Transcription audio geree ici (lfm3-audio, independant du reste). Pour
l'interpretation et l'execution, aucune logique propre : tout passe par
l'API de wireclaw.service (port 8765), qui fait interpretation LFM3 +
validation en dur + confirmation pour les actions critiques
(wireclaw_core.py). Ne jamais dupliquer ce pipeline ici — c'est le seul
filet contre une hallucination du LLM, le dupliquer reviendrait a le
contourner par un deuxieme chemin non protege.
"""
import difflib
import os
import subprocess
import tempfile

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

WIRECLAW_URL = "http://127.0.0.1:8765"
OLLAMA_URL = "http://127.0.0.1:11434"

# Vocabulaire de pilotage connu, pour corriger en sortie toute transcription
# proche d'un terme attendu (ex. "statue" entendu a la place de "status").
KNOWN_VOCABULARY = [
    "status", "position", "altitude", "hover", "orbite",
    "decolle", "atterris", "monte", "descend", "retour", "base",
]


def correct_known_vocabulary(text: str) -> str:
    """Corrige, mot par mot, toute transcription proche d'un terme de
    pilotage connu (ex. "statue" -> "status") sans repasser par le LLM —
    pur rapprochement de chaines, donc instantane."""
    words = text.split()
    corrected = []
    for word in words:
        bare = word.strip(".,!?").lower()
        match = difflib.get_close_matches(bare, KNOWN_VOCABULARY, n=1, cutoff=0.72)
        corrected.append(match[0] if match else word)
    return " ".join(corrected)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class InterpretRequest(BaseModel):
    text: str


class ConfirmRequest(BaseModel):
    token: str


@app.post("/api/interpret")
async def interpret(req: InterpretRequest):
    """Texte -> interpretation + validation + execution (ou mise en attente
    si action critique), via le pipeline de production de wireclaw.service."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{WIRECLAW_URL}/command", json={"text": req.text})
    return r.json()


@app.post("/api/interpret/confirm")
async def confirm(req: ConfirmRequest):
    """Confirme et execute une commande critique mise en attente par /api/interpret."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{WIRECLAW_URL}/command/confirm", json={"token": req.token})
    return r.json()


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Audio (webm/ogg/wav, peu importe) -> conversion 16kHz mono -> transcription lfm3-audio.

    Passe par l'endpoint OpenAI-compatible /v1/audio/transcriptions d'Ollama,
    pas par `ollama run` en CLI : ~0.3s contre 11-15s pour le meme fichier,
    le CLI ayant un gros surcout de demarrage (parsing, rendu du spinner de
    progression, pas d'API HTTP directe pour l'audio avant Ollama 0.40.13)."""
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

        with open(wav_path, "rb") as f:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{OLLAMA_URL}/v1/audio/transcriptions",
                    data={"model": "lfm3-audio"},
                    files={"file": ("audio16k.wav", f, "audio/wav")},
                )
    if r.status_code != 200:
        return {"error": f"Transcription echouee ({r.status_code}) : {r.text}"}

    text = correct_known_vocabulary(r.json().get("text", "").strip())
    return {"text": text}


@app.get("/api/status")
async def status():
    """Proxy vers l'API WireClaw reelle (variables de navigation en direct)."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{WIRECLAW_URL}/status")
            return r.json()
        except httpx.RequestError as e:
            return {"error": f"WireClaw inaccessible : {e}"}
