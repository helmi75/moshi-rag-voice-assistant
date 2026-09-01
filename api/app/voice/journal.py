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

## Ce que trois vrais appels ont corrigé (01/09/2026)

451 tests ne l'avaient pas vu, parce qu'ils vérifiaient ma représentation du pipeline et
non le pipeline. Quatre corrections, toutes issues de la même leçon — **l'ordre réel des
frames n'est pas celui qu'on imagine** :

1. **Le texte arrive hors de la fenêtre de tour.** La transcription finale de Deepgram
   arrive AVANT `UserStoppedSpeakingFrame` — c'est elle qui déclenche la fin de tour — et
   le texte TTS arrive APRÈS `BotStartedSpeakingFrame`. Scoper la collecte à la fenêtre
   laissait donc `entendu`, `dit` et `confiance` systématiquement vides. On accumule
   désormais en continu, et on rattache au tour.

2. **`InterruptionFrame` est DIFFUSÉE** à tous les processeurs, dans les deux sens : une
   seule vraie coupure en produisait des dizaines. Compteurs relevés sur de vrais appels
   de deux minutes : 77, 275, 121. On détecte maintenant l'évènement lui-même — quelqu'un
   qui se met à parler pendant que l'autre parle.

3. **Deepgram ne publie aucun TTFB** (vérifié dans le code du service) : `stt_ms` ne
   pouvait qu'être vide. Remplacé par une mesure qui existe vraiment, `attente_tour_ms`.

4. **Le silence était sous-mesuré.** L'appelant se tait à la détection VAD, mais
   `UserStoppedSpeakingFrame` n'arrive qu'après la décision de fin de tour. Tout ce délai
   — smart-turn qui hésite, `USER_TURN_STOP_TIMEOUT` — n'était compté nulle part, alors
   que l'appelant l'entend. `blanc_ressenti_ms` le comprend enfin.
