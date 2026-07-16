"""Service TTS Pipecat basé sur Kyutai TTS 1.6B (la voix EXACTE d'unmute.sh).

C'est le grand frère GPU de Pocket TTS : modèle 1.6B (kyutai/tts-1.6b-en_fr),
anglais + français, ~220 ms de latence, streaming natif (Delayed Streams Modeling).
Nécessite un GPU (~5-6 Go VRAM) — pensé pour tourner sur Modal (voir deploy/).

API vérifiée sur moshi.models.tts (paquet moshi 0.2.13) :
    checkpoint = CheckpointInfo.from_hf_repo(repo)
    tts_model  = TTSModel.from_checkpoint_info(checkpoint, n_q=32, temp=..., device=...)
    voice_path = tts_model.get_voice_path(voice)
    cond       = tts_model.make_condition_attributes([voice_path], cfg_coef=2.0)
    entries    = tts_model.prepare_script([texte], padding_between=1)
    tts_model.generate([entries], [cond], on_frame=cb)  # cb reçoit chaque frame mimi
    # frame -> pcm : tts_model.mimi.decode(frame[:, 1:, :]) ; sr = tts_model.mimi.sample_rate
    # (la classe TTSGen n'existe pas dans moshi 0.2.13 ; generate() streame via on_frame)

Le modèle et la voix sont chargés une seule fois par process (singleton).
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

# (tts_model, condition_attributes, native_sample_rate) — chargé paresseusement.
_MODEL_CACHE: tuple | None = None
_MODEL_LOCK = threading.Lock()
_GEN_LOCK: asyncio.Lock | None = None


def _select_device() -> str:
    """cuda si dispo (obligatoire en pratique pour le 1.6B), sinon cpu (très lent)."""
    choice = os.getenv("KYUTAI_TTS_DEVICE", "auto").strip().lower()
    if choice in ("cuda", "gpu"):
        return "cuda"
    if choice == "cpu":
        return "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_model_and_voice():
    """Charge le 1.6B et l'état de voix — EXACTEMENT une fois (double-checked lock)."""
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    with _MODEL_LOCK:
        if _MODEL_CACHE is None:
            from moshi.models.loaders import CheckpointInfo
            from moshi.models.tts import DEFAULT_DSM_TTS_REPO, TTSModel

            device = _select_device()
            repo = os.getenv("KYUTAI_TTS_REPO", "").strip() or DEFAULT_DSM_TTS_REPO
            voice = os.getenv(
                "KYUTAI_TTS_VOICE",
                # Voix par défaut documentée (existe à coup sûr). Pour une voix
                # française native, mettre p.ex. un fichier du dépôt kyutai/tts-voices
                # (voir docs/MODAL.md).
                "expresso/ex03-ex01_happy_001_channel1_334s.wav",
            )
            temp = float(os.getenv("KYUTAI_TTS_TEMP", "0.6"))
            logger.info(
                f"Chargement de Kyutai TTS 1.6B (repo={repo}, voix={voice}, "
                f"device={device})..."
            )
            started = time.monotonic()
            checkpoint_info = CheckpointInfo.from_hf_repo(repo)
            tts_model = TTSModel.from_checkpoint_info(
                checkpoint_info, n_q=32, temp=temp, device=device
            )
            voice_path = tts_model.get_voice_path(voice)
            condition_attributes = tts_model.make_condition_attributes(
                [voice_path], cfg_coef=2.0
            )
            native_rate = int(tts_model.mimi.sample_rate)
            _MODEL_CACHE = (tts_model, condition_attributes, native_rate)
            logger.info(
                f"Kyutai TTS 1.6B prêt ({native_rate} Hz, device={device}) en "
                f"{time.monotonic() - started:.1f}s."
            )
            if device == "cpu":
                logger.warning(
                    "Kyutai 1.6B sur CPU : impraticable (bien trop lent). "
                    "Ce modèle exige un GPU — déployez sur Modal (deploy/modal_app.py)."
                )
    return _MODEL_CACHE


