"""Les mesures du parc au format Prometheus, pour Grafana (ASSISTANTE-156).

`/supervision` rend un verdict à l'instant, relevé toutes les quinze minutes : il ne garde
aucun historique et ne voit pas qu'UN restaurant va mal pendant que les autres vont bien.
Ici, des compteurs par établissement, que Grafana relève chaque minute et garde dans le
temps : c'est sur eux que se posent les tableaux de bord et les alertes par client.

Trois règles :

- **Rien sur le chemin de l'audio.** Les mesures sont prises là où l'appel passe déjà :
  l'ouverture du flux (main.py), la clôture en base (`calls.finish_call`, commune aux deux
  moteurs), le passage en secours (`calls.ouvrir_secours`) et la vigie, qui est un
  observateur. Incrémenter un compteur en mémoire ne bloque rien.
- **Une mesure ne fait jamais échouer un appel.** Chaque fonction publique avale ses
  propres erreurs : un appel vaut plus qu'un point sur un graphique.
- **Aucune donnée personnelle.** Ni numéro, ni transcription, ni identifiant d'appel :
  l'établissement est désigné par son identifiant, son nom n'apparaît que dans
  `helmane_etablissement_info` (un nom de commerce, pas une personne).

Les compteurs vivent dans la mémoire du processus et repartent de zéro au redéploiement :
Prometheus le sait (`rate`, `increase`), c'est le fonctionnement normal d'un compteur.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

# Un registre à nous plutôt que le registre global : seul ce qui est déclaré ici sort,
# et rien de ce qu'une bibliothèque y enregistrerait d'elle-même.
REGISTRE = CollectorRegistry()
# Mémoire et processeur du worker : c'est ce qui dira quand une machine ne suffit plus.
ProcessCollector(registry=REGISTRE)

# Les issues d'un appel clos. « muet » est la panne du 30/07/2026 : l'appel finit
# normalement, l'assistante n'a rien dit (même règle que le contrôle de supervision).
SERVI, MUET, ECHEC, SECOURS = "servi", "muet", "echec", "secours"
PIPECAT, GPT_LIVE = "pipecat", "gpt-live"
# Les niveaux de supervision en nombre, pour qu'une règle d'alerte puisse les comparer.
_RANG = {"ok": 0, "attention": 1, "panne": 2}

APPELS_EN_COURS = Gauge(
    "helmane_appels_en_cours",
    "Appels dont le flux audio est ouvert, par établissement.",
    ["etablissement"], registry=REGISTRE,
)
APPELS_TERMINES = Counter(
    "helmane_appels_termines",
    "Appels clos, par établissement, moteur et issue (servi, muet, echec, secours).",
    ["etablissement", "moteur", "issue"], registry=REGISTRE,
)
SECOURS_OUVERTS = Counter(
    "helmane_secours",
    "Appels que l'assistante a rendus au restaurant ou au répondeur, par motif.",
    ["etablissement", "motif"], registry=REGISTRE,
)
ERREURS = Counter(
    "helmane_erreurs_maillon",
    "Erreurs signalées pendant un appel Pipecat, par maillon (voix, modele, transcription).",
    ["etablissement", "maillon"], registry=REGISTRE,
)
# Les seuils de la supervision (attention 2,5 s, panne 4 s) sont des bornes de seau :
# une règle d'alerte peut ainsi compter exactement les blancs qui les dépassent.
BLANCS = Histogram(
    "helmane_blanc_secondes",
    "Blanc ressenti par l'appelant entre la fin de sa phrase et la reprise de la voix.",
    ["etablissement"], buckets=(0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0),
    registry=REGISTRE,
)
DUREES = Histogram(
    "helmane_appel_duree_secondes",
    "Durée des appels clos.",
    ["etablissement"], buckets=(15, 30, 60, 120, 180, 300, 600, 1200),
    registry=REGISTRE,
)


def etablissement(tenant_id, call_sid: Optional[str] = None) -> str:
    """L'étiquette d'un établissement. Ni l'établissement d'essai de la recette
    (« essai ») ni le banc d'appels (« banc », `calls.PREFIXE_BANC`, qui passe par un vrai
    établissement) ne doivent se confondre avec le trafic d'un client."""
    from . import calls, essai

    if call_sid and call_sid.startswith(calls.PREFIXE_BANC):
        return "banc"
    if essai.est_d_essai(tenant_id):
        return "essai"
    return str(tenant_id)


def appel_ouvert(tenant_id, call_sid: Optional[str] = None) -> None:
    try:
        APPELS_EN_COURS.labels(etablissement(tenant_id, call_sid)).inc()
    except Exception as exc:
        logger.debug(f"[métriques] appel_ouvert : {exc}")


def appel_ferme(tenant_id, call_sid: Optional[str] = None) -> None:
    try:
        APPELS_EN_COURS.labels(etablissement(tenant_id, call_sid)).dec()
    except Exception as exc:
        logger.debug(f"[métriques] appel_ferme : {exc}")


