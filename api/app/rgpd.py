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

from loguru import logger

from . import db


# --- Mention d'information au décroché ---------------------------------------
#
# L'appelant doit savoir DEUX choses avant de parler : qu'il s'adresse à une machine, et
# que ce qu'il dit est traité. Formulée pour être ajoutée à la suite de l'accueil du
# restaurateur, sans casser sa phrase — et volontairement courte : personne ne lit une
# politique de confidentialité au téléphone, et une mention qu'on n'écoute pas
# n'informe personne. C'est une information de premier niveau ; le détail (durées,
# droits, contact) vit dans `docs/RGPD.md` et sera repris sur la page publique.
_MENTION_BASE = ("Cet accueil est assuré par un assistant vocal ; "
                 "votre appel est traité pour votre réservation.")
_MENTION_ENREGISTRE = ("Cet accueil est assuré par un assistant vocal ; "
                       "votre appel est enregistré et traité pour votre réservation.")

# Conservé pour compatibilité de lecture : la mention réellement prononcée dépend de
# l'état de l'enregistrement, et se lit par `mention()`.
MENTION = _MENTION_BASE


def mention() -> str:
    """La mention RÉELLEMENT prononcée.

    Elle annonce l'enregistrement si et seulement s'il a lieu. C'est le même
    interrupteur qui pilote les deux (`enregistrement.actif()` exige `mention_active()`),
    donc ils ne peuvent pas diverger : on n'enregistre jamais sans l'avoir dit, et on ne
    prétend jamais enregistrer sans le faire.

    ⚠️ Réserve portée au registre (docs/RGPD.md §8) : la finalité réelle de l'audio est
    le DIAGNOSTIC, pas la réservation. Nommer une finalité incomplète est un défaut de
    transparence — formulation retenue par Helmi le 31/08/2026, à faire valider.
    """
    from .voice import enregistrement

    return _MENTION_ENREGISTRE if enregistrement.actif() else _MENTION_BASE


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
    return f"{texte} {mention()}".strip()


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
# La voix est la donnée la plus sensible du produit. Sa durée est donc PLAFONNÉE par
# celle de la transcription : un enregistrement ne survit jamais au texte qu'il a produit.
# Un réglage distrait sur `RETENTION_ENREGISTREMENT_JOURS` ne peut pas allonger la
# conservation de la voix au-delà de ce que le registre annonce pour le transcript.
def jours_enregistrement() -> int:
    return min(_jours("RETENTION_ENREGISTREMENT_JOURS", 30), jours_transcript())


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
    enregistrements: int = 0
    messages: int = 0

    @property
    def total(self) -> int:
        return (self.transcripts + self.numeros + self.reservations
                + self.enregistrements + self.messages)


