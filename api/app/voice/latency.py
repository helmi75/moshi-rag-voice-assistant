"""Mesure du blanc ressenti par l'appelant, tour par tour.

Ce qu'on mesure est ce que l'oreille perçoit : le temps entre l'instant où le client
ARRÊTE DE PARLER et l'instant où la voix repart. Ce délai englobe tout — l'attente du
VAD, l'analyse de fin de tour, le LLM, la synthèse — parce que l'appelant, lui, ne
distingue pas les étages : il entend un silence, et au-delà de deux ou trois secondes
il dit « allô ».

Passe par un OBSERVATEUR (`observers=` de PipelineTask) et non par un maillon du
pipeline : rien n'est inséré sur le chemin de l'audio, donc le turn-taking réglé à
l'oreille n'est pas perturbé.
"""
from pipecat.frames.frames import BotStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed

# Un blanc au-delà de ce seuil n'est plus une latence, c'est un incident (relance
# d'inactivité, appel d'outil parti en vrille) : le compter fausserait la médiane.
_PLAFOND_MS = 30_000


class TurnLatencyObserver(BaseObserver):
    """Collecte les blancs « fin de parole client -> reprise de la voix », en ms."""

    def __init__(self):
        super().__init__()
        self._depuis = None
        self.samples: list[int] = []

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, UserStoppedSpeakingFrame):
            # Le client vient de se taire : le chronomètre part. Un nouveau silence
            # écrase le précédent (il n'a pas obtenu de réponse, il est déjà compté
            # ou perdu) — on mesure toujours le dernier blanc réellement vécu.
            self._depuis = data.timestamp
        elif isinstance(frame, BotStartedSpeakingFrame) and self._depuis is not None:
            ecart_ms = int((data.timestamp - self._depuis) / 1_000_000)
            self._depuis = None
            if 0 <= ecart_ms <= _PLAFOND_MS:
                self.samples.append(ecart_ms)

    def median_ms(self) -> int | None:
        """Médiane des blancs, ou None si l'appel n'a pas eu un seul échange."""
        if not self.samples:
            return None
        ordonnes = sorted(self.samples)
        milieu = len(ordonnes) // 2
        if len(ordonnes) % 2:
            return ordonnes[milieu]
        return (ordonnes[milieu - 1] + ordonnes[milieu]) // 2