def issue(status: str, duree: float, transcript: Optional[str],
          secours_motif: Optional[str], call_sid: Optional[str] = None) -> str:
    """Ce qu'est devenu l'appel, avec la règle du contrôle « Appels muets » : un appel
    `completed` de plus de `SUPERVISION_MUET_SECONDES` où le modèle n'a rien dit.

    Un appel que la vigie rend au restaurant est clos ICI avant que `/twilio/suite`
    n'écrive son motif en base (elle arrive après la fermeture du flux) : c'est la
    demande en mémoire (`renvoi.est_demande`) qui dit qu'il part en secours. Sans elle,
    il serait compté « servi » si le modèle avait parlé avant la panne."""
    from . import renvoi, supervision

    if secours_motif or renvoi.est_demande(call_sid):
        return SECOURS
    if status != "completed":
        return ECHEC
    if duree >= supervision.muet_secondes() and not supervision._a_repondu(transcript):
        return MUET
    return SERVI


def appel_clos(tenant_id, *, call_sid: Optional[str], status: str, duree: float,
               transcript: Optional[str], secours_motif: Optional[str],
               fournisseur: Optional[str], blancs_ms: Optional[list[int]]) -> None:
    """Appelée par `calls.finish_call`, une fois l'appel écrit en base."""
    try:
        cle = etablissement(tenant_id, call_sid)
        moteur = GPT_LIVE if fournisseur == GPT_LIVE else PIPECAT
        APPELS_TERMINES.labels(cle, moteur, issue(status, duree, transcript,
                                                  secours_motif, call_sid)).inc()
        DUREES.labels(cle).observe(max(0.0, float(duree or 0)))
        for blanc in blancs_ms or ():
            BLANCS.labels(cle).observe(blanc / 1000.0)
    except Exception as exc:
        logger.debug(f"[métriques] appel_clos : {exc}")


def secours(tenant_id, motif: str, call_sid: Optional[str] = None) -> None:
    try:
        SECOURS_OUVERTS.labels(etablissement(tenant_id, call_sid), motif or "inconnu").inc()
    except Exception as exc:
        logger.debug(f"[métriques] secours : {exc}")


def erreur(tenant_id, maillon: str) -> None:
    try:
        ERREURS.labels(etablissement(tenant_id), maillon).inc()
    except Exception as exc:
        logger.debug(f"[métriques] erreur : {exc}")


def _amorcer(cle: str) -> None:
    """Crée à zéro toutes les séries d'un établissement. Une série qui naît à 1, au
    premier secours, est invisible pour `increase()` : sans amorce, l'alerte « un appel
    en secours » ne partirait pas pour le premier secours depuis le déploiement — au
    trafic actuel, presque tous."""
    from . import renvoi

    APPELS_EN_COURS.labels(cle)
    for moteur in (PIPECAT, GPT_LIVE):
        for fin in (SERVI, MUET, ECHEC, SECOURS):
            APPELS_TERMINES.labels(cle, moteur, fin)
    for motif in renvoi.LIBELLES:
        SECOURS_OUVERTS.labels(cle, motif)
    for maillon in (renvoi.VOIX, renvoi.MODELE, renvoi.TRANSCRIPTION):
        ERREURS.labels(cle, maillon)


class _EtatDuParc:
    """Ce qui se lit au moment de la relève plutôt que de se compter : le niveau de
    chaque contrôle de supervision (les 18 deviennent ainsi des alertes Grafana) et le
    nom de chaque établissement, pour que les tableaux de bord affichent des noms."""

    def collect(self):
        from . import supervision, tenants

        niveaux = GaugeMetricFamily(
            "helmane_supervision_niveau",
            "Niveau de chaque contrôle de /supervision : 0 ok, 1 attention, 2 panne.",
            labels=["controle"])
        try:
            etat = supervision.etat()
            for controle in etat["controles"]:
                niveaux.add_metric([controle["cle"]], _RANG.get(controle["niveau"], 1))
            niveaux.add_metric(["global"], _RANG.get(etat["niveau"], 1))
        except Exception as exc:
            # Pas de mesure n'est pas un feu vert : la série « global » disparaît, et
            # l'alerte « absence de donnée » de Grafana prend le relais.
            logger.warning(f"[métriques] supervision illisible : {exc}")
        yield niveaux

        noms = GaugeMetricFamily(
            "helmane_etablissement_info",
            "Nom de chaque établissement, à joindre aux autres séries par « etablissement ».",
            labels=["etablissement", "nom"])
        try:
            for tenant in tenants.list_all():
                cle = etablissement(tenant.id)
                noms.add_metric([cle, tenant.name], 1)
                _amorcer(cle)
        except Exception as exc:
            logger.warning(f"[métriques] établissements illisibles : {exc}")
        yield noms


REGISTRE.register(_EtatDuParc())


def exposition() -> bytes:
    """Le texte servi par `/metrics`. Lit la base (supervision, établissements) : à
    appeler hors de la boucle d'événements."""
    return generate_latest(REGISTRE)


def amorcer_le_parc() -> None:
    """Au démarrage : les séries de chaque établissement existent à zéro dès la
    première relève (voir `_amorcer`). Lit la base : hors de la boucle d'événements."""
    from . import tenants

    try:
        for tenant in tenants.list_all():
            _amorcer(etablissement(tenant.id))
    except Exception as exc:
        logger.warning(f"[métriques] amorce impossible : {exc}")