def purger() -> Purge:
    """Applique les durées de conservation. Idempotente : relancée dans la minute, elle
    ne trouve plus rien à faire."""
    with db.get_conn() as conn:
        transcripts = conn.execute(
            """UPDATE calls SET transcript = NULL, summary = NULL, journal = NULL
               WHERE started_at < datetime('now', ?)
                 AND (transcript IS NOT NULL OR summary IS NOT NULL
                      OR journal IS NOT NULL)""",
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
        # Un message contient le NOM et la DEMANDE de l'appelant, donc de la donnée
        # personnelle en clair — parfois plus parlante qu'un transcript, puisqu'elle est
        # résumée. Il suit donc la même durée que le transcript, et le numéro celle du
        # numéro. Un message plus vieux que ça n'a de toute façon plus d'usage : un
        # rappel promis il y a un mois ne se rattrape pas.
        messages_vides = conn.execute(
            """UPDATE messages SET subject = 'Message effacé (durée de conservation)',
                   details = NULL, customer_name = NULL
               WHERE created_at < datetime('now', ?) AND details IS NOT NULL""",
            (f"-{jours_transcript()} days",),
        ).rowcount
        conn.execute(
            """UPDATE messages SET caller_number = NULL
               WHERE created_at < datetime('now', ?) AND caller_number IS NOT NULL""",
            (f"-{jours_numero()} days",),
        )
    resultat = Purge(
        transcripts=max(0, transcripts),
        numeros=max(0, numeros),
        reservations=max(0, reservations),
        quand=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        enregistrements=_purger_enregistrements(),
        messages=max(0, messages_vides),
    )
    _noter(resultat)
    return resultat


def _purger_enregistrements() -> int:
    """Efface les fichiers audio expirés. **Piloté par la base, jamais par le répertoire.**

    On demande à SQLite quels appels ont dépassé leur durée, puis on reconstruit les
    chemins côté serveur. Balayer le dossier serait plus court et plus dangereux : on
    effacerait ce qui s'y trouve, y compris ce qu'on n'y a pas mis.

    Un balayage complémentaire ramasse les orphelins — fichiers dont la ligne a disparu,
    ce qui arrive après une restauration de sauvegarde (la base revient, les fichiers non,
    et l'inverse). Sans lui, ces fichiers resteraient indéfiniment, hors de toute durée
    de conservation.

    Tout est sous `try/except` : une purge de fichiers qui échoue ne doit jamais empêcher
    la purge SQL, qui est celle que le registre promet.
    """
    from . import calls
    from .voice import enregistrement

    supprimes = 0
    try:
        expires = calls.appels_avec_enregistrement(jours_enregistrement())
        for appel in expires:
            supprimes += enregistrement.supprimer(appel["tenant_id"], appel["id"])
        calls.oublier_enregistrements([a["id"] for a in expires])
    except Exception as exc:
        logger.warning(f"rétention : purge des enregistrements échouée ({exc})")
    try:
        supprimes += _purger_orphelins()
    except Exception as exc:
        logger.warning(f"rétention : balayage des orphelins échoué ({exc})")
    return supprimes


def _purger_orphelins() -> int:
    """Fichiers audio plus vieux que la durée de conservation dont aucune ligne ne parle.

    Filet de sécurité, pas mécanisme principal : la base reste la source de vérité. On
    se fie ici à la date du fichier, faute de mieux — c'est précisément le cas où la
    ligne n'existe plus."""
    import time

    from .voice import enregistrement

    dossier = enregistrement.dossier()
    if not dossier.exists():
        return 0
    limite = time.time() - jours_enregistrement() * 86_400
    supprimes = 0
    for fichier in dossier.rglob("*.ulaw"):
        try:
            if fichier.stat().st_mtime < limite:
                fichier.unlink()
                supprimes += 1
        except OSError:
            continue
    return supprimes


def _noter(resultat: Purge) -> None:
    """Trace la purge sur l'ardoise de supervision : une obligation de conservation qui
    n'est pas vérifiable n'est pas tenue, elle est seulement écrite quelque part."""
    from . import supervision

    supervision.noter("purge", {
        "transcripts": resultat.transcripts,
        "numeros": resultat.numeros,
        "reservations": resultat.reservations,
        "enregistrements": resultat.enregistrements,
        "messages": resultat.messages,
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

    from . import calls as calls_mod
    from .voice import enregistrement

    # Les fichiers d'abord, AVANT d'anonymiser : c'est `caller_number` qui relie un
    # numéro à ses appels, donc à ses enregistrements. Une fois mis à NULL, le lien est
    # rompu et les fichiers deviennent des orphelins que plus rien ne désigne.
    fichiers = 0
    try:
        concernes = calls_mod.appels_d_un_numero(numero)
        for appel in concernes:
            fichiers += enregistrement.supprimer(appel["tenant_id"], appel["id"])
        calls_mod.oublier_enregistrements([a["id"] for a in concernes])
    except Exception as exc:
        logger.warning(f"effacement : enregistrements non supprimés ({exc})")

    with db.get_conn() as conn:
        appels = conn.execute(
            """UPDATE calls SET caller_number = NULL, transcript = NULL, summary = NULL,
                   journal = NULL
               WHERE caller_number = ?""",
            (numero,),
        ).rowcount
        reservations = conn.execute(
            "DELETE FROM reservations WHERE customer_phone = ?", (numero,)
        ).rowcount
        # Les messages sont SUPPRIMÉS, pas anonymisés : contrairement à un appel, ils ne
        # portent aucune valeur comptable. Un message vidé de son objet et de son numéro
        # n'est plus qu'une ligne que le restaurateur ne peut ni traiter ni comprendre.
        messages_effaces = conn.execute(
            "DELETE FROM messages WHERE caller_number = ?", (numero,)
        ).rowcount
    return Purge(
        transcripts=max(0, appels), numeros=max(0, appels),
        reservations=max(0, reservations),
        quand=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        enregistrements=fichiers,
        messages=max(0, messages_effaces),
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
