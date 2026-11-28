"""Accueil pré-rendu (Voxtral).

Au décroché, le client doit entendre du son IMMÉDIATEMENT. On rend donc l'accueil UNE
fois, hors du chemin d'appel, dans la voix de l'établissement, on le met en cache sur
le volume persistant (/app/data), et on le rejoue en frames audio brutes (latence 0).

Le cache est indexé par (voix + texte du greeting) : si l'un change, un nouveau
fichier est rendu et l'ancien est ignoré — pas de resynchronisation manuelle.

Depuis le retrait de Moshi (ASSISTANTE-103), il n'y a plus de GPU à réveiller, de
musique d'attente ni de message « rappelez dans quelques minutes » pré-rendu : sans clé
Mistral, l'appel est rendu au restaurant (renvoi).
"""
import asyncio
import hashlib
import os
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

from ..tenants import Tenant
from . import voices

# Débit de la synthèse Voxtral (PCM flottant) avant conversion pour Twilio.
_NATIVE_RATE = 24000

# Ce que dit l'assistante, sans le modèle, après l'accueil. Les appels d'avant le retrait
# de Moshi portent aussi « Merci d'avoir patienté… » (après la musique d'attente) : la
# supervision doit continuer à les reconnaître comme du décor.
_REPRISE = "Je vous écoute."
REPRISES_HISTORIQUES = ("Merci d'avoir patienté, je vous écoute.",
                        "Toutes nos excuses, nous rencontrons un petit souci technique. "
                        "Merci de nous rappeler dans quelques minutes. Au revoir.")

# Avance de la reprise sur la fin de l'accueil : le temps que la synthèse rende sa
# première syllabe (≈ 610 ms mesurés, journal de bord de l'appel 39). Lancée plus tard,
# elle laisserait un blanc ; plus tôt, elle chevaucherait l'accueil.
_AVANCE_REPRISE = 0.6


def texte_de_reprise() -> str:
    """Ce que dit l'assistante pour passer la main. Une seule source, lue aussi par la
    supervision : la voix et le décor reconnu ne divergent jamais."""
    return _REPRISE


# Débit du WAV mis en cache : celui de Twilio (8 kHz), pour être rejoué tel quel
# par le transport sortant sans rééchantillonnage.
TWILIO_RATE = 8000


def _texte(tenant: Tenant) -> str:
    """Accueil + mention d'information (#22). Un seul point d'entrée : l'accueil ne doit
    jamais être prononcé sans la mention, quel que soit le chemin emprunté."""
    from ..rgpd import accueil

    return accueil(tenant)


def pre_rendu_possible(tenant: Optional[Tenant] = None) -> bool:
    """Peut-on rendre l'accueil de cet établissement ? Seulement si la clé Mistral est
    posée : sans elle, rien ne se dit (la supervision le signale en panne)."""
    return voices.voxtral_disponible()


def _voice(tenant: Optional[Tenant] = None) -> str:
    """Voix de cet établissement (voix par défaut du parc si rien n'est choisi)."""
    return voices.resolve(tenant)


def _cache_dir() -> Path:
    return Path(os.getenv("GREETING_CACHE_DIR", "/app/data/greetings"))


def _cache_path(tenant: Tenant) -> Path:
    """Chemin de cache déterministe, invalidé si la voix ou le texte change.

    C'est ce qui rend le changement de voix sûr : le WAV rendu dans l'ancienne voix
    n'est plus jamais retrouvé, donc jamais rejoué par-dessus la nouvelle."""
    return _chemin_en_cache(tenant, _texte(tenant))


def _chemin_en_cache(tenant: Tenant, texte: str) -> Path:
    key = f"{_voice(tenant)}|{texte}".encode("utf-8")
    digest = hashlib.sha1(key, usedforsecurity=False).hexdigest()[:12]  # nosemgrep: insecure-hash-algorithm-sha1 — empreinte de cache, pas de sécurité
    return _cache_dir() / f"tenant{tenant.id}_{digest}.wav"


def _si_valide(path: Path) -> Path | None:
    # 44 octets = en-tête WAV seul : un fichier de cette taille est vide/corrompu.
    if path.exists() and path.stat().st_size > 44:
        return path
    return None


def cached_greeting_path(tenant: Tenant) -> Path | None:
    """Retourne le WAV d'accueil déjà en cache, ou None. NE rend RIEN (un rendu à
    froid bloquerait l'accueil ; l'appelant retombe alors sur le TTS live)."""
    return _si_valide(_cache_path(tenant))