"""
import os
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
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
        self._vad_stop: Optional[int] = None
        self._premiere_parole: Optional[int] = None
        # Buffers HORS fenêtre de tour : le texte n'arrive pas quand on l'attend.
        self._entendu: Optional[str] = None
        self._confiance: Optional[float] = None
        self._revisions = 0
        self._dit: str = ""
        self._client_parle = False
        self._bot_parle = False
        self._compteurs = {
            "tours": 0, "coupures_du_client": 0, "coupures_du_bot": 0,
            "finales_vides": 0, "revisions_stt": 0,
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

        # --- qui parle, et qui coupe qui -------------------------------------
        if isinstance(frame, UserStartedSpeakingFrame):
            if self._bot_parle:
                # L'ÉVÉNEMENT lui-même, pas son écho : quelqu'un se met à parler pendant
                # que l'autre parle. C'est ce qu'on voulait compter depuis le début.
                self._compteurs["coupures_du_client"] += 1
                if self.tours:
                    self.tours[-1]["interruption"] = "client_coupe_bot"
                self._noter(t, "coupure", sens="client_coupe_bot")
            self._client_parle = True
            self._debut_parole_client = t
            self._noter(t, "client_parle")
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            # L'instant où l'appelant SE TAIT réellement. Le silence qu'il ressent part
            # d'ici — pas de `UserStoppedSpeakingFrame`, qui n'arrive qu'une fois la fin
            # de tour décidée.
            self._vad_stop = t
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._client_parle = False
            duree = (t - self._debut_parole_client) if self._debut_parole_client else None
            attente = (t - self._vad_stop) if self._vad_stop is not None else None
            self._tour = {
                "n": len(self.tours) + 1, "t_ms": t, "parole_client_ms": duree,
                "attente_tour_ms": max(0, attente) if attente is not None else None,
                "entendu": self._entendu, "confiance": self._confiance,
                "revisions_stt": self._revisions, "dit": "",
                "llm_ms": None, "outil_ms": 0, "tts_ms": None,
                "interruption": None, "smart_turn": None,
            }
            # Le texte entendu a été collecté AVANT ce point : on le consomme ici, puis
            # on remet les compteurs à zéro pour le tour suivant.
            self._entendu = None
            self._confiance = None
            self._revisions = 0
            self._vad_stop = None
            self._debut_parole_client = None
            self._noter(t, "client_stop")
            return

        # --- ce qui a été entendu (hors fenêtre) ------------------------------
        if isinstance(frame, InterimTranscriptionFrame):
            self._revisions += 1
            self._compteurs["revisions_stt"] += 1
            return

        if isinstance(frame, TranscriptionFrame):
            texte = (getattr(frame, "text", "") or "").strip()
            if not texte:
                self._compteurs["finales_vides"] += 1
            else:
                self._entendu = texte
                self._confiance = _confiance(frame)
            self._noter(t, "entendu", texte=texte[:120])
            return

        # --- outils -----------------------------------------------------------
        if isinstance(frame, FunctionCallInProgressFrame):
            self._debut_outil = t
            self._noter(t, "outil", nom=getattr(frame, "function_name", None))
            return

        if isinstance(frame, FunctionCallResultFrame):
            cible = self._tour if self._tour is not None else (
                self.tours[-1] if self.tours else None)
            if cible is not None and self._debut_outil is not None:
                cible["outil_ms"] = (cible.get("outil_ms") or 0) + max(0, t - self._debut_outil)
            self._debut_outil = None
            return

        # --- ce qui a été dit (hors fenêtre, il arrive APRÈS le début de parole) --
        if isinstance(frame, TTSTextFrame):
            self._dit = (self._dit + " " + (frame.text or "")).strip()
            if self.tours:
                self.tours[-1]["dit"] = self._dit
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            if self._client_parle:
                self._compteurs["coupures_du_bot"] += 1
                if self._tour is not None:
                    self._tour["interruption"] = "bot_coupe_client"
                self._noter(t, "coupure", sens="bot_coupe_client")
            if self._premiere_parole is None:
                self._premiere_parole = t
            self._bot_parle = True
            self._debut_parole_bot = t
            self._dit = ""  # nouveau tour de parole du bot
            self._cloturer_tour(t)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_parle = False
            if self._debut_parole_bot is not None and self.tours:
                self.tours[-1]["parole_bot_ms"] = t - self._debut_parole_bot
            self._debut_parole_bot = None
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
            elif nom == "TTFBMetricsData":
                etage = _etage(getattr(donnee, "processor", None))
                # Pas de `stt` : Deepgram ne publie aucun TTFB (vérifié dans le service).
                # Le temps de transcription n'est pas dans le blanc de toute façon — la
                # transcription finale DÉCLENCHE la fin de tour, elle la précède.
                if etage in ("llm", "tts"):
                    cible = self._tour if self._tour is not None else (
                        self.tours[-1] if self.tours else None)
                    if cible is not None:
                        # `value` est en secondes chez Pipecat.
                        cible[f"{etage}_ms"] = int(float(donnee.value or 0) * 1000)

    def _cloturer_tour(self, t: int) -> None:
        tour = self._tour
        self._tour = None
        if tour is None:
            return
        blanc = t - tour["t_ms"]
        if not (0 <= blanc <= _PLAFOND_BLANC_MS):
            return  # reprise sans parole préalable (accueil, relance) : pas un blanc
        tour["blanc_ms"] = blanc
        # Le silence RÉELLEMENT entendu par l'appelant : il se tait (VAD), puis attend
        # que la fin de tour soit décidée, puis attend la réponse. Le premier segment
        # n'était compté nulle part alors qu'il s'entend exactement comme le second.
        tour["blanc_ressenti_ms"] = blanc + (tour.get("attente_tour_ms") or 0)
        attribue = (tour.get("llm_ms") or 0) + (tour.get("tts_ms") or 0) + tour["outil_ms"]
        # Le reste va dans un poste EXPLICITE. Un écart qu'on ne nomme pas se lirait
        # comme une mesure fausse ; nommé, il devient une piste (réseau, agrégation).
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
        blancs = [t["blanc_ressenti_ms"] for t in self.tours if t.get("blanc_ressenti_ms")]
        return {
            "version": 2,
            "tronque": self._tronque,
            "enregistrement": enregistrement or {},
            # Combien de temps avant que l'appelant entende la PREMIÈRE parole utile.
            # Sur un GPU froid, c'est ~90 s de musique d'attente : sans ce chiffre, ce
            # démarrage se confondrait avec un problème de latence de conversation, qui
            # est un tout autre sujet et appelle un tout autre correctif.
            "accueil": {"premiere_parole_ms": self._premiere_parole},
            "compteurs": {
                **self._compteurs,
                "blanc_median_ms": sorted(blancs)[len(blancs) // 2] if blancs else None,
            },
            "tours": self.tours,
            "evenements": self.evenements,
        }
