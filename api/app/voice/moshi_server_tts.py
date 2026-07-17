"""Service TTS Pipecat client du serveur Rust `moshi-server` (voix Moshi 1.6B).

C'est LA voie de production de Kyutai (celle d'unmute.sh) : le modèle 1.6B est servi
par le serveur Rust `moshi-server` (CUDA graphs + batching) qui tient le temps réel là
où le chemin PyTorch de référence (kyutai_tts.py) reste sous le temps réel et sacade.

Ici, l'application n'exécute AUCUN modèle : elle est simple cliente websocket du serveur
(déployé sur Modal GPU, voir deploy/modal_moshi_server.py). Le pipeline reste donc léger,
sans torch ni moshi côté app.

Protocole vérifié sur scripts/tts_rust_server.py du dépôt kyutai-labs :
    URI    : {ws_base}/api/tts_streaming?voice=<voix>&format=PcmMessagePack
    header : {"kyutai-api-key": <clé>}          (défaut serveur : "public_token")
    envoi  : msgpack {"type": "Text", "text": mot}  pour chaque mot, puis {"type": "Eos"}
    retour : msgpack {"type": "Audio", "pcm": [float32...]}   à 24000 Hz
Le serveur ferme la connexion quand l'énoncé est terminé (fin de la boucle de réception).
"""
import asyncio
import os
import time
from collections.abc import AsyncGenerator
from urllib.parse import quote

import numpy as np
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

# Voix par défaut : « Développeuse », voix française d'une développeuse de Kyutai
# (site unmute). Timbre naturel, adapté à un accueil pro. Surchargeable via MOSHI_TTS_VOICE.
_DEFAULT_VOICE = "unmute-prod-website/developpeuse-3.wav"
_NATIVE_RATE = 24000  # le serveur renvoie du PCM 24 kHz


def _ws_base() -> str:
    """Base websocket du serveur. Accepte http(s):// ou ws(s):// dans MOSHI_TTS_URL
    et normalise vers ws(s):// (Modal expose en https -> wss)."""
    url = os.getenv("MOSHI_TTS_URL", "ws://127.0.0.1:8080").strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


class MoshiServerTTSService(TTSService):
    """TTS Pipecat client du serveur Rust moshi-server (voix Moshi 1.6B, en/fr)."""

    def __init__(self, **kwargs):
        self._voice = os.getenv("MOSHI_TTS_VOICE", _DEFAULT_VOICE)
        self._api_key = os.getenv("MOSHI_TTS_API_KEY", "public_token")
        settings = TTSSettings(model=None, voice=self._voice, language=None)
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
        # Imports paresseux : l'app reste utilisable (mode gather / autres providers)
        # sans ces dépendances, et les tests peuvent les mocker.
        import msgpack
        import websockets

        try:
            await self.start_ttfb_metrics()
            await self.start_tts_usage_metrics(text)

            uri = (
                f"{_ws_base()}/api/tts_streaming"
                f"?voice={quote(self._voice)}&format=PcmMessagePack"
            )
            headers = {"kyutai-api-key": self._api_key}
            started = time.monotonic()
            first = True
            n_samples = 0

            # open_timeout large : si le serveur Modal a scale-to-zero, la 1re connexion
            # réveille la box GPU + recharge le modèle (~30-60 s). Le défaut (10 s) ferait
            # échouer le handshake au réveil. (La latence perçue du 1er appel est traitée
            # séparément par le greeting pré-rendu + le ping de warmup — voir plan Phase 3.)
            async with websockets.connect(
                uri,
                additional_headers=headers,
                max_size=None,
                open_timeout=float(os.getenv("MOSHI_TTS_OPEN_TIMEOUT", "90")),
            ) as ws:

                async def _send():
                    # Un message par mot, puis Eos (comme le client de référence).
                    for word in text.split():
                        await ws.send(msgpack.packb({"type": "Text", "text": word}))
                    await ws.send(msgpack.packb({"type": "Eos"}))

                send_task = asyncio.create_task(_send())
                try:
                    async for message_bytes in ws:
                        msg = msgpack.unpackb(message_bytes)
                        if msg.get("type") != "Audio":
                            continue
                        pcm = np.clip(
                            np.array(msg["pcm"], dtype=np.float32), -1.0, 1.0
                        )
                        if pcm.size == 0:
                            continue
                        audio_int16 = (pcm * 32767).astype(np.int16).tobytes()
                        if first:
                            await self.stop_ttfb_metrics()
                            logger.info(
                                f"moshi-server : 1er chunk en "
                                f"{time.monotonic() - started:.2f}s"
                            )
                            first = False
                        n_samples += pcm.shape[0]
                        audio_data = await self._resampler.resample(
                            audio_int16, _NATIVE_RATE, self.sample_rate
                        )
                        yield TTSAudioRawFrame(
                            audio=audio_data,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                finally:
                    # Barge-in / fin : on n'attend pas l'envoi restant, on coupe.
                    send_task.cancel()

            wall = time.monotonic() - started
            audio_sec = n_samples / _NATIVE_RATE if n_samples else 0.0
            rtf = audio_sec / wall if wall else 0.0
            logger.info(
                f"moshi-server : {audio_sec:.2f}s d'audio en {wall:.2f}s "
                f"(x{rtf:.2f} temps réel)."
            )
        except Exception as e:
            logger.error(f"moshi-server erreur: {e}")
            yield ErrorFrame(error=f"moshi-server error: {e}")
        finally:
            await self.stop_ttfb_metrics()