async def _render_pcm(text: str, voice: Optional[str] = None) -> np.ndarray | None:
    """Rend `text` → float32 mono @24 kHz (None si rendu vide) par Voxtral (Mistral). La
    voix passée est toujours résolue par `voices.resolve` : une ancienne voix Moshi y
    devient la voix par défaut."""
    from .voxtral_tts import rendre_pcm

    voix = voices.get(voices.resolve_id(voice))
    if voix is None:
        raise ValueError("voix par défaut absente du catalogue Mistral")
    return await rendre_pcm(text, voix.voxtral_id)


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
    """Rend et met en cache le WAV d'accueil s'il manque. Idempotent. Rend son chemin,
    None si échec (l'appelant retombe alors sur le TTS live)."""
    if not pre_rendu_possible(tenant):
        return None
    return cached_greeting_path(tenant) or await _rendre_en_cache(
        tenant, _texte(tenant), _cache_path(tenant), "accueil")


async def _rendre_en_cache(tenant: Tenant, texte: str, chemin: Path, quoi: str) -> Path | None:
    try:
        t0 = time.monotonic()
        pcm = await _render_pcm(texte, _voice(tenant))
        if pcm is None:
            logger.warning(f"greeting: rendu vide ({quoi}, tenant {tenant.id})")
            return None
        _write_wav(chemin, await _to_twilio_int16(pcm))
        logger.info(
            f"greeting: {quoi} pré-rendu, tenant {tenant.id}, en voix « {_voice(tenant)} » "
            f"({pcm.shape[0] / _NATIVE_RATE:.1f}s d'audio) en {time.monotonic() - t0:.1f}s "
            f"→ {chemin.name}"
        )
        return chemin
    except Exception as exc:
        logger.warning(f"greeting: pré-rendu échoué ({quoi}), tenant {tenant.id}: {exc}")
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


async def run_switchboard_intro(
    task, output_transport, tenant: Tenant, pipeline_ready: Optional[asyncio.Event] = None,
) -> None:
    """Flux « standardiste » : accueil pré-rendu, puis « Je vous écoute. » par le pipeline.
    Lancé en tâche de fond parallèle au pipeline.

    ⚠️ `pipeline_ready` : le transport de sortie JETTE l'audio reçu avant le StartFrame.
    Sans cette attente, l'accueil (4 s envoyées d'un bloc à t=0) part dans le vide et
    le client n'entend rien (bug constaté en appel réel : accueil envoyé à t+0,71 s,
    pipeline prêt à t+0,99 s).

    L'accueil est injecté via output_transport.send_audio() : il va DIRECTEMENT à Twilio
    sans traverser STT/VAD. Sinon (injecté en tête de pipeline), ces AudioRawFrame
    empoisonnaient Deepgram ET engorgeaient le VAD, bloquant l'audio entrant réel après
    la reprise (« no audio received while speaking »).

    « Je vous écoute. » s'enchaîne sur la fin de l'accueil (`_AVANCE_REPRISE`).
    """
    from pipecat.frames.frames import STTMuteFrame, TTSSpeakFrame

    # STT muté pendant l'intro : la parole du client pendant l'accueil est ignorée ;
    # on écoute à la reprise.
    await task.queue_frames([STTMuteFrame(mute=True)])

    # Attend le démarrage effectif du pipeline avant d'émettre le moindre audio.
    if pipeline_ready is not None:
        try:
            await asyncio.wait_for(pipeline_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Pipeline pas signalé prêt après 5 s : accueil envoyé quand même.")
    try:
        # 1. Accueil pré-rendu → envoyé DIRECT vers la sortie (bypass STT/VAD).
        greeting_path = cached_greeting_path(tenant)
        debut = time.monotonic()
        duree_accueil = 0.0
        if greeting_path is not None:
            logger.info(f"Accueil pré-rendu joué depuis {greeting_path.name} (latence 0).")
            frames = load_greeting_frames(greeting_path)
            duree_accueil = len(frames) * 0.02  # trames de 20 ms
            for frame in frames:
                await output_transport.send_audio(frame)
        else:
            await task.queue_frames([TTSSpeakFrame(_texte(tenant))])  # repli TTS live

        # `send_audio` met en file et rend la main aussitôt : l'accueil est encore en
        # train d'être joué. On attend sa fin, moins le temps de rendu de la reprise.
        reste = debut + duree_accueil - _AVANCE_REPRISE - time.monotonic()
        if reste > 0:
            await asyncio.sleep(reste)

        # 2. Reprise, via le pipeline normal.
        await task.queue_frames([TTSSpeakFrame(texte_de_reprise())])
    finally:
        # Démute : le client peut désormais parler et être transcrit.
        await task.queue_frames([STTMuteFrame(mute=False)])
