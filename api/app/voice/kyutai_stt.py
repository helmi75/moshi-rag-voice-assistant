"""Service STT Pipecat client du module ASR de `moshi-server` (Kyutai stt-1b-en_fr).

Pendant STT du client TTS `moshi_server_tts.py` : l'application est simple cliente
websocket du MÊME serveur Rust (déployé sur Modal GPU, voir deploy/modal_moshi_server.py),
qui sert l'ASR sur /api/asr-streaming à côté du TTS sur /api/tts_streaming. Français natif,
ponctuation, VAD sémantique embarquée — remplace Deepgram (transcriptions parfois farfelues
sur le 8 kHz téléphone, coût, fournisseur externe de plus).

Protocole ASR vérifié (kyutai-labs/delayed-streams-modeling, moshi rust/moshi-server/asr.rs,
unmute) :
    URI    : {ws_base}/api/asr-streaming
    header : {"kyutai-api-key": <clé>}                 (défaut serveur : "public_token")
    envoi  : msgpack {"type": "Audio", "pcm": [f32...]}  24 kHz mono, chunks de 1920 (80 ms)
             {"type": "Marker", "id": n}  (fence de flush de fin de tour)
             packb DOIT utiliser use_single_float=True (Vec<f32> côté Rust) + use_bin_type=True
    retour : {"type":"Ready"}
             {"type":"Word","text","start_time"}   (mots FINALISÉS, pas d'hypothèses)
             {"type":"EndWord","stop_time"}
             {"type":"Step","step_idx","prs":[...]} (prs = probas de pause de la VAD sémantique)
             {"type":"Marker","id"}                 (écho : tout l'audio d'avant est transcrit)
             {"type":"Error","message"}

Le modèle a un délai de ~0,5 s (asr_delay_in_tokens=6) : les mots arrivent en retard sur
l'audio. La fin de tour est gérée nativement par Pipecat (smart-turn v3 attend le transcript
finalisé après COMPLETE). On finalise donc via request_finalize()/confirm_finalize() : sur
VADUserStoppedSpeakingFrame on envoie un Marker + du silence (draine le délai plus vite que
le temps réel sur GPU) ; à l'écho du Marker on pousse un TranscriptionFrame finalisé qui
déclenche l'inférence immédiatement.

Phase 1 : le turn-taking reste piloté par Silero + smart-turn v3 (inchangé vs Deepgram) ;
les probas `prs` de la VAD sémantique ne sont que journalisées (observabilité) en vue d'une
phase 2 optionnelle (fin de tour prédictive façon unmute).
"""
import asyncio
import os
import time
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

_NATIVE_RATE = 24000  # le module ASR attend du PCM 24 kHz mono
_FRAME = 1920  # 80 ms à 24 kHz : taille de chunk canonique côté Kyutai


def _stt_ws_base() -> str:
    """Base websocket du module ASR. MOSHI_STT_URL, à défaut MOSHI_TTS_URL (même serveur),
    à défaut le serveur local. Accepte http(s):// ou ws(s):// et normalise vers ws(s)://."""
    url = (
        os.getenv("MOSHI_STT_URL")
        or os.getenv("MOSHI_TTS_URL")
        or "ws://127.0.0.1:8080"
    ).strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


