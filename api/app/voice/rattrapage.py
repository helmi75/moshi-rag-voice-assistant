"""La parole entendue par le VAD mais jamais transcrite : la rattraper, ou le dire.

Relevé le 27/09/2026 sur 19 appels de test (183 à 201) : 27 fois « Vous êtes toujours
là ? ». Dans 17 cas, l'appelant AVAIT répondu — le VAD l'a entendu, en général une
seconde après la question — mais aucun mot n'est arrivé du STT. Huit secondes plus tard,
la relance d'inactivité partait, et l'appelant devait tout redire. Réécoutées, ces
réponses sont nettes : « 20 heures, 20 heures », « Ouais ouais ». Envoyées seules à
Deepgram (transcription d'un fichier, pas du flux), elles sont transcrites avec 0,97 de
confiance. Rejouées dans le flux en direct, avec les réglages de production, certaines
le sont aussi, d'autres non : la perte ne se reproduit pas à coup sûr, et sa cause n'est
pas établie. On ne la corrige donc pas à la source ; on la rattrape.

Ce processeur se place juste après le STT. Il garde les dernières secondes d'audio de
l'appelant. Quand un segment de voix se termine sans qu'AUCUN mot (même provisoire) ne
soit arrivé, il attend un court instant une transcription tardive, puis envoie ce
segment seul à Deepgram. Un texte revient : il le pousse comme l'aurait fait le STT, et
la conversation reprend comme si de rien n'était. Rien ne revient : l'assistante dit
« Pardon, je n'ai pas bien entendu », tout de suite — et non « Vous êtes toujours là ? »
huit secondes plus tard, à quelqu'un qui vient de parler.

Prudence délibérée : rien n'est poussé pendant que l'assistante parle, ni pendant un
tour de parole déjà ouvert (le STT y travaille). Une réponse mal rattrapée coûte une
question ; une réponse injectée par-dessus l'assistante coûterait la conversation.
"""
import asyncio
import io
import os
import time
import wave
from collections import deque
from typing import Awaitable, Callable, Optional

import httpx
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601

# Le temps laissé au STT pour rendre une transcription tardive avant de rattraper. Une
# finale arrive d'ordinaire 0,1 à 0,6 s après la fin de la voix (mesuré le 27/09/2026) ;
# le VAD ne signale cette fin qu'après VAD_STOP_SECS (0,5 s) de silence.
ATTENTE_SECONDES = 0.8
# En dessous, c'est un souffle ou un bruit de ligne : rien à rattraper.
DUREE_MIN_SECONDES = 0.3
# « Pardon, je n'ai pas bien entendu » seulement pour une vraie prise de parole, et pas
# deux fois de suite à moins de 12 s : sur une ligne bruyante, la même phrase en boucle
# serait pire que le silence.
DUREE_MIN_PARDON_SECONDES = 0.6
ESPACEMENT_PARDON_SECONDES = 12.0
# Ce qui précède le début de voix signalé par le VAD : il ne le signale qu'après
# VAD_START_SECS de voix, et une attaque coupée fait perdre le premier mot.
AVANT_SECONDES = 0.4
MEMOIRE_SECONDES = 20.0
DELAI_TRANSCRIPTION_SECONDES = 3.0
CONFIANCE_MIN = 0.5

PARDON = {
    "fr": "Pardon, je n'ai pas bien entendu. Vous pouvez répéter ?",
    "en": "Sorry, I didn't catch that. Could you say it again?",
}


def pardon(langue: Optional[str]) -> str:
    return PARDON.get((langue or "").split("-")[0].lower(), PARDON["fr"])


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as fichier:
        fichier.setnchannels(1)
        fichier.setsampwidth(2)
        fichier.setframerate(sample_rate)
        fichier.writeframes(pcm)
    return tampon.getvalue()


async def transcrire_deepgram(pcm: bytes, sample_rate: int, langue: str,
                              vocabulaire: Optional[list[str]] = None) -> Optional[tuple[str, float]]:
    """Le segment, transcrit comme un fichier : (texte, confiance), ou None.

    Même modèle et même vocabulaire que le flux. Ne lève jamais : un rattrapage raté
    se termine en « Pardon, je n'ai pas bien entendu », pas en appel cassé."""
    cle = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not cle or not pcm:
        return None
    params: list[tuple[str, str]] = [
        ("model", os.getenv("DEEPGRAM_MODEL", "nova-3").strip() or "nova-3"),
        ("language", langue or "multi"),
        ("smart_format", "true"),
        ("profanity_filter", "false"),
    ]
    params += [("keyterm", mot) for mot in (vocabulaire or [])]
    try:
        async with httpx.AsyncClient(timeout=DELAI_TRANSCRIPTION_SECONDES) as client:
            reponse = await client.post(
                "https://api.deepgram.com/v1/listen", params=params,
                headers={"Authorization": f"Token {cle}", "Content-Type": "audio/wav"},
                content=_wav(pcm, sample_rate))
        reponse.raise_for_status()
        alternative = reponse.json()["results"]["channels"][0]["alternatives"][0]
        texte = (alternative.get("transcript") or "").strip()
        return (texte, float(alternative.get("confidence") or 0)) if texte else None
    except Exception as exc:
        logger.warning(f"rattrapage de parole : transcription impossible ({exc})")
        return None