def _prepare_entries(tts_model, text: str):
    """Texte -> liste d'Entry via l'API publique prepare_script (moshi 0.2.13)."""
    return tts_model.prepare_script([text], padding_between=1)


def _gen_lock() -> asyncio.Lock:
    global _GEN_LOCK
    if _GEN_LOCK is None:
        _GEN_LOCK = asyncio.Lock()
    return _GEN_LOCK


class KyutaiTTSService(TTSService):
    """TTS Pipecat utilisant Kyutai TTS 1.6B (GPU, en/fr, la voix d'unmute.sh)."""

    def __init__(self, **kwargs):
        settings = TTSSettings(
            model=None,
            voice=os.getenv("KYUTAI_TTS_VOICE", "expresso"),
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
            tts_model, condition_attributes, native_rate = await asyncio.to_thread(
                _load_model_and_voice
            )

            async with _gen_lock():
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()
                done = object()
                err: dict = {}
                stop_event = threading.Event()

                def _produce():
                    """Génère l'énoncé dans un thread ; le callback on_frame pousse
                    chaque morceau PCM dans la file (le GPU reste alimenté).

                    generate() est un appel bloquant qui invoque on_frame à chaque
                    frame mimi produite. Pour couper net sur un barge-in, on lève
                    _Stopped depuis on_frame -> l'exception remonte hors de generate()."""

                    class _Stopped(Exception):
                        pass

                    def _on_frame(frame):
                        if stop_event.is_set():
                            raise _Stopped()
                        # frame == -1 : pas encore d'audio prêt (padding initial).
                        if (frame != -1).all():
                            pcm = tts_model.mimi.decode(frame[:, 1:, :]).cpu().numpy()
                            samples = np.clip(pcm[0, 0], -1.0, 1.0)
                            audio_int16 = (samples * 32767).astype(np.int16).tobytes()
                            loop.call_soon_threadsafe(
                                queue.put_nowait, (audio_int16, samples.shape[0])
                            )

                    try:
                        entries = _prepare_entries(tts_model, text)
                        tts_model.generate(
                            [entries], [condition_attributes], on_frame=_on_frame
                        )
                    except _Stopped:
                        pass  # interruption (barge-in) demandée : arrêt silencieux
                    except Exception as e:  # remonté côté consommateur
                        err["e"] = e
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, done)

                started = time.monotonic()
                producer = asyncio.create_task(asyncio.to_thread(_produce))
                first = True
                n_chunks = 0
                n_samples = 0
                try:
                    while True:
                        item = await queue.get()
                        if item is done:
                            break
                        audio_int16, n = item
                        if first:
                            await self.stop_ttfb_metrics()
                            logger.info(
                                f"Kyutai 1.6B : 1er chunk en "
                                f"{time.monotonic() - started:.2f}s"
                            )
                            first = False
                        n_chunks += 1
                        n_samples += n
                        audio_data = await self._resampler.resample(
                            audio_int16, native_rate, self.sample_rate
                        )
                        yield TTSAudioRawFrame(
                            audio=audio_data,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                finally:
                    stop_event.set()
                    await producer
                if err:
                    raise err["e"]
                wall = time.monotonic() - started
                audio_sec = n_samples / native_rate if native_rate else 0.0
                rtf = audio_sec / wall if wall else 0.0
                logger.info(
                    f"Kyutai 1.6B : {n_chunks} chunks, {audio_sec:.2f}s d'audio "
                    f"généré en {wall:.2f}s (x{rtf:.2f} temps réel)."
                )
        except Exception as e:
            logger.error(f"Kyutai 1.6B erreur: {e}")
            yield ErrorFrame(error=f"Kyutai 1.6B error: {e}")
        finally:
            await self.stop_ttfb_metrics()