class KyutaiSTTService(STTService):
    """STT Pipecat client du module ASR de moshi-server (Kyutai stt-1b-en_fr, streaming continu)."""

    def __init__(self, *, language: Language = Language.FR, **kwargs):
        # ttfs_p99_latency : diffusé via STTMetadataFrame au démarrage ; sert de filet de
        # timeout (max(0, ttfs - stop_secs)) à la stratégie de fin de tour smart-turn v3 en
        # attendant le transcript finalisé. ~1 s couvre le délai modèle (0,5 s) + le flush.
        kwargs.setdefault("ttfs_p99_latency", float(os.getenv("MOSHI_STT_TTFS_P99", "1.0")))
        super().__init__(**kwargs)
        self._language = language
        self._api_key = os.getenv("MOSHI_STT_API_KEY") or os.getenv("MOSHI_TTS_API_KEY", "public_token")
        self._open_timeout = float(os.getenv("MOSHI_STT_OPEN_TIMEOUT", "90"))
        # Keepalive : Twilio streame l'audio en continu pendant l'appel, SAUF durant l'accueil
        # muet (STTMuteFrame, jusqu'à ~90 s). On envoie alors un peu de silence pour empêcher le
        # serveur de fermer la session ASR restée sans audio. Ne se déclenche qu'à l'inactivité.
        self._keepalive_interval = float(os.getenv("MOSHI_STT_KEEPALIVE_INTERVAL", "5"))
        self._keepalive_after = float(os.getenv("MOSHI_STT_KEEPALIVE_AFTER", "2"))

        self._resampler = create_stream_resampler()
        self._resample_buf = np.empty(0, dtype=np.float32)  # reste 24 kHz entre appels
        self._ws = None
        self._conn_task: "asyncio.Task | None" = None
        self._ready = asyncio.Event()
        self._last_send = 0.0
        # Accumulation des mots du tour courant + suivi du Marker de flush en attente.
        self._words: list[str] = []
        self._marker_id = 0
        self._pending_marker: "int | None" = None

    def can_generate_metrics(self) -> bool:
        return True

    def _ws_uri_and_headers(self) -> tuple[str, dict]:
        return f"{_stt_ws_base()}/api/asr-streaming", {"kyutai-api-key": self._api_key}

    # ---- Cycle de vie -----------------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)  # fixe self._sample_rate depuis audio_in_sample_rate
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def _connect(self):
        if self._conn_task is None:
            self._conn_task = self.create_task(self._connection_handler())

    async def _disconnect(self):
        # Annuler la tâche de connexion suffit : son `async with websockets.connect`
        # ferme le websocket et son `finally` arrête le keepalive. On l'annule AVANT de
        # toucher au ws pour éviter une reconnexion pendant l'arrêt.
        self._ready.clear()
        self._ws = None
        if self._conn_task is not None:
            await self.cancel_task(self._conn_task)
            self._conn_task = None

    async def _connection_handler(self):
        """Ouvre le websocket ASR et lit les transcripts, avec reconnexion automatique.

        La 1re connexion peut bloquer jusqu'à ~90 s au cold start Modal (réveil GPU +
        chargement du modèle). La boucle `while True` reconnecte après une coupure ;
        elle sort proprement quand la tâche est annulée (stop/cancel)."""
        import msgpack
        import websockets

        uri, headers = self._ws_uri_and_headers()
        while True:
            keepalive = None
            try:
                async with websockets.connect(
                    uri, additional_headers=headers, max_size=None,
                    open_timeout=self._open_timeout,
                ) as ws:
                    self._ws = ws
                    self._ready.set()
                    keepalive = self.create_task(self._keepalive_handler())
                    logger.info("Kyutai STT : session ASR ouverte.")
                    async for message in ws:
                        await self._on_message(msgpack.unpackb(message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Kyutai STT : session perdue, reconnexion… ({exc})")
            finally:
                self._ready.clear()
                self._ws = None
                if keepalive is not None:
                    await self.cancel_task(keepalive)

    async def _keepalive_handler(self):
        """Envoie un peu de silence si aucun audio n'a été transmis récemment (accueil muet)."""
        while True:
            await asyncio.sleep(self._keepalive_interval)
            if self._ws is None:
                continue
            if (time.monotonic() - self._last_send) < self._keepalive_after:
                continue
            try:
                await self._send_pcm(np.zeros(_FRAME, dtype=np.float32))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Kyutai STT : keepalive raté ({exc})")

    # ---- Émission audio ---------------------------------------------------------------

    async def _send_pcm(self, samples: np.ndarray) -> None:
        """Envoie un bloc de float32 24 kHz au serveur en msgpack Audio."""
        import msgpack

        ws = self._ws
        if ws is None:
            return
        await ws.send(msgpack.packb(
            {"type": "Audio", "pcm": samples.astype(np.float32).tolist()},
            use_single_float=True, use_bin_type=True,
        ))
        self._last_send = time.monotonic()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Rééchantillonne l'audio du pipeline (8 kHz) vers 24 kHz et l'envoie au module ASR.

        Les transcripts remontent par la tâche de réception (pas ici), comme Deepgram."""
        if self._ws is None:
            yield None
            return
        try:
            # bytes int16 @ sample_rate pipeline -> bytes int16 @ 24 kHz (resampler streaming).
            resampled = await self._resampler.resample(audio, self.sample_rate, _NATIVE_RATE)
            pcm = np.frombuffer(resampled, dtype=np.int16).astype(np.float32) / 32768.0
            buf = np.concatenate((self._resample_buf, pcm)) if self._resample_buf.size else pcm
            # Envoie par trames de 1920 ; conserve le reste pour la continuité.
            n = (len(buf) // _FRAME) * _FRAME
            for i in range(0, n, _FRAME):
                await self._send_pcm(buf[i:i + _FRAME])
            self._resample_buf = buf[n:]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Kyutai STT : envoi audio échoué, reconnexion ({exc}).")
            self._ws = None  # force la boucle de reconnexion
        yield None

    # ---- Fin de tour (flush) ----------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # La base a déjà routé l'audio/le mute/les VAD frames. En fin de tour utilisateur,
        # on flushe : Marker + silence pour drainer le délai modèle, et on demande la
        # finalisation (le prochain TranscriptionFrame poussé sera marqué finalized=True).
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._ws is not None:
            await self._flush_turn()

    async def _flush_turn(self):
        import msgpack

        ws = self._ws
        if ws is None:
            return
        self._marker_id += 1
        self._pending_marker = self._marker_id
        self.request_finalize()
        try:
            await ws.send(msgpack.packb({"type": "Marker", "id": self._marker_id}, use_single_float=True))
            # ~0,8 s de zéros : fait avancer le modèle au-delà du Marker (il ne le renvoie
            # qu'une fois l'audio d'avant transcrit, délai compris). Le GPU draine > temps réel.
            for _ in range(10):
                await self._send_pcm(np.zeros(_FRAME, dtype=np.float32))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Kyutai STT : flush échoué ({exc}).")
            self._ws = None

    # ---- Réception --------------------------------------------------------------------

    async def _on_message(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "Word":
            text = msg.get("text", "")
            if text:
                self._words.append(text)
                # Interim cumulatif : alimente la détection de tour et donne de la visibilité.
                await self.push_frame(InterimTranscriptionFrame(
                    " ".join(self._words), self._user_id, time_now_iso8601(), self._language,
                ))
        elif mtype == "Marker":
            if self._pending_marker is not None and msg.get("id") == self._pending_marker:
                self._pending_marker = None
                await self._finalize()
        elif mtype == "Step":
            prs = msg.get("prs")
            if prs:
                # Observabilité (phase 2) : prs[2] = proba de pause ~2 s (indice unmute).
                logger.trace(f"Kyutai STT VAD prs={[round(p, 2) for p in prs]}")
        elif mtype == "Error":
            logger.warning(f"Kyutai STT : erreur serveur : {msg.get('message')}")
        elif mtype == "Ready":
            logger.debug("Kyutai STT : Ready.")

    async def _finalize(self):
        """Pousse le transcript finalisé du tour (déclenche l'inférence immédiatement)."""
        text = " ".join(self._words).strip()
        self._words = []
        if not text:
            return
        self.confirm_finalize()  # -> push_frame marquera finalized=True
        await self.push_frame(TranscriptionFrame(
            text, self._user_id, time_now_iso8601(), self._language,
        ))
        logger.info(f"Kyutai STT : transcript finalisé [{text}]")
