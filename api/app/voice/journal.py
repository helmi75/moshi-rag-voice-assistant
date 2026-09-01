"""Journal de bord d'un appel — le capteur qui manquait (#88).

`latency.py` mesure UN intervalle et le rend comme une liste d'entiers nus. Conséquence :
les quatre symptômes constatés au téléphone — des blancs, elle comprend mal, elle coupe
la parole, le ton ne va pas — laissaient **exactement la même trace**. Impossible de
choisir entre eux, donc impossible de corriger.

Ce module corrèle enfin, tour par tour : ce qui a été entendu, ce qui a été dit, et le
blanc **décomposé par étage** — transcription, modèle de langage, appel d'outil, synthèse
vocale — plus la décision de fin de tour et chaque interruption.

**Il ne calcule pas les étages lui-même.** Pipecat les mesure déjà : chaque service
publie son « temps jusqu'au premier octet » dans une `MetricsFrame`. Ces métriques
existaient et personne ne les ramassait, faute d'un `enable_metrics=True` dans
`PipelineParams`. Reconstruire ces durées à la main depuis les horodatages de frames
serait à la fois plus fragile et moins juste.

**Un capteur ne doit jamais faire tomber ce qu'il mesure** : tout le corps de
`on_push_frame` est sous `try/except`. Un journal incomplet est un désagrément ; un appel
raccroché parce que l'instrument a levé une exception est une panne.
"""
import os
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

_NS_PAR_MS = 1_000_000

# Au-delà, un appel n'est plus un appel : ligne restée ouverte, boucle d'interruptions.
# On cesse d'ajouter plutôt que de gonfler la base et les sauvegardes.
_MAX_TOURS = 100
_MAX_EVENEMENTS = 400

# Un blanc plus long que ça n'est pas un blanc, c'est un silence entre deux appels.
_PLAFOND_BLANC_MS = 30_000


def _entier(nom: str, defaut: int) -> int:
    try:
        return int(os.getenv(nom, "").strip() or defaut)
    except ValueError:
        return defaut


def _etage(processeur: Optional[str]) -> Optional[str]:
    """À quel maillon appartient cette métrique.

    Les noms de classe de Pipecat portent l'étage : `DeepgramSTTService`,
    `OpenAILLMService`, `MoshiServerTTSService`. On lit ce marqueur plutôt que le nom du
    fournisseur — sinon « kyutai », qui existe en STT ET en TTS, serait ambigu."""
    nom = (processeur or "").lower()
    for marqueur in ("stt", "llm", "tts"):
        if marqueur in nom:
            return marqueur
    return None


def _confiance(frame) -> Optional[float]:
    """Confiance de transcription, si le fournisseur la donne.

    C'est ce qui distingue « elle a mal ENTENDU » de « elle a mal COMPRIS » : un mot
    transcrit à 0,4 de confiance et un contresens du modèle appellent des corrections
    opposées. Extraction défensive : la forme du résultat appartient au fournisseur et
    peut changer sans prévenir — on préfère None à une exception."""
    brut = getattr(frame, "result", None)
    if brut is None:
        return None
    try:
        canal = brut["channel"] if isinstance(brut, dict) else brut.channel
        alternatives = (canal["alternatives"] if isinstance(canal, dict)
                        else canal.alternatives)
        premiere = alternatives[0]
        valeur = (premiere["confidence"] if isinstance(premiere, dict)
                  else premiere.confidence)
        return round(float(valeur), 3)
    except Exception:
        return None


