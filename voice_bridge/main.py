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
import re
import subprocess
import tempfile

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

WIRECLAW_URL = "http://127.0.0.1:8765"

# Vocabulaire de pilotage connu — sert a la fois d'indice dans le prompt de
# transcription (guide lfm3-audio vers les mots attendus, plus rapide et
# plus fiable qu'une transcription libre) et de correction deterministe en
# sortie (ex. "statue" entendu a la place de "status").
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


def clean_ollama_cli_output(raw: str, prompt: str = "") -> str:
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
    if prompt:
        text = text.replace(prompt, "")
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

        prompt = (
            "Transcris cet audio en francais, rien d'autre. "
            "C'est une commande courte de pilotage de drone, vocabulaire probable : "
            + ", ".join(KNOWN_VOCABULARY) + "."
        )
        result = subprocess.run(
            ["ollama", "run", "lfm3-audio", prompt, wav_path],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            return {"error": f"Transcription echouee : {result.stderr.strip()}"}

    text = correct_known_vocabulary(clean_ollama_cli_output(result.stdout, prompt))
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
