"""Accueil pré-rendu + warmup moshi-server (Phase 3).

Au décroché, le client doit entendre du son IMMÉDIATEMENT. Si le message d'accueil
passait par le TTS live alors que le GPU Modal a scale-to-zero, le client subirait
le blanc du cold start (30-70 s). On rend donc l'accueil UNE fois, hors du chemin
d'appel, dans la voix « Développeuse », on le met en cache sur le volume persistant
(/app/data), et on le rejoue en frames audio brutes (latence 0). En parallèle un
ping de warmup réveille le serveur, pour que la 1re réponse live tombe sur un GPU
déjà (en cours d')allumé.

Le cache est indexé par (voix + texte du greeting) : si l'un change, un nouveau
fichier est rendu et l'ancien est ignoré — pas de resynchronisation manuelle.
"""
import asyncio
import hashlib
import os
import time
import wave
from pathlib import Path
from urllib.parse import quote

import numpy as np
from loguru import logger

from ..tenants import Tenant
from .moshi_server_tts import _DEFAULT_HOLD_MUSIC, _DEFAULT_VOICE, _NATIVE_RATE, _ws_base

# Phrase de reprise après l'attente (vrai standard téléphonique). Surchargeable.
_RESUME_TEXT = "Merci d'avoir patienté, je vous écoute."

# Débit du WAV mis en cache : celui de Twilio (8 kHz), pour être rejoué tel quel
# par le transport sortant sans rééchantillonnage.
TWILIO_RATE = 8000


def is_moshi_server() -> bool:
    return os.getenv("TTS_PROVIDER", "").strip().lower() == "moshi_server"


def _voice() -> str:
    return os.getenv("MOSHI_TTS_VOICE", _DEFAULT_VOICE)


def _api_key() -> str:
    return os.getenv("MOSHI_TTS_API_KEY", "public_token")


def _cache_dir() -> Path:
    return Path(os.getenv("GREETING_CACHE_DIR", "/app/data/greetings"))


def _cache_path(tenant: Tenant) -> Path:
    """Chemin de cache déterministe, invalidé si la voix ou le texte change."""
    key = f"{_voice()}|{tenant.greeting}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:12]
    return _cache_dir() / f"tenant{tenant.id}_{digest}.wav"


def cached_greeting_path(tenant: Tenant) -> Path | None:
    """Retourne le WAV d'accueil déjà en cache, ou None. NE rend RIEN (un rendu à
    froid bloquerait l'accueil ; l'appelant retombe alors sur le TTS live)."""
    path = _cache_path(tenant)
    # 44 octets = en-tête WAV seul : un fichier de cette taille est vide/corrompu.
    if path.exists() and path.stat().st_size > 44:
        return path
    return None


async def _render_pcm(text: str) -> np.ndarray | None:
    """Rend `text` via moshi-server → float32 mono @24 kHz (None si rendu vide)."""
    import msgpack
    import websockets

    uri = (
        f"{_ws_base()}/api/tts_streaming"
        f"?voice={quote(_voice())}&format=PcmMessagePack"
    )
    headers = {"kyutai-api-key": _api_key()}
    chunks: list[np.ndarray] = []
    async with websockets.connect(
        uri,
        additional_headers=headers,
        max_size=None,
        # Large : le 1er accès réveille la box GPU + recharge le modèle (cold start).
        open_timeout=float(os.getenv("MOSHI_TTS_OPEN_TIMEOUT", "90")),
    ) as ws:

        async def _send():
            for word in text.split():
                await ws.send(msgpack.packb({"type": "Text", "text": word}))
            await ws.send(msgpack.packb({"type": "Eos"}))

        send_task = asyncio.create_task(_send())
        try:
            async for message_bytes in ws:
                msg = msgpack.unpackb(message_bytes)
                if msg.get("type") != "Audio":
                    continue
                pcm = np.clip(np.array(msg["pcm"], dtype=np.float32), -1.0, 1.0)
                if pcm.size:
                    chunks.append(pcm)
        finally:
            send_task.cancel()

    return np.concatenate(chunks) if chunks else None