class RattrapageDeParole(FrameProcessor):
    """Entre le STT et la suite : voit l'audio et les transcriptions descendre, les
    signaux du VAD et de l'assistante remonter. Ne retient ni ne modifie rien.

    `transcrire(pcm, sample_rate, langue)` : la transcription de secours.
    `langue()` : la langue que le STT utilise en ce moment (« fr », « multi »…).
    `pardonner()` : fait dire « Pardon, je n'ai pas bien entendu » dans la langue de
    l'appel (hors modèle, comme les relances).
    `noter(quoi, **details)` : le journal de bord."""

    def __init__(self, *, transcrire: Callable[[bytes, int, str], Awaitable[Optional[tuple[str, float]]]],
                 langue: Callable[[], str], pardonner: Callable[[], Awaitable[None]],
                 noter: Optional[Callable[..., None]] = None,
                 horloge: Callable[[], float] = time.monotonic, **kwargs):
        super().__init__(**kwargs)
        self._transcrire = transcrire
        self._langue = langue
        self._pardonner = pardonner
        self._noter = noter or (lambda *a, **k: None)
        self._horloge = horloge
        self._audio: deque = deque()          # (instant de fin du morceau, pcm)
        self._sample_rate = 8000
        self._debut: Optional[float] = None   # début du segment sans mots en cours
        self._mots = False                    # un mot (même provisoire) depuis ce début
        self._bot_parle = False
        self._tour_ouvert = False
        self._tache: Optional[asyncio.Task] = None
        self._dernier_pardon = -ESPACEMENT_PARDON_SECONDES
        self.pardons_de_suite = 0
        self._bot_arrete_a: Optional[float] = None
        self._voix_a: Optional[float] = None

    def voix_depuis_le_bot(self) -> bool:
        """L'appelant a-t-il fait entendre sa voix depuis que l'assistante s'est tue ?
        Si oui, un silence de huit secondes n'est pas une absence : il a parlé, on ne
        l'a pas compris."""
        return self._voix_a is not None and (
            self._bot_arrete_a is None or self._voix_a > self._bot_arrete_a)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        try:
            await self._regarder(frame)
        except Exception as exc:
            logger.warning(f"rattrapage de parole : {exc}")

    async def _regarder(self, frame: Frame) -> None:
        maintenant = self._horloge()
        if isinstance(frame, InputAudioRawFrame):
            self._sample_rate = frame.sample_rate or self._sample_rate
            self._audio.append((maintenant, frame.audio))
            while self._audio and maintenant - self._audio[0][0] > MEMOIRE_SECONDES:
                self._audio.popleft()
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if (getattr(frame, "text", "") or "").strip():
                self._mots = True
                self._debut = None
                self._annuler()
                if isinstance(frame, TranscriptionFrame) and not isinstance(frame, InterimTranscriptionFrame):
                    self.pardons_de_suite = 0
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._annuler()
            if self._debut is None:
                attaque = float(getattr(frame, "start_secs", 0) or 0)
                self._debut = maintenant - attaque - AVANT_SECONDES
                self._mots = False
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            silence = float(getattr(frame, "stop_secs", 0) or 0)
            fin = maintenant - silence
            self._voix_a = maintenant
            if self._debut is not None and not self._mots:
                duree = fin - (self._debut + AVANT_SECONDES)
                if duree >= DUREE_MIN_SECONDES:
                    # Une tâche asyncio simple, annulée à la fin du pipeline (cleanup) : le
                    # processeur reste testable sans pipeline autour.
                    self._tache = asyncio.get_running_loop().create_task(
                        self._rattraper(self._debut, maintenant, duree))
                else:
                    self._debut = None  # un souffle : le prochain segment repart de zéro
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_parle = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_parle = False
            self._bot_arrete_a = maintenant
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._tour_ouvert = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._tour_ouvert = False

    async def cleanup(self):
        self._annuler()
        await super().cleanup()

    def _annuler(self) -> None:
        if self._tache is not None and not self._tache.done():
            self._tache.cancel()
        self._tache = None

    def _segment(self, debut: float, fin: float) -> bytes:
        return b"".join(pcm for t, pcm in self._audio if debut <= t <= fin)

    async def _rattraper(self, debut: float, fin: float, duree: float) -> None:
        await asyncio.sleep(ATTENTE_SECONDES)
        # À partir d'ici le segment est traité, quoi qu'il arrive : le suivant repart de
        # zéro. (Annulée AVANT, par une reprise de voix, la tâche laisse le début en
        # place : les deux morceaux partent ensemble.)
        self._debut = None
        if self._mots or self._bot_parle or self._tour_ouvert:
            return
        langue = self._langue() or "multi"
        resultat = await self._transcrire(self._segment(debut, fin), self._sample_rate, langue)
        # Le monde a pu bouger pendant la requête : un mot arrivé entre-temps, ou
        # l'assistante qui s'est remise à parler. Dans les deux cas, on se tait.
        if self._mots or self._bot_parle or self._tour_ouvert:
            return
        if resultat and resultat[1] >= CONFIANCE_MIN:
            texte, confiance = resultat
            logger.info(f"rattrapage de parole : « {texte} » ({confiance:.2f}) sur {duree:.1f} s de voix")
            self._noter("rattrape", texte=texte[:120], confiance=round(confiance, 3),
                        duree_ms=int(duree * 1000))
            await self.push_frame(TranscriptionFrame(texte, "", time_now_iso8601()))
            return
        self._noter("pas_entendu", duree_ms=int(duree * 1000),
                    confiance=round(resultat[1], 3) if resultat else None)
        maintenant = self._horloge()
        if duree >= DUREE_MIN_PARDON_SECONDES and maintenant - self._dernier_pardon >= ESPACEMENT_PARDON_SECONDES:
            self._dernier_pardon = maintenant
            self.pardons_de_suite += 1
            await self._pardonner()
