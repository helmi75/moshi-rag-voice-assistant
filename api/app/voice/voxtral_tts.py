"""La voix Voxtral de Mistral (Marie), en flux HTTPS (SCRUM-94).

La liaison avec Twilio ne change pas : l'appel reste un websocket Media Streams, et le
pipeline Pipecat reste le même. Seule la synthèse change de fournisseur : au lieu d'un
websocket vers moshi-server sur notre GPU, une requête HTTPS par phrase à l'API de
Mistral, dont la réponse arrive EN FLUX (Server-Sent Events, `speech.audio.delta`, PCM
float32 24 kHz). Le premier morceau arrive en 0,43 s de médiane (mesuré le 28/09/2026,
dix phrases réelles) : l'audio part vers l'appelant pendant que la suite se calcule.

Pourquoi pas `pipecat.services.mistral` : il ajoute la bibliothèque `mistralai` à l'image,
et convertit l'audio échantillon par échantillon en Python. Ici, `httpx` (déjà là) et
numpy. Une seule connexion HTTP gardée ouverte pour tout l'appel : pas de poignée de main
TLS à chaque phrase.

Aucun GPU derrière : ni réveil de 46 s, ni capacité L4 introuvable. En revanche, une
panne de Mistral rendrait l'assistante muette. D'où le réessai (une fois, si rien n'est
encore parti), les délais bornés, et le relevé des échecs que lit la supervision.
"""
import asyncio
import base64
import json
import os
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Optional

import httpx
import numpy as np
import soxr
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

URL = "https://api.mistral.ai/v1/audio/speech"
TAUX = 24000  # PCM rendu par Voxtral
# Connexion 5 s, puis 15 s au plus entre deux morceaux : jamais d'attente infinie. Une
# phrase se rend en 1 à 2 s de bout en bout.
DELAIS = httpx.Timeout(15.0, connect=5.0)


class VoxtralErreur(Exception):
    pass


def modele() -> str:
    return os.getenv("VOXTRAL_MODEL", "voxtral-mini-tts-2603").strip() or "voxtral-mini-tts-2603"


def _cle() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


# --- Santé : lue par le contrôle de supervision « Voix Voxtral » --------------------
# En mémoire du processus : une panne de Mistral se lit dans les appels de l'heure, et un
# redémarrage n'en efface que le souvenir, pas la cause.
_ECHECS: deque = deque(maxlen=200)  # (time.time(), message)


def noter_echec(message: str) -> None:
    _ECHECS.append((time.time(), message[:200]))


def echecs_depuis(secondes: float) -> list[tuple[float, str]]:
    seuil = time.time() - secondes
    return [e for e in _ECHECS if e[0] >= seuil]


def oublier_echecs() -> None:
    """Pour les tests."""
    _ECHECS.clear()


async def flux_pcm(client: httpx.AsyncClient, texte: str, voix_id: str) -> AsyncIterator[np.ndarray]:
    """Les morceaux d'audio d'une phrase, float32 24 kHz, au fil de leur arrivée."""
    corps = {"model": modele(), "input": texte, "voice_id": voix_id,
             "response_format": "pcm", "stream": True}
    entetes = {"Authorization": f"Bearer {_cle()}", "Accept": "text/event-stream"}
    async with client.stream("POST", URL, json=corps, headers=entetes) as reponse:
        if reponse.status_code >= 400:
            detail = (await reponse.aread())[:200].decode("utf-8", "replace")
            raise VoxtralErreur(f"HTTP {reponse.status_code} : {detail}")
        async for ligne in reponse.aiter_lines():
            if not ligne.startswith("data:"):
                continue
            donnees = json.loads(ligne[5:])
            if donnees.get("type") == "speech.audio.delta":
                yield np.frombuffer(base64.b64decode(donnees["audio_data"]), dtype="<f4")
            elif donnees.get("type") == "error" or "error" in donnees:
                raise VoxtralErreur(str(donnees)[:200])


async def rendre_pcm(texte: str, voix_id: str) -> Optional[np.ndarray]:
    """La phrase entière, float32 24 kHz : pour l'accueil pré-enregistré, rendu une fois."""
    if not _cle():
        raise VoxtralErreur("MISTRAL_API_KEY absente")
    morceaux = []
    async with httpx.AsyncClient(timeout=DELAIS) as client:
        async for pcm in flux_pcm(client, texte, voix_id):
            morceaux.append(pcm)
    return np.clip(np.concatenate(morceaux), -1.0, 1.0) if morceaux else None


class VoxtralTTSService(TTSService):
    """TTS Pipecat de la voix Voxtral choisie par l'établissement."""

    def __init__(self, voix_id: str, **kwargs):
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(model=modele(), voice=voix_id, language=None),
            **kwargs,
        )
        self._voix_id = voix_id
        self._client: Optional[httpx.AsyncClient] = None

    def can_generate_metrics(self) -> bool:
        return True

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DELAIS)
        return self._client

    async def cleanup(self):
        await super().cleanup()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Génération TTS [{text}]")
        await self.start_tts_usage_metrics(text)
        for essai in (1, 2):
            envoyes = 0
            # Un rééchantillonneur par phrase, VIDÉ à la fin (`last=True`). Celui de
            # Pipecat garde ~60 ms en réserve et les jette après 0,2 s d'inactivité :
            # la fin de chaque phrase y restait (mesuré au test : 0,2 s rendues, 0,14 s
            # sorties).
            flux = soxr.ResampleStream(TAUX, self.sample_rate, 1, dtype="int16", quality="VHQ")
            try:
                async for pcm in flux_pcm(self._http(), text, self._voix_id):
                    entier = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
                    audio = flux.resample_chunk(entier).tobytes()
                    await self.stop_ttfb_metrics()
                    if audio:
                        envoyes += 1
                        yield TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=context_id)
                reste = flux.resample_chunk(np.zeros(0, dtype=np.int16), last=True).tobytes()
                if reste:
                    yield TTSAudioRawFrame(reste, self.sample_rate, 1, context_id=context_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                noter_echec(message)
                logger.warning(f"Voxtral : synthèse échouée (essai {essai}) : {message}")
                # Réessayer après un début d'audio ferait entendre deux fois le début
                # de la phrase : on ne réessaie que si rien n'est encore parti.
                if envoyes or essai == 2:
                    yield ErrorFrame(error=f"Voxtral : {message}")
                    return
