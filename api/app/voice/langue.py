"""Langue de l'appel : on décroche en bilingue, puis on suit l'appelant.

Mesuré le 10/09/2026 en rejouant dans Deepgram, en direct, les pistes « appelant » des
sept appels réels 100 à 106, une fois en `language=fr`, une fois en `language=multi` :

- en `fr`, l'anglais est PERDU. « Yes, hello, my name is Helmi. I want to make a
  reservation. Do you speak English? » n'a rien produit du tout, puis « Du speak
  english ». Un anglophone n'aurait jamais été compris.
- en `multi`, l'anglais est transcrit mot pour mot, mais les réponses françaises
  COURTES basculent en anglais : « C'est ça » → « Yes, sir. », « Ouais » → « What? »,
  « Allô » → « Hello. ». Sur le français, l'écart à Whisper passe de 20 % à 26 % (une
  partie tient aux nombres écrits en lettres). Un « Yes, sir. » au moment de confirmer,
  c'est le modèle qui répond en anglais à un Français.

D'où la règle : on DÉCROCHE en `multi` — un anglophone est compris dès sa première
phrase — et dès qu'une phrase assez longue tranche pour la langue de l'établissement, on
reconnecte Deepgram dans cette langue. Sur les sept appels, aucune phrase de quatre mots
ou plus n'a été mal étiquetée ; les fausses alertes font trois mots au plus (« Oh, yes,
yes. », « l m I. »).

⚠️ Cette décision ne peut PAS être définitive, et c'est ce qui manquait jusqu'au
21/09/2026. Une conversation commencée en français puis basculée en anglais rendait
l'assistante SOURDE : le STT était fixé sur `fr`, où l'anglais ne produit rien, et
l'appelant n'était plus transcrit du tout jusqu'à ce qu'il revienne au français
(constaté à l'oreille, et lisible dans les appels 138 à 141). Le piège est refermé sur
lui-même : une fois fixé sur `fr`, Deepgram n'étiquette plus les mots par langue, donc
le signal qui dirait de déverrouiller n'existe plus.

Deux mécanismes répondent à ça :

1. la langue se **re-décide en continu**, sur une fenêtre glissante des dernières
   phrases, et le réglage du STT suit ;
2. quand le STT est verrouillé, on surveille les tours où le VAD entend parler sans
   qu'AUCUNE transcription n'arrive. C'est le seul symptôme qui survive au verrou.
   Deux tours pareils et on redevient bilingue — **définitivement** pour cet appel :
   on tient la preuve que l'appelant change de langue, et rester bilingue coûte un peu
   de précision là où rester sourd coûte l'appel.

La reconnexion ne perd rien : Pipecat la diffère tant que l'appelant parle, et met
l'audio en tampon le temps qu'elle a lieu (`STTService._request_reconnect`).
"""
import time
from collections import Counter, deque
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Ce qu'on demande à Deepgram pour qu'il étiquette chaque mot au lieu de se fixer.
BILINGUE = "multi"

# Une phrase tranche si elle compte au moins ce nombre de mots, dont les trois quarts
# dans une même langue. Les fausses alertes mesurées font trois mots au plus.
MOTS_MIN_PHRASE = 4
PART_MIN_PHRASE = 0.75
# Un appelant qui ne répond que par bribes (« oui », « Paul », « vingt heures ») finit
# par trancher sur le cumul, avec une majorité moins exigeante.
MOTS_MIN_CUMUL = 8
PART_MIN_CUMUL = 0.6
# Le cumul GLISSE sur les dernières phrases : passé cette fenêtre, le début de l'appel
# ne doit plus peser sur la langue COURANTE, sinon la langue ne peut plus changer.
# Cinq phrases, c'est ce qu'il faut pour que des bribes finissent quand même par
# trancher sans que la première phrase fige tout le reste.
PHRASES_CUMULEES = 5

# Tours de parole sans la moindre transcription avant de conclure qu'on n'entend plus.
# Un seul suffirait à réagir plus vite, mais une fausse alerte (un bruit pris pour de
# la parole) coûterait la précision du français pour tout le reste de l'appel.
TOURS_SOURDS_MAX = 2
# En dessous, ce n'est pas une phrase : une toux, un choc, un « mmh ». On ne compte pas
# un tour aussi court comme la preuve qu'on est devenu sourd.
DUREE_MIN_TOUR = 0.8


def trancher(mots: Counter, mots_min: int = MOTS_MIN_PHRASE,
             part_min: float = PART_MIN_PHRASE) -> Optional[str]:
    """La langue majoritaire si elle est assez nette sur assez de mots, sinon None."""
    total = sum(mots.values())
    if total < mots_min:
        return None
    langue, n = mots.most_common(1)[0]
    return langue if n >= part_min * total else None


def _champ(objet, nom):
    return objet.get(nom) if isinstance(objet, dict) else getattr(objet, nom, None)


