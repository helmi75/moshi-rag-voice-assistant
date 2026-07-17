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


def _select_device() -> str:
    """Choisit le périphérique d'inférence pour Pocket TTS.

    `POCKET_TTS_DEVICE` : "auto" (défaut) -> "cuda" si un GPU CUDA est visible,
    sinon "cpu" ; ou forcer "cuda"/"cpu". Sur GPU, Pocket TTS génère en temps réel
    (1er chunk < 1 s) — indispensable pour éviter la voix saccadée obtenue sur CPU
    (où `french_24l` met 5-10 s à produire son premier morceau)."""
    choice = os.getenv("POCKET_TTS_DEVICE", "auto").strip().lower()
    if choice in ("cuda", "gpu"):
        return "cuda"
    if choice == "cpu":
        return "cpu"
    # auto : GPU si disponible, sinon CPU.
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


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
            device = _select_device()
            if device != "cpu":
                # TTSModel est un nn.Module : .to() déplace tous ses poids sur le GPU.
                # Fait AVANT get_state_for_audio_prompt pour que l'état de voix vive
                # aussi sur le bon périphérique.
                model = model.to(device)
                logger.info(f"Pocket TTS : modèle déplacé sur {device} (GPU).")
            voice_ref = _resolve_voice(voice)
            base_state = model.get_state_for_audio_prompt(voice_ref)
            _MODEL_CACHE = (model, base_state, int(model.sample_rate))
            logger.info(
                f"Pocket TTS prêt ({_MODEL_CACHE[2]} Hz, device={device}) en "
                f"{time.monotonic() - started:.1f}s."
            )
            if device == "cpu":
                logger.warning(
                    "Pocket TTS tourne sur CPU : la voix française (french_24l) y est "
                    "TROP LENTE (débit < temps réel) et sera SACCADÉE au téléphone. "
                    "Pour le GPU, lancez avec la surcouche : "
                    "docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d. "
                    "Sinon, préférez TTS_PROVIDER=cartesia ou VOICE_MODE=gather."
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
            #
            # PIPELINE : la génération tourne EN CONTINU dans un thread dédié qui
            # pousse les morceaux (déjà convertis en int16) dans une file ; la boucle
            # asyncio consomme en parallèle (rééchantillonnage + envoi). Ainsi le GPU
            # enchaîne les pas sans attendre le traitement aval — sur la version
            # précédente, il redemandait un chunk seulement APRÈS l'envoi du précédent,
            # d'où un GPU à ~30 % et un débit sous le temps réel (voix saccadée).
            async with _gen_lock():
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()
                done = object()
                err: dict = {}
                # Signal d'arrêt anticipé : si l'appelant coupe la parole (barge-in),
                # Pipecat annule run_tts ; on pose ce drapeau pour que le thread de
                # génération s'arrête au pas suivant (~110 ms) au lieu de terminer tout
                # l'énoncé — supprime le « timed out waiting for task to cancel » et la
                # latence d'~1 s avant que le bot se taise.
                stop_event = threading.Event()

                # Mouchards : décomposent le temps pour localiser le goulot.
                # Producteur (thread) : "gen" = pas de génération du modèle (GPU),
                # "conv" = copie GPU->CPU + conversion int16, "wall" = total thread.
                # Consommateur (boucle) : "wait" = temps passé à ATTENDRE un morceau
                # (file vide = producteur trop lent), "resample" = rééchantillonnage 8 kHz.
                prof = {"gen": 0.0, "conv": 0.0, "steps": 0, "wall": 0.0}
                profile_each = os.getenv("POCKET_TTS_PROFILE", "").strip() not in ("", "0")

                def _produce():
                    """Génère tous les morceaux dans un thread, sans rendre la main
                    entre chaque pas — le GPU reste alimenté en permanence."""
                    prod_started = time.monotonic()
                    gen = None
                    try:
                        gen = model.generate_audio_stream(
                            base_state, text, copy_state=True
                        )
                        while not stop_event.is_set():
                            t0 = time.monotonic()
                            try:
                                chunk = next(gen)
                            except StopIteration:
                                break
                            t1 = time.monotonic()
                            # chunk : tenseur torch [samples] float dans [-1, 1].
                            # .to("cpu") force une synchro CUDA (copie GPU->CPU).
                            samples = chunk.clamp(-1.0, 1.0).to("cpu").numpy()
                            audio_int16 = (samples * 32767).astype(np.int16).tobytes()
                            t2 = time.monotonic()
                            prof["gen"] += t1 - t0
                            prof["conv"] += t2 - t1
                            prof["steps"] += 1
                            if profile_each:
                                logger.debug(
                                    f"Pocket TTS pas #{prof['steps']} : "
                                    f"gen {1000 * (t1 - t0):.0f}ms, "
                                    f"conv {1000 * (t2 - t1):.0f}ms, "
                                    f"{samples.shape[0]} éch."
                                )
                            loop.call_soon_threadsafe(
                                queue.put_nowait, (audio_int16, samples.shape[0])
                            )
                    except Exception as e:  # remonté côté consommateur
                        err["e"] = e
                    finally:
                        # Libère le générateur (et son état GPU) sans attendre.
                        if gen is not None:
                            try:
                                gen.close()
                            except Exception:
                                pass
                        prof["wall"] = time.monotonic() - prod_started
                        loop.call_soon_threadsafe(queue.put_nowait, done)

                started = time.monotonic()
                producer = asyncio.create_task(asyncio.to_thread(_produce))
                first = True
                n_chunks = 0
                n_samples = 0
                cons_wait = 0.0
                cons_resample = 0.0
                try:
                    while True:
                        tw = time.monotonic()
                        item = await queue.get()
                        cons_wait += time.monotonic() - tw
                        if item is done:
                            break
                        audio_int16, n = item
                        if first:
                            await self.stop_ttfb_metrics()
                            logger.info(
                                f"Pocket TTS : 1er chunk en "
                                f"{time.monotonic() - started:.2f}s"
                            )
                            first = False
                        n_chunks += 1
                        n_samples += n
                        tr = time.monotonic()
                        audio_data = await self._resampler.resample(
                            audio_int16, native_rate, self.sample_rate
                        )
                        cons_resample += time.monotonic() - tr
                        yield TTSAudioRawFrame(
                            audio=audio_data,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                finally:
                    # Coupe la génération (barge-in / fin normale) puis attend que
                    # le thread se termine — il s'arrête au pas suivant (~110 ms)
                    # grâce à stop_event, au lieu de finir tout l'énoncé.
                    stop_event.set()
                    await producer
                if err:
                    raise err["e"]
                wall = time.monotonic() - started
                audio_sec = n_samples / native_rate if native_rate else 0.0
                rtf = audio_sec / wall if wall else 0.0
                # rtf > 1 = plus rapide que le temps réel (indispensable au téléphone).
                logger.info(
                    f"Pocket TTS : {n_chunks} chunks, {audio_sec:.2f}s d'audio "
                    f"généré en {wall:.2f}s (x{rtf:.2f} temps réel)."
                )
                steps = max(prof["steps"], 1)
                # Décomposition du goulot. Lecture :
                # - producteur(total) ~= mur ET attente file élevée -> génération GPU = goulot
                # - dans le producteur : gen >> conv -> le modèle lui-même (Maxwell lent)
                #                        conv élevé   -> la copie/synchro GPU->CPU
                # - resample élevé (et producteur < mur) -> le rééchantillonnage CPU
                logger.info(
                    f"Pocket TTS profil : [producteur] génération {prof['gen']:.2f}s "
                    f"({1000 * prof['gen'] / steps:.0f}ms/pas) | copie GPU→CPU {prof['conv']:.2f}s "
                    f"| total {prof['wall']:.2f}s || [consommateur] attente file "
                    f"{cons_wait:.2f}s | resample {cons_resample:.2f}s || mur {wall:.2f}s "
                    f"| {steps} pas"
                )
        except Exception as e:
            logger.error(f"Pocket TTS erreur: {e}")
            yield ErrorFrame(error=f"Pocket TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()
