"""La vigie d'un appel : elle repère la panne PENDANT la conversation (ASSISTANTE-118).

Une voix, un modèle ou une transcription qui lâche ne fait pas tomber le pipeline : le
service signale une erreur, la phrase est perdue, et l'appel continue — en silence.
Vu de l'appelant, c'est la pire panne : personne ne lui dit rien et la ligne reste
ouverte. La vigie compte ces erreurs ; à la deuxième d'affilée sur le même maillon,
sans rien de réussi entre les deux, elle prévient, et l'appel est renvoyé au
restaurant (app/renvoi.py).

Deux, pas une : chaque service réessaie déjà de lui-même (la voix, une fois par
phrase). Une seule phrase perdue peut être un accroc ; deux de suite, le client attend
déjà depuis trop longtemps pour parier sur la troisième.

Passe par un OBSERVATEUR, comme la mesure de latence : rien n'est inséré sur le chemin
de l'audio.
"""
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    ErrorFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from .. import renvoi

ERREURS_DE_SUITE = 2


def cause(processeur) -> Optional[str]:
    """Le maillon en panne, d'après le service qui a signalé l'erreur. None pour tout
    autre processeur : une erreur ailleurs n'est pas une panne de la conversation."""
    if isinstance(processeur, TTSService):
        return renvoi.VOIX
    if isinstance(processeur, LLMService):
        return renvoi.MODELE
    if isinstance(processeur, STTService):
        return renvoi.TRANSCRIPTION
    return None


class Vigie(BaseObserver):
    """Prévient (`alerter(cause)`) quand un maillon échoue deux fois de suite. Une seule
    alerte par appel : après, l'appel n'est plus le sien."""

    def __init__(self, alerter: Callable[[str], Awaitable[None]]):
        super().__init__()
        self._alerter = alerter
        self._de_suite = {renvoi.VOIX: 0, renvoi.MODELE: 0, renvoi.TRANSCRIPTION: 0}
        self._vues: set[int] = set()
        self.alertee: Optional[str] = None

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # Ce qui prouve qu'un maillon marche : la voix est sortie, le modèle a écrit, la
        # transcription a rendu des mots.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._de_suite[renvoi.VOIX] = 0
        elif isinstance(frame, LLMTextFrame):
            self._de_suite[renvoi.MODELE] = 0
        elif isinstance(frame, TranscriptionFrame):
            self._de_suite[renvoi.TRANSCRIPTION] = 0
        elif isinstance(frame, ErrorFrame):
            await self._erreur(frame, data.source)

    async def _erreur(self, frame: ErrorFrame, source) -> None:
        # Une erreur remonte le pipeline de maillon en maillon : comptée une seule fois.
        if frame.id in self._vues or self.alertee:
            return
        self._vues.add(frame.id)
        maillon = cause(getattr(frame, "processor", None)) or cause(source)
        if getattr(frame, "fatal", False):
            # Pipecat va arrêter le pipeline sur cette erreur : inutile d'attendre la
            # seconde, et il faut rendre la ligne AVANT qu'il ne raccroche.
            self.alertee = maillon or renvoi.PIPELINE
            await self._alerter(self.alertee)
            return
        if maillon is None:
            return
        self._de_suite[maillon] += 1
        if self._de_suite[maillon] >= ERREURS_DE_SUITE:
            self.alertee = maillon
            await self._alerter(maillon)
