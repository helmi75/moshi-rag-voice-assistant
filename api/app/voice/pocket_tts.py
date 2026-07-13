"""Service TTS Pipecat basé sur Kyutai Pocket TTS (la voix de la famille Unmute).

Pocket TTS (kyutai-labs, MIT) : 100 M paramètres, tourne sur CPU en temps réel,
français natif, voix préréglées ou clonage de voix. C'est le petit frère du TTS
1.6B qui donne la voix d'unmute.sh — même famille technologique, sans GPU.

- Voix du catalogue (défaut) : `POCKET_TTS_VOICE=estelle` (voix française prête,
  chargée depuis un .safetensors préréglé, sans clonage). Catalogue : estelle,
  cosette, marius, alba, jean, anna, vera, fantine, paul, eponine, george...
- Clonage : `POCKET_TTS_VOICE` = chemin d'un fichier audio, ou une référence
  `hf://...` / URL vers un échantillon → la voix est clonée depuis cet extrait
  (nécessite les poids de cloning : accepter les conditions sur huggingface.co
  et être authentifié `hf auth login`).
- Langue : `POCKET_TTS_LANGUAGE=french` (défaut), `french_24l` = meilleure qualité,
  plus lente.

Le modèle et l'état de voix sont chargés une seule fois par process (singleton) ;
le premier chargement télécharge les poids depuis Hugging Face (~30-60 s).
"""
import asyncio
import os
import threading
import time
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

# Chargé paresseusement : (model, base_voice_state, native_sample_rate)
_MODEL_CACHE: tuple | None = None
# Pocket TTS n'est pas thread-safe sur une même instance de modèle : on sérialise
# les générations concurrentes (limite assumée en phase A — faible simultanéité).
_GEN_LOCK: asyncio.Lock | None = None
# Le chargement du modèle se fait depuis asyncio.to_thread (vrais threads) : un
# threading.Lock est nécessaire (pas asyncio.Lock) pour garantir UN SEUL chargement,
# même si plusieurs tours de parole déclenchent run_tts en parallèle.
_MODEL_LOCK = threading.Lock()


def _resolve_voice(voice: str) -> str:
    """Transforme la valeur POCKET_TTS_VOICE en référence audio pour le modèle.

    Un nom du catalogue ("estelle", "alba"...) est passé TEL QUEL : le modèle
    charge alors la voix depuis son fichier .safetensors préréglé — aucun clonage,
    donc pas besoin d'accepter les conditions HF ni des poids de cloning. Toute
    autre valeur (chemin local, URL http(s), hf:// vers un extrait audio) est aussi
    passée telle quelle mais déclenche le clonage de voix (poids dédiés requis)."""
    return voice


def _load_model_and_state():
    """Charge le modèle Pocket TTS et l'état de la voix — EXACTEMENT une fois.

    Verrouillage à double vérification (threading.Lock car appelé depuis des
    threads via asyncio.to_thread) : le premier appelant charge le modèle (long),
    les appels concurrents attendent puis récupèrent le cache — évite le
    rechargement en boucle à chaque tour de parole."""
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    with _MODEL_LOCK:
        if _MODEL_CACHE is None:
            from pocket_tts import TTSModel

            language = os.getenv("POCKET_TTS_LANGUAGE", "french_24l")
            voice = os.getenv("POCKET_TTS_VOICE", "estelle")
            logger.info(f"Chargement de Pocket TTS (langue={language}, voix={voice})...")
            started = time.monotonic()
            model = TTSModel.load_model(language=language)
            voice_ref = _resolve_voice(voice)
            base_state = model.get_state_for_audio_prompt(voice_ref)
            _MODEL_CACHE = (model, base_state, int(model.sample_rate))
            logger.info(
                f"Pocket TTS prêt ({_MODEL_CACHE[2]} Hz) en "
                f"{time.monotonic() - started:.1f}s."
            )
    return _MODEL_CACHE


def _gen_lock() -> asyncio.Lock:
    global _GEN_LOCK
    if _GEN_LOCK is None:
        _GEN_LOCK = asyncio.Lock()
    return _GEN_LOCK


class PocketTTSService(TTSService):
    """TTS Pipecat utilisant Kyutai Pocket TTS (CPU, français, voix Unmute)."""

    def __init__(self, **kwargs):
        # La voix et la langue sont pilotées par les variables d'environnement
        # (résolues au chargement du modèle) ; on renseigne quand même le settings
        # Pipecat pour satisfaire sa validation (model/voice/language "given").
        settings = TTSSettings(
            model=None,
            voice=os.getenv("POCKET_TTS_VOICE", "estelle"),
            language=None,
        )
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._resampler = create_stream_resampler()

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Génération TTS [{text}]")
        try:
            await self.start_tts_usage_metrics(text)
            model, base_state, native_rate = await asyncio.to_thread(_load_model_and_state)

            # Une génération à la fois (modèle non thread-safe). copy_state=True
            # préserve l'état de voix de base d'un énoncé à l'autre.
            async with _gen_lock():
                gen = model.generate_audio_stream(base_state, text, copy_state=True)
                sentinel = object()
                first = True
                while True:
                    chunk = await asyncio.to_thread(next, gen, sentinel)
                    if chunk is sentinel:
                        break
                    if first:
                        await self.stop_ttfb_metrics()
                        first = False
                    # chunk : tenseur torch [samples] float dans [-1, 1]
                    samples = chunk.clamp(-1.0, 1.0).to("cpu").numpy()
                    audio_int16 = (samples * 32767).astype(np.int16).tobytes()
                    audio_data = await self._resampler.resample(
                        audio_int16, native_rate, self.sample_rate
                    )
                    yield TTSAudioRawFrame(
                        audio=audio_data,
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )
        except Exception as e:
            logger.error(f"Pocket TTS erreur: {e}")
            yield ErrorFrame(error=f"Pocket TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()