class JournalDeBord(BaseObserver):
    """Observateur : il regarde passer les frames sans jamais en pousser.

    Volontairement SÉPARÉ de `TurnLatencyObserver`, qui reste inchangé. Celui-ci alimente
    `calls.latency_stats` et le contrôle de supervision « Blanc ressenti » : y toucher
    risquerait la seule mesure qui fonctionne aujourd'hui. Deux capteurs indépendants sur
    le même intervalle donnent en prime un test de recoupement.
    """

    def __init__(self):
        super().__init__()
        self._t0: Optional[int] = None
        self.tours: list[dict] = []
        self.evenements: list[dict] = []
        self._tour: Optional[dict] = None
        self._tronque = False
        self._debut_parole_client: Optional[int] = None
        self._debut_parole_bot: Optional[int] = None
        self._debut_outil: Optional[int] = None
        self._compteurs = {
            "tours": 0, "interruptions": 0, "finales_vides": 0,
            "revisions_stt": 0, "relances": 0,
        }

    # -- horloge --------------------------------------------------------------

    def _ms(self, horodatage: int) -> int:
        if self._t0 is None:
            self._t0 = horodatage
        return int((horodatage - self._t0) / _NS_PAR_MS)

    def _noter(self, t_ms: int, quoi: str, **details) -> None:
        if len(self.evenements) >= _entier("JOURNAL_MAX_EVENEMENTS", _MAX_EVENEMENTS):
            self._tronque = True
            return
        self.evenements.append({"t_ms": t_ms, "quoi": quoi, **details})

    # -- observation ----------------------------------------------------------

    async def on_push_frame(self, data: FramePushed):
        try:
            self._observer(data)
        except Exception:
            # Un instrument ne fait jamais tomber ce qu'il mesure. On perd une ligne de
            # journal, jamais un appel.
            self._tronque = True

    def _observer(self, data: FramePushed) -> None:
        frame = data.frame
        t = self._ms(data.timestamp)

        if isinstance(frame, StartFrame):
            self._t0 = data.timestamp
            return

        if isinstance(frame, UserStartedSpeakingFrame):
            self._debut_parole_client = t
            self._noter(t, "client_parle")
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Un tour commence quand le client se tait : c'est de là que part le blanc
            # qu'il ressent.
            if self._debut_parole_client is not None:
                duree = t - self._debut_parole_client
            else:
                duree = None
            self._tour = {
                "n": len(self.tours) + 1, "t_ms": t, "parole_client_ms": duree,
                "entendu": None, "confiance": None, "dit": "",
                "stt_ms": None, "llm_ms": None, "outil_ms": 0, "tts_ms": None,
                "revisions_stt": 0, "interruption": None, "smart_turn": None,
            }
            self._debut_parole_client = None
            self._noter(t, "client_stop")
            return

        if isinstance(frame, InterimTranscriptionFrame):
            if self._tour is not None:
                self._tour["revisions_stt"] += 1
            self._compteurs["revisions_stt"] += 1
            return

        if isinstance(frame, TranscriptionFrame):
            texte = (getattr(frame, "text", "") or "").strip()
            if not texte:
                self._compteurs["finales_vides"] += 1
            if self._tour is not None:
                self._tour["entendu"] = texte or None
                self._tour["confiance"] = _confiance(frame)
            self._noter(t, "entendu", texte=texte[:120])
            return

        if isinstance(frame, FunctionCallInProgressFrame):
            self._debut_outil = t
            self._noter(t, "outil", nom=getattr(frame, "function_name", None))
            return

        if isinstance(frame, FunctionCallResultFrame):
            if self._tour is not None and self._debut_outil is not None:
                self._tour["outil_ms"] += max(0, t - self._debut_outil)
            self._debut_outil = None
            return

        if isinstance(frame, TTSTextFrame):
            if self._tour is not None:
                self._tour["dit"] = (self._tour["dit"] + " " + (frame.text or "")).strip()
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._debut_parole_bot = t
            self._cloturer_tour(t)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            if self._debut_parole_bot is not None and self.tours:
                self.tours[-1]["parole_bot_ms"] = t - self._debut_parole_bot
            self._debut_parole_bot = None
            return

        if isinstance(frame, InterruptionFrame):
            # Le sens compte : le client qui coupe l'assistante est un signal de qualité
            # (elle est trop longue) ; l'inverse est un défaut de réglage.
            sens = "client_coupe_bot" if self._debut_parole_bot is not None else "bot_coupe_client"
            self._compteurs["interruptions"] += 1
            if self.tours:
                self.tours[-1]["interruption"] = sens
            self._noter(t, "interruption", sens=sens)
            return

        if isinstance(frame, MetricsFrame):
            self._metriques(t, frame)

    def _metriques(self, t: int, frame) -> None:
        for donnee in getattr(frame, "data", None) or []:
            nom = type(donnee).__name__
            if nom in ("TurnMetricsData", "SmartTurnMetricsData"):
                # Les deux noms coexistent : `SmartTurnMetricsData` est déprécié depuis
                # Pipecat 0.0.104 au profit de `TurnMetricsData`, qui a perdu au passage
                # `inference_time_ms`. Accepter les deux évite qu'une montée de version
                # ne fasse disparaître la mesure des coupures EN SILENCE — la panne que
                # tout ce module existe pour éviter.
                ms = getattr(donnee, "inference_time_ms", None)
                if ms is None:
                    ms = getattr(donnee, "e2e_processing_time_ms", None)
                decision = {
                    "complet": bool(getattr(donnee, "is_complete", False)),
                    "proba": round(float(getattr(donnee, "probability", 0) or 0), 3),
                    "ms": int(ms or 0),
                }
                if self._tour is not None:
                    self._tour["smart_turn"] = decision
                elif self.tours:
                    self.tours[-1]["smart_turn"] = decision
                self._noter(t, "fin_de_tour", **decision)
            elif nom == "TTFBMetricsData" and self._tour is not None:
                etage = _etage(getattr(donnee, "processor", None))
                if etage:
                    # `value` est en secondes chez Pipecat.
                    self._tour[f"{etage}_ms"] = int(float(donnee.value or 0) * 1000)

    def _cloturer_tour(self, t: int) -> None:
        tour = self._tour
        self._tour = None
        if tour is None:
            return
        blanc = t - tour["t_ms"]
        if not (0 <= blanc <= _PLAFOND_BLANC_MS):
            return  # reprise sans parole préalable (accueil, relance) : pas un blanc
        tour["blanc_ms"] = blanc
        attribue = sum(tour.get(f"{e}_ms") or 0 for e in ("stt", "llm", "tts")) + tour["outil_ms"]
        # Le reste va dans un poste EXPLICITE. Un écart qu'on ne nomme pas se lirait
        # comme une mesure fausse ; nommé, il devient une piste (réseau, agrégation,
        # attente de fin de tour).
        tour["non_attribue_ms"] = max(0, blanc - attribue)
        if len(self.tours) < _entier("JOURNAL_MAX_TOURS", _MAX_TOURS):
            self.tours.append(tour)
        else:
            self._tronque = True
        self._compteurs["tours"] = len(self.tours)

    # -- restitution ----------------------------------------------------------

    def journal(self, enregistrement: Optional[dict] = None) -> dict:
        """Le journal complet, prêt à être stocké. `None` si rien n'a été observé —
        on préfère « pas de mesure » à un objet vide qui se lirait comme un appel muet."""
        return {
            "version": 1,
            "tronque": self._tronque,
            "enregistrement": enregistrement or {},
            "compteurs": dict(self._compteurs),
            "tours": self.tours,
            "evenements": self.evenements,
        }
