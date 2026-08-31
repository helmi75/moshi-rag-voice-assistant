"""Durées de conservation, purge et droit à l'effacement (#22).

Le numéro de l'appelant et le contenu de sa conversation sont des données à caractère
personnel. Jusqu'ici la base les gardait **sans limite de durée** : un transcript de
juillet est toujours là en août, et le serait encore dans dix ans.

⚠️ **Certaines de ces données sont sensibles au sens de l'article 9 du RGPD.** Une
allergie alimentaire est une donnée de santé. Le mot « allergie » est explicitement
renforcé dans le vocabulaire Deepgram (voice/bot.py) et « demandes particulières » est
un champ libre : l'assistante *va* en collecter. Ce n'est pas une hypothèse, c'est une
conséquence du produit.

**On anonymise, on ne supprime pas.** Effacer les lignes d'appel détruirait l'historique
de facturation (le compteur de forfait compte des lignes dans `calls`) et les
statistiques. On vide donc les CHAMPS personnels en gardant la ligne : la date, la durée,
le coût et l'établissement restent, le numéro et le transcript partent. L'appel reste
comptable, l'appelant redevient inconnu.

Les réservations, elles, sont supprimées : une réservation anonymisée n'a plus d'usage,
ni pour le restaurateur ni pour la statistique.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import db


# --- Mention d'information au décroché ---------------------------------------
#
# L'appelant doit savoir DEUX choses avant de parler : qu'il s'adresse à une machine, et
# que ce qu'il dit est traité. Formulée pour être ajoutée à la suite de l'accueil du
# restaurateur, sans casser sa phrase — et volontairement courte : personne ne lit une
# politique de confidentialité au téléphone, et une mention qu'on n'écoute pas
# n'informe personne. C'est une information de premier niveau ; le détail (durées,
# droits, contact) vit dans `docs/RGPD.md` et sera repris sur la page publique.
MENTION = ("Cet accueil est assuré par un assistant vocal ; "
           "votre appel est traité pour votre réservation.")


def mention_active() -> bool:
    """Activée par défaut. La couper est une décision juridique, pas un réglage de
    confort : le mettre à 0 sans avoir informé les appelants autrement expose le
    restaurateur, pas seulement Helmane."""
    return os.getenv("RGPD_MENTION", "1").strip().lower() not in ("0", "false", "non")


def accueil(tenant) -> str:
    """Le texte RÉELLEMENT prononcé au décroché : celui du restaurateur, puis la mention.

    Passer par ici partout où l'accueil est dit — TTS pré-rendu, repli live, mode
    `gather` — garantit qu'aucun chemin ne décroche sans avoir informé. La mention
    entre aussi dans la clé de cache du WAV : la modifier invalide les accueils déjà
    rendus, au lieu de laisser tourner l'ancienne version en silence.
    """
    texte = (getattr(tenant, "greeting", "") or "").strip()
    if not mention_active():
        return texte
    return f"{texte} {MENTION}".strip()


def _jours(nom: str, defaut: int) -> int:
    try:
        return max(1, int(os.getenv(nom, "").strip() or defaut))
    except ValueError:
        return defaut


# Le transcript ne sert qu'au diagnostic : relire un appel qui s'est mal passé. La panne
# des appels muets a été trouvée en écoutant un appel du jour même. Au-delà d'un mois il
# ne sert plus à rien — et il contient tout ce que l'appelant a dit.
def jours_transcript() -> int:
    return _jours("RETENTION_TRANSCRIPT_JOURS", 30)


# Le numéro sert à rappeler un client ou à retrouver sa réservation. Il survit donc au
# transcript, mais pas indéfiniment.
def jours_numero() -> int:
    return _jours("RETENTION_NUMERO_JOURS", 90)


# Les réservations sont la donnée métier du restaurateur : il en a besoin après coup
# (litige, habitué qui revient, comptabilité). Un an après la date, plus.
def jours_reservation() -> int:
    return _jours("RETENTION_RESERVATION_JOURS", 365)


@dataclass(frozen=True)
class Purge:
    """Ce que la purge a RÉELLEMENT fait. Des compteurs, pas un « ok » : une purge qui
    ne trouve rien et une purge qui ne tourne pas se ressemblent trop."""
    transcripts: int
    numeros: int
    reservations: int
    quand: str

    @property
    def total(self) -> int:
        return self.transcripts + self.numeros + self.reservations


def purger() -> Purge:
    """Applique les durées de conservation. Idempotente : relancée dans la minute, elle
    ne trouve plus rien à faire."""
    with db.get_conn() as conn:
        transcripts = conn.execute(
            """UPDATE calls SET transcript = NULL, summary = NULL
               WHERE started_at < datetime('now', ?)
                 AND (transcript IS NOT NULL OR summary IS NOT NULL)""",
            (f"-{jours_transcript()} days",),
        ).rowcount
        numeros = conn.execute(
            """UPDATE calls SET caller_number = NULL
               WHERE started_at < datetime('now', ?) AND caller_number IS NOT NULL""",
            (f"-{jours_numero()} days",),
        ).rowcount
        # Sur la DATE de la réservation, pas sa création : une table réservée six mois
        # à l'avance ne doit pas être effacée avant d'avoir eu lieu.
        reservations = conn.execute(
            "DELETE FROM reservations WHERE date < date('now', ?)",
            (f"-{jours_reservation()} days",),
        ).rowcount
    resultat = Purge(
        transcripts=max(0, transcripts),
        numeros=max(0, numeros),
        reservations=max(0, reservations),
        quand=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _noter(resultat)
    return resultat


def _noter(resultat: Purge) -> None:
    """Trace la purge sur l'ardoise de supervision : une obligation de conservation qui
    n'est pas vérifiable n'est pas tenue, elle est seulement écrite quelque part."""
    from . import supervision

    supervision.noter("purge", {
        "transcripts": resultat.transcripts,
        "numeros": resultat.numeros,
        "reservations": resultat.reservations,
    })


def derniere_purge() -> Optional[tuple[dict, Optional[datetime]]]:
    from . import supervision

    return supervision.relire("purge")


# --- Droit à l'effacement (article 17) ---------------------------------------

def effacer_appelant(numero: str) -> Purge:
    """Efface à la demande tout ce qui rattache ce numéro à des appels et réservations.

    Même principe que la purge : les appels sont anonymisés (l'historique de facturation
    du restaurateur ne doit pas disparaître parce qu'un appelant exerce son droit), les
    réservations sont supprimées.

    ⚠️ Ce que cette fonction ne fait PAS, et qu'il faut savoir avant de répondre à une
    demande : elle ne touche ni aux journaux du conteneur, ni aux sauvegardes
    quotidiennes (`/opt/backups`), ni au journal d'appels de Twilio. Une réponse honnête
    à une demande d'effacement doit mentionner que les sauvegardes conservent la donnée
    jusqu'à leur propre expiration — 14 jours de rétention côté sauvegarde.
    """
    numero = (numero or "").strip()
    if not numero:
        raise ValueError("numéro vide : refus d'effacer au hasard")
    with db.get_conn() as conn:
        appels = conn.execute(
            """UPDATE calls SET caller_number = NULL, transcript = NULL, summary = NULL
               WHERE caller_number = ?""",
            (numero,),
        ).rowcount
        reservations = conn.execute(
            "DELETE FROM reservations WHERE customer_phone = ?", (numero,)
        ).rowcount
    return Purge(
        transcripts=max(0, appels), numeros=max(0, appels),
        reservations=max(0, reservations),
        quand=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# --- Boucle de fond -----------------------------------------------------------

def intervalle_secondes() -> int:
    """0 désactive la purge. Une fois par jour suffit : les durées se comptent en jours,
    purger toutes les heures ne rapprocherait de rien."""
    return _jours("RETENTION_INTERVALLE_SECONDES", 86_400)


async def boucle() -> None:
    """Purge périodique, lancée au démarrage de l'application.

    La première purge a lieu tout de suite : au premier déploiement de cette
    fonctionnalité, la base contient des mois de transcripts qui auraient déjà dû
    disparaître. Attendre 24 h de plus n'aurait aucune justification.
    """
    import asyncio

    from loguru import logger

    intervalle = intervalle_secondes()
    if intervalle <= 0:
        return
    logger.info(f"rétention : purge toutes les {intervalle} s.")
    while True:
        try:
            resultat = await asyncio.to_thread(purger)
            if resultat.total:
                logger.info(
                    f"rétention : {resultat.transcripts} transcript(s), "
                    f"{resultat.numeros} numéro(s), {resultat.reservations} "
                    "réservation(s) purgés.")
        except Exception as exc:  # la boucle ne doit jamais mourir
            logger.warning(f"rétention : purge échouée ({exc}).")
        await asyncio.sleep(intervalle)
