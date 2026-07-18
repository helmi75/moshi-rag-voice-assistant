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
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
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

# Musique d'attente (Phase 3) : jouée si la 1re réponse tarde (cold start du GPU),
# pour ne jamais laisser de blanc. Embarquée dans l'image (cf. Dockerfile ./app).
_DEFAULT_HOLD_MUSIC = str(Path(__file__).resolve().parent.parent / "assets" / "hold_music.wav")


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
        # Frames de musique d'attente (chargées à la 1re utilisation) : None = pas
        # encore tenté, [] = tenté mais indisponible (silence pendant l'attente).
        self._hold_frames: list[bytes] | None = None

    def can_generate_metrics(self) -> bool:
        return True

    async def _hold_music_frames(self) -> list[bytes] | None:
        """Charge (une fois) la musique d'attente en frames de 20 ms au débit du
        pipeline. None si aucune n'est disponible (le remplissage est alors désactivé)."""
        if self._hold_frames is not None:
            return self._hold_frames or None
        frames: list[bytes] = []
        path = os.getenv("MOSHI_HOLD_MUSIC_PATH", _DEFAULT_HOLD_MUSIC)
        try:
            with wave.open(path, "rb") as w:
                rate = w.getframerate()
                raw = w.readframes(w.getnframes())
            if rate != self.sample_rate:
                # Rééchantillonneur dédié : ne pas polluer l'état de self._resampler
                # (qui traite le flux audio réel à 24 kHz).
                raw = await create_stream_resampler().resample(raw, rate, self.sample_rate)
                rate = self.sample_rate
            step = int(rate * 0.02) * 2  # 20 ms, 2 octets/échantillon (int16 mono)
            frames = [raw[i : i + step] for i in range(0, len(raw), step) if raw[i : i + step]]
        except FileNotFoundError:
            logger.info(
                "Pas de musique d'attente (fichier absent) : silence pendant un cold start."
            )
        except Exception as exc:
            logger.warning(f"musique d'attente non chargée: {exc}")
        self._hold_frames = frames
        return frames or None

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Génération TTS [{text}]")
        # Imports paresseux : l'app reste utilisable (mode gather / autres providers)
        # sans ces dépendances, et les tests peuvent les mocker.
        import msgpack
        import websockets

        uri = (
            f"{_ws_base()}/api/tts_streaming"
            f"?voice={quote(self._voice)}&format=PcmMessagePack"
        )
        headers = {"kyutai-api-key": self._api_key}
        started = time.monotonic()
        first = True
        n_samples = 0

        # Producteur : établit la connexion (peut bloquer ~50 s au cold start Modal :
        # la 1re connexion réveille la box GPU et recharge le modèle), envoie le texte
        # mot à mot puis Eos, et pousse chaque chunk PCM dans une file. Le consommateur
        # (boucle ci-dessous) draine la file ; tant qu'aucun audio réel n'est arrivé au
        # bout de MOSHI_HOLD_AFTER_SECONDS, il meuble avec la musique d'attente — le
        # client n'entend jamais de blanc, même GPU froid.
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def _produce():
            try:
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
                            if pcm.size:
                                await queue.put(pcm)
                    finally:
                        # Barge-in / fin : on n'attend pas l'envoi restant, on coupe.
                        send_task.cancel()
            except Exception as exc:  # remontée au consommateur pour l'ErrorFrame
                await queue.put(exc)
            finally:
                await queue.put(_DONE)

        producer = asyncio.create_task(_produce())
        # Seuil élevé : le flux « standardiste » (voice/greeting.py) gère déjà la musique
        # pendant le cold start. Ce filet dans run_tts ne doit se déclencher qu'en secours
        # profond (serveur réellement froid en pleine conversation), jamais sur une réponse
        # un peu lente (~2 s) — sinon micro-blip de musique au milieu d'une phrase.
        hold_after = float(os.getenv("MOSHI_HOLD_AFTER_SECONDS", "8"))
        hold_frames = await self._hold_music_frames()
        hold_i = 0
        got_real = False
        error: Exception | None = None
        try:
            await self.start_ttfb_metrics()
            await self.start_tts_usage_metrics(text)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.02)
                except asyncio.TimeoutError:
                    # Rien encore : meuble avec la musique d'attente (1 frame de 20 ms
                    # par itération ≈ temps réel) tant que le 1er audio réel n'est pas là.
                    if (
                        not got_real
                        and hold_frames
                        and (time.monotonic() - started) >= hold_after
                    ):
                        yield TTSAudioRawFrame(
                            audio=hold_frames[hold_i % len(hold_frames)],
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                        hold_i += 1
                    continue
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    error = item
                    break
                pcm = item  # np.float32 @ 24 kHz : audio réel
                if first:
                    await self.stop_ttfb_metrics()
                    logger.info(
                        f"moshi-server : 1er chunk en {time.monotonic() - started:.2f}s"
                    )
                    first = False
                got_real = True
                n_samples += pcm.shape[0]
                audio_int16 = (pcm * 32767).astype(np.int16).tobytes()
                audio_data = await self._resampler.resample(
                    audio_int16, _NATIVE_RATE, self.sample_rate
                )
                yield TTSAudioRawFrame(
                    audio=audio_data,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            if error is not None:
                raise error
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
            producer.cancel()
            await self.stop_ttfb_metrics()
