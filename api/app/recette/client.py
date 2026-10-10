"""Le client d'essai : il écoute, et parle quand l'assistante s'est tue.

Il n'entend que du son, comme un téléphone : il ne lit ni la transcription ni le journal.
C'est ce qui lui permet de servir aux deux étages — derrière un faux Twilio comme au bout
d'un vrai appel. Le verdict, lui, se lit en base après coup (`verdicts.py`).

« Elle s'est tue » se décide comme sur une ligne : un son audible reçu se joue à son
arrivée, ou à la suite du précédent ; l'assistante a fini quand plus rien d'audible n'est
à jouer depuis `silence` secondes. Le flux de l'application arrive par blocs, celui d'un
vrai appel trame par trame, silences compris : le même calcul vaut pour les deux.
"""
import time
from typing import Callable, Optional

import numpy as np

from ..voice import ulaw

TRAME = 160                      # 20 ms en µ-law 8 kHz
SILENCE = b"\xff" * TRAME
# En dessous, c'est le souffle d'une ligne, pas une voix (amplitude sur 16 bits).
_SEUIL_AUDIBLE = 500.0


def audible(charge: bytes) -> bool:
    if not charge:
        return False
    pcm = np.frombuffer(ulaw.decoder(charge), dtype="<i2").astype(np.float64)
    return bool(pcm.size) and float(np.sqrt(np.mean(pcm * pcm))) > _SEUIL_AUDIBLE


class Client:
    """Joue ses répliques l'une après l'autre. `recu` reçoit le son de l'assistante,
    `trame` rend les 20 ms suivantes à envoyer ; `fini` passe à vrai quand tout est dit."""

    # Quatre secondes de silence avant de parler : sur GPT-Live, « Je vérifie tout de
    # suite. » est suivi de deux à quatre secondes de blanc pendant que l'outil travaille
    # (bancs du 09/10/2026). À 1,5 s, le client répondait dans ce blanc, parlait par-dessus
    # le récapitulatif et disait au revoir avant la confirmation (essai du 10/10/2026).
    def __init__(self, repliques: list[bytes], silence: float = 4.0, patience: float = 30.0,
                 conge: float = 6.0, duree_max: float = 170.0,
                 horloge: Callable[[], float] = time.monotonic):
        self._repliques = list(repliques)
        self._silence, self._patience, self._conge = silence, patience, conge
        self._duree_max = duree_max
        self._horloge = horloge
        self._debut = horloge()
        self._fin_de_lecture: Optional[float] = None   # quand le son de l'assistante finit d'être joué
        self._entendue = False        # a-t-elle parlé depuis ma dernière réplique ?
        self._en_cours = b""
        self._fin_de_replique = self._debut
        self.dites = 0
        self.forcees: list[int] = []  # répliques dites sans qu'elle ait parlé : à dire au verdict

    def recu(self, charge: bytes) -> None:
        if not audible(charge):
            return
        maintenant = self._horloge()
        depart = max(maintenant, self._fin_de_lecture or maintenant)
        self._fin_de_lecture = depart + len(charge) / 8000
        self._entendue = True

    def _elle_s_est_tue(self) -> bool:
        return (self._fin_de_lecture is not None
                and self._horloge() - self._fin_de_lecture >= self._silence)

    def _a_moi(self) -> bool:
        if self._entendue and self._elle_s_est_tue():
            return True
        if self._horloge() - self._fin_de_replique >= self._patience:
            self.forcees.append(self.dites + 1)
            return True
        return False

    def trame(self) -> bytes:
        if self._en_cours:
            trame, self._en_cours = self._en_cours[:TRAME], self._en_cours[TRAME:]
            if not self._en_cours:
                self._fin_de_replique = self._horloge()
                self._entendue = False
            return trame.ljust(TRAME, b"\xff")
        if self.dites < len(self._repliques) and self._a_moi():
            self._en_cours = self._repliques[self.dites]
            self.dites += 1
            return self.trame()
        return SILENCE

    @property
    def fini(self) -> bool:
        """Tout est dit et elle a pris congé (ou ne dit plus rien) ; ou l'appel a trop duré :
        un client d'essai qui ne raccroche pas coûte de l'argent jusqu'à la limite de Twilio."""
        maintenant = self._horloge()
        if maintenant - self._debut >= self._duree_max:
            return True
        if self._en_cours or self.dites < len(self._repliques):
            return False
        if self._entendue and self._elle_s_est_tue():
            return maintenant - (self._fin_de_lecture or maintenant) >= self._conge
        return maintenant - self._fin_de_replique >= self._patience