async def _to_twilio_int16(pcm_f32_24k: np.ndarray) -> bytes:
    """24 kHz float32 -> 8 kHz int16 (rééchantillonnage anti-repliement de Pipecat)."""
    from pipecat.audio.utils import create_stream_resampler

    int16_24k = (pcm_f32_24k * 32767).astype(np.int16).tobytes()
    resampler = create_stream_resampler()
    return await resampler.resample(int16_24k, _NATIVE_RATE, TWILIO_RATE)


def _write_wav(path: Path, pcm_int16: bytes) -> None:
    """Écriture atomique (tmp puis rename) pour ne jamais laisser un WAV partiel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TWILIO_RATE)
        w.writeframes(pcm_int16)
    tmp.replace(path)


async def ensure_greeting_wav(tenant: Tenant) -> Path | None:
    """Rend et met en cache le WAV d'accueil s'il manque. Idempotent. None si échec
    (l'appelant retombe alors sur le TTS live)."""
    if not is_moshi_server():
        return None
    existing = cached_greeting_path(tenant)
    if existing:
        return existing
    try:
        t0 = time.monotonic()
        pcm = await _render_pcm(tenant.greeting)
        if pcm is None:
            logger.warning(f"greeting: rendu vide (tenant {tenant.id})")
            return None
        _write_wav(_cache_path(tenant), await _to_twilio_int16(pcm))
        logger.info(
            f"greeting: pré-rendu tenant {tenant.id} "
            f"({pcm.shape[0] / _NATIVE_RATE:.1f}s d'audio) en {time.monotonic() - t0:.1f}s "
            f"→ {_cache_path(tenant).name}"
        )
        return _cache_path(tenant)
    except Exception as exc:
        logger.warning(f"greeting: pré-rendu échoué (repli TTS live), tenant {tenant.id}: {exc}")
        return None


def load_greeting_frames(path: Path, chunk_ms: int = 20) -> list:
    """Charge un WAV mono 16 bits en frames audio brutes prêtes pour le transport."""
    from pipecat.frames.frames import OutputAudioRawFrame

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        data = w.readframes(w.getnframes())
    frame_bytes = int(rate * chunk_ms / 1000) * 2  # 2 octets/échantillon (int16 mono)
    frames = []
    for i in range(0, len(data), frame_bytes):
        chunk = data[i : i + frame_bytes]
        if chunk:
            frames.append(
                OutputAudioRawFrame(audio=chunk, sample_rate=rate, num_channels=1)
            )
    return frames


async def warmup_moshi_server() -> None:
    """Réveille (ou garde chaud) le serveur moshi : mini-requête TTS. Non bloquant,
    erreurs avalées — c'est un préchauffage best-effort, jamais un point de panne."""
    if not is_moshi_server():
        return
    try:
        t0 = time.monotonic()
        await _render_pcm(os.getenv("MOSHI_WARMUP_TEXT", "Bonjour."))
        logger.info(f"warmup moshi-server : OK en {time.monotonic() - t0:.1f}s")
    except Exception as exc:
        logger.warning(f"warmup moshi-server échoué (sans conséquence): {exc}")


def load_hold_music_chunks(chunk_ms: int = 20) -> tuple[int, list[bytes]]:
    """Charge la musique d'attente en morceaux de 20 ms. (rate, [bytes]). ([]) si absente."""
    path = os.getenv("MOSHI_HOLD_MUSIC_PATH", _DEFAULT_HOLD_MUSIC)
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception as exc:
        logger.warning(f"musique d'attente non chargée: {exc}")
        return 8000, []
    step = int(rate * chunk_ms / 1000) * 2  # 2 octets/échantillon (int16 mono)
    return rate, [raw[i : i + step] for i in range(0, len(raw), step) if raw[i : i + step]]


async def run_switchboard_intro(task, output_transport, tenant: Tenant) -> None:
    """Flux « standardiste » (Phase 3) : accueil pré-rendu → musique d'attente pendant
    le réveil du GPU → reprise proactive. Lancé en tâche de fond parallèle au pipeline.

    L'accueil et la musique sont injectés via output_transport.send_audio() : ils vont
    DIRECTEMENT à Twilio sans traverser STT/VAD. Sinon (injectés en tête de pipeline),
    ces AudioRawFrame empoisonnaient Deepgram ET engorgeaient le VAD, bloquant l'audio
    entrant réel après la reprise (« no audio received while speaking »).

    Séquence : « Bonjour … un instant s'il vous plaît » (WAV, latence 0) → 🎻 musique
    (bouclée) tant que le warmup n'a pas rendu le GPU chaud → « Merci d'avoir patienté,
    je vous écoute » (TTS, désormais rapide) → la conversation prend le relais.
    """
    from pipecat.frames.frames import OutputAudioRawFrame, STTMuteFrame, TTSSpeakFrame

    # STT muté pendant l'intro : la parole du client pendant l'attente est ignorée
    # (il est « en ligne d'attente ») ; on écoute à la reprise. La musique, elle, ne
    # passe plus par le STT (send_audio) : le mute ne concerne donc que l'entrée réelle.
    await task.queue_frames([STTMuteFrame(mute=True)])
    try:
        # 1. Accueil pré-rendu → envoyé DIRECT vers la sortie (bypass STT/VAD).
        greeting_path = cached_greeting_path(tenant)
        if greeting_path is not None:
            logger.info(f"Accueil pré-rendu joué depuis {greeting_path.name} (latence 0).")
            for frame in load_greeting_frames(greeting_path):
                await output_transport.send_audio(frame)
        else:
            await task.queue_frames([TTSSpeakFrame(tenant.greeting)])  # repli TTS live

        if is_moshi_server():
            # 2. Réveil du GPU en tâche de fond (retourne quand le serveur est chaud).
            warm = asyncio.create_task(warmup_moshi_server())

            # 3. Musique d'attente jusqu'au réveil, en morceaux de 1 s envoyés DIRECT à la
            #    sortie (pacé : le transport joue à débit réel, on garde un petit tampon).
            rate, chunks = load_hold_music_chunks(chunk_ms=1000)
            deadline = time.monotonic() + float(os.getenv("MOSHI_HOLD_MAX_SECONDS", "90"))
            i = 0
            if chunks:
                logger.info("Musique d'attente : lecture pendant le réveil du GPU.")
                while not warm.done() and time.monotonic() < deadline:
                    await output_transport.send_audio(OutputAudioRawFrame(
                        audio=chunks[i % len(chunks)], sample_rate=rate, num_channels=1))
                    i += 1
                    await asyncio.sleep(0.9)  # < 1 s : petit tampon d'avance anti-coupure
            try:
                await asyncio.wait_for(warm, timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                pass  # warmup KO/expiré : on reprend quand même la main
            logger.info(f"Fin de l'attente (~{i}s de musique jouée) : reprise proactive.")

        # 4. Reprise proactive (TTS désormais chaud → ~1,2 s), via le pipeline normal.
        await task.queue_frames([TTSSpeakFrame(os.getenv("MOSHI_RESUME_TEXT", _RESUME_TEXT))])
    finally:
        # Démute : le client peut désormais parler et être transcrit.
        await task.queue_frames([STTMuteFrame(mute=False)])


async def keep_warm_loop() -> None:
    """Boucle de préchauffage périodique (opt-in via MOSHI_KEEPWARM_SECONDS > 0).

    Le cold start (55-70 s) est plus long que tout accueil : seul un GPU maintenu
    chaud le supprime vraiment. Utile pour une démo à faible trafic. ⚠️ COÛTE de
    l'argent (empêche le scale-to-zero du GPU). Régler l'intervalle sous le
    scaledown Modal (défaut 120 s) — p. ex. 90 s."""
    interval = float(os.getenv("MOSHI_KEEPWARM_SECONDS", "0") or "0")
    if interval <= 0 or not is_moshi_server():
        return
    logger.info(f"keep-warm moshi-server activé (toutes les {interval:.0f}s).")
    while True:
        await warmup_moshi_server()
        await asyncio.sleep(interval)
