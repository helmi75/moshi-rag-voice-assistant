"""Langue de l'appel : on décroche en bilingue, puis on se fixe sur le français.

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
reconnecte Deepgram dans cette langue pour le reste de l'appel. Sur les sept appels,
aucune phrase de quatre mots ou plus n'a été mal étiquetée ; les fausses alertes font
trois mots au plus (« Oh, yes, yes. », « l m I. »).

La reconnexion ne perd rien : Pipecat la diffère tant que l'appelant parle, et met
l'audio en tampon le temps qu'elle a lieu (`STTService._request_reconnect`).
"""
from collections import Counter
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Une phrase tranche si elle compte au moins ce nombre de mots, dont les trois quarts
# dans une même langue. Les fausses alertes mesurées font trois mots au plus.
MOTS_MIN_PHRASE = 4
PART_MIN_PHRASE = 0.75
# Un appelant qui ne répond que par bribes (« oui », « Paul », « vingt heures ») finit
# par trancher sur le cumul, avec une majorité moins exigeante.
MOTS_MIN_CUMUL = 8
PART_MIN_CUMUL = 0.6


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
    """Entre le STT et l'agrégateur : regarde passer les transcriptions finales, sans
    rien retenir ni rien modifier, jusqu'à ce que la langue de l'appel soit tranchée.

    `reglage(langue)` fabrique la frame qui reconnecte le STT dans une langue ; elle
    est poussée VERS L'AMONT, donc vers le STT placé juste avant. `noter(langue)`
    consigne la décision (journal de bord). `langue` reste lisible par le pipeline :
    c'est elle qui choisit la langue des relances."""

    def __init__(self, langue_du_lieu: str, reglage: Callable[[str], Frame],
                 noter: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(**kwargs)
        self._lieu = langue_du_lieu
        self._reglage = reglage
        self._noter = noter
        self.langue: Optional[str] = None
        self._cumul: Counter = Counter()

    def decider(self, mots: Counter) -> Optional[str]:
        """Ajoute une phrase au cumul ; renvoie la langue si elle est tranchée."""
        if not mots:
            return None
        self._cumul.update(mots)
        return trancher(mots) or trancher(self._cumul, MOTS_MIN_CUMUL, PART_MIN_CUMUL)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # D'abord la frame, ensuite l'analyse : la détection n'ajoute rien au blanc.
        await self.push_frame(frame, direction)
        if (self.langue is None and isinstance(frame, TranscriptionFrame)
                and not isinstance(frame, InterimTranscriptionFrame)):
            try:
                await self._entendre(frame)
            except Exception as exc:
                # Rester en `multi` est la situation d'avant : dégradé, jamais muet.
                logger.warning(f"langue de l'appel : détection KO ({exc}), on reste bilingue")

    async def _entendre(self, frame) -> None:
        langue = self.decider(mots_par_langue(frame))
        if langue is None:
            return
        self.langue = langue
        if self._noter is not None:
            self._noter(langue)
        if langue == self._lieu:
            logger.info(f"langue de l'appel : {langue} — transcription fixée sur {langue}")
            await self.push_frame(self._reglage(langue), FrameDirection.UPSTREAM)
        else:
            # Anglais (ou autre) : on RESTE en `multi`, qui l'entend bien et suit
            # l'appelant s'il repasse au français pour un nom de rue ou de plat.
            logger.info(f"langue de l'appel : {langue} — transcription laissée bilingue")