def mots_par_langue(frame) -> Counter:
    """Nombre de mots par langue dans une transcription finale.

    En `multi`, Deepgram étiquette chaque MOT ; Pipecat ne remonte que la première
    langue de la phrase (`frame.language`), on lit donc le résultat brut. Extraction
    défensive : sa forme appartient au fournisseur. Rien d'étiqueté (Deepgram déjà
    fixé sur une langue) → compteur vide."""
    compte: Counter = Counter()
    try:
        canal = _champ(getattr(frame, "result", None), "channel")
        alternative = _champ(canal, "alternatives")[0]
        for mot in _champ(alternative, "words") or []:
            langue = _champ(mot, "language")
            if langue:
                compte[str(langue).split("-")[0].lower()] += 1
    except Exception:
        compte.clear()
    if not compte and getattr(frame, "language", None):
        # Repli : la langue de la phrase, pour tous ses mots.
        langue = str(getattr(frame.language, "value", frame.language)).split("-")[0].lower()
        compte[langue] = len((getattr(frame, "text", "") or "").split())
    return compte


class DetecteurDeLangue(FrameProcessor):
    """Entre le STT et l'agrégateur : regarde passer les transcriptions finales et les
    tours de parole, sans rien retenir ni rien modifier.

    `reglage(langue)` fabrique la frame qui reconnecte le STT dans une langue ; elle
    est poussée VERS L'AMONT, donc vers le STT placé juste avant. `noter(langue)`
    consigne chaque changement (journal de bord). `langue` reste lisible par le
    pipeline : c'est elle qui choisit la langue des relances."""

    def __init__(self, langue_du_lieu: str, reglage: Callable[[str], Frame],
                 noter: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(**kwargs)
        self._lieu = langue_du_lieu
        self._reglage = reglage
        self._noter = noter
        self.langue: Optional[str] = None
        self._fenetre: deque = deque(maxlen=PHRASES_CUMULEES)
        # Langue imposée au STT, None tant qu'il est bilingue.
        self._fixee: Optional[str] = None
        # Une fois sourd, plus jamais de verrou : l'appelant a prouvé qu'il change de
        # langue, et un deuxième verrou le rendrait sourd une deuxième fois.
        self._sourd_une_fois = False
        self._debut_parole: Optional[float] = None
        self._transcrite = False
        self._tour_en_attente = False
        self._tours_sourds = 0

    def decider(self, mots: Counter) -> Optional[str]:
        """Ajoute une phrase à la fenêtre ; renvoie la langue si elle est tranchée."""
        if not mots:
            return None
        self._fenetre.append(mots)
        cumul: Counter = Counter()
        for phrase in self._fenetre:
            cumul.update(phrase)
        return trancher(mots) or trancher(cumul, MOTS_MIN_CUMUL, PART_MIN_CUMUL)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # D'abord la frame, ensuite l'analyse : la détection n'ajoute rien au blanc.
        await self.push_frame(frame, direction)
        try:
            await self._regarder(frame)
        except Exception as exc:
            # Rester en `multi` est la situation d'avant : dégradé, jamais muet.
            logger.warning(f"langue de l'appel : détection KO ({exc}), on reste bilingue")

    async def _regarder(self, frame: Frame) -> None:
        if isinstance(frame, UserStartedSpeakingFrame):
            # Le tour précédent est jugé ICI, pas à sa fin : entre deux tours il y a la
            # réponse de l'assistante, donc tout le temps qu'il faut à Deepgram pour
            # rendre sa transcription finale. La juger plus tôt inventerait des sourds.
            await self._cloturer_tour()
            self._debut_parole = time.monotonic()
            self._transcrite = False
            return
        if isinstance(frame, UserStoppedSpeakingFrame):
            debut = self._debut_parole
            self._debut_parole = None
            assez_long = debut is not None and time.monotonic() - debut >= DUREE_MIN_TOUR
            self._tour_en_attente = assez_long and not self._transcrite
            return
        if isinstance(frame, TranscriptionFrame) and not isinstance(frame, InterimTranscriptionFrame):
            if (getattr(frame, "text", "") or "").strip():
                # On entend : ce tour n'est pas sourd, et le compteur repart de zéro.
                self._transcrite = True
                self._tour_en_attente = False
                self._tours_sourds = 0
            await self._entendre(frame)

    async def _cloturer_tour(self) -> None:
        """Un tour de parole s'est terminé sans transcription : est-on devenu sourd ?"""
        if not self._tour_en_attente:
            return
        self._tour_en_attente = False
        if self._fixee is None:
            # Bilingue : un tour sans transcription est du bruit, pas une surdité.
            return
        self._tours_sourds += 1
        if self._tours_sourds < TOURS_SOURDS_MAX:
            return
        logger.warning(
            f"langue de l'appel : {self._tours_sourds} tours de parole sans aucune "
            f"transcription alors que le STT est fixé sur {self._fixee} — "
            f"l'appelant a probablement changé de langue, retour au bilingue")
        self._tours_sourds = 0
        self._sourd_une_fois = True
        self._fenetre.clear()
        await self._regler(None)

    async def _regler(self, cible: Optional[str]) -> None:
        """Reconnecte le STT : `cible` = la langue, ou None pour le bilingue."""
        self._fixee = cible
        await self.push_frame(self._reglage(cible or BILINGUE), FrameDirection.UPSTREAM)

    async def _entendre(self, frame) -> None:
        langue = self.decider(mots_par_langue(frame))
        if langue is None or langue == self.langue:
            return
        self.langue = langue
        if self._noter is not None:
            self._noter(langue)
        cible = langue if langue == self._lieu and not self._sourd_une_fois else None
        if cible == self._fixee:
            logger.info(f"langue de l'appel : {langue} — transcription laissée bilingue")
            return
        await self._regler(cible)
        logger.info(f"langue de l'appel : {langue} — transcription fixée sur {cible or BILINGUE}")
