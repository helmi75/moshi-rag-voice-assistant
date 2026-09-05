"""Partage du modèle de fin de tour entre les appels simultanés (#40).

**Le défaut, mesuré au banc d'essai le 05/09/2026 :** le modèle ONNX de smart-turn v3
est rechargé DEPUIS LE DISQUE à chaque appel. Relevé dans les journaux du conteneur :
vingt chargements pour vingt appels, 84 à 411 ms chacun — et sérialisés, parce que la
construction se fait dans la boucle d'événements.

Conséquence directe et mesurable : à huit appels simultanés, le premier son de
l'assistante arrivait après **6,2 secondes**, contre 0,5 s pour un appel seul. Or cet
accueil est un simple fichier WAV en cache : il ne touche ni le GPU, ni la transcription,
ni le modèle de langage. Les six secondes n'étaient pas de la charge, c'était le serveur
qui relisait huit fois le même fichier pendant que la boucle attendait.

**Pourquoi c'est sûr de partager.** Une `InferenceSession` d'ONNX Runtime est prévue pour
être appelée par plusieurs fils d'exécution : `Run()` est thread-safe, et l'état propre à
un appel (le tampon audio, l'horloge de silence) vit dans l'objet analyseur, pas dans la
session. Deux appels qui partagent la session ne partagent donc aucune conversation.

**Pourquoi c'est étroit.** On n'intercepte QUE le modèle smart-turn, reconnu à son chemin.
Tout autre modèle ONNX continue d'être chargé normalement. Détourner globalement le
constructeur d'une bibliothèque tierce se paierait tôt ou tard sur un modèle auquel on
n'aurait pas pensé ; ici, ce qui n'est pas explicitement visé n'est pas touché.
"""
import threading

from loguru import logger

# Ce qui identifie le modèle à partager. Le nom de fichier vient de Pipecat
# (`smart-turn-v3.2-cpu.onnx`) ; on filtre sur le radical pour survivre à une montée de
# version mineure sans rattraper silencieusement un autre modèle.
_MARQUEUR = "smart-turn"

_sessions: dict[str, object] = {}
_verrou = threading.Lock()
_actif = False


def partager_le_modele_de_fin_de_tour() -> bool:
    """Fait charger le modèle smart-turn une seule fois par processus.

    Renvoie True si le partage est en place. Idempotent : appelée deux fois, elle ne
    détourne rien une seconde fois. Ne lève jamais — un partage impossible doit
    dégrader la latence, pas empêcher le service de démarrer.
    """
    global _actif
    if _actif:
        return True
    try:
        import onnxruntime as ort
    except ImportError:  # pragma: no cover — onnxruntime est une dépendance de Pipecat
        logger.warning("partage du modèle de fin de tour : onnxruntime absent")
        return False

    originale = ort.InferenceSession

    def _session(model, sess_options=None, providers=None, **kwargs):
        cle = str(model)
        if _MARQUEUR not in cle:
            # Tout ce qui n'est pas smart-turn suit le chemin normal, sans mémoire.
            return originale(model, sess_options=sess_options, providers=providers, **kwargs)
        with _verrou:
            deja = _sessions.get(cle)
            if deja is not None:
                return deja
            session = originale(model, sess_options=sess_options,
                                providers=providers, **kwargs)
            _sessions[cle] = session
            logger.info(f"modèle de fin de tour chargé une fois pour tout le processus "
                        f"({cle.rsplit('/', 1)[-1]}) — les appels suivants le réutilisent")
            return session

    ort.InferenceSession = _session
    _actif = True
    return True


def etat() -> dict:
    """Ce que la supervision peut afficher : partagé ou non, et combien de modèles."""
    return {"actif": _actif, "modeles_partages": len(_sessions)}
