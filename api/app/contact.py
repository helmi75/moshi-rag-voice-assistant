"""« Nous écrire » : le formulaire de contact de la page d'accueil (ASSISTANTE-185).

Un prospect ou un curieux écrit à Helmane sans passer par « Rappelez-moi ». Le message est
GARDÉ EN BASE et se lit dans l'admin (`/admin/contacts`) : l'e-mail n'est qu'une alerte,
et le SMTP peut très bien ne pas être configuré en production — un message ne doit pas se
perdre pour autant.

Comme « Rappelez-moi », la route est ouverte à tout le monde : ce module est d'abord une
suite de refus (champs bornés, plafond par adresse et par heure, plafond du site par
24 h). Aucune adresse IP n'est stockée : le plafond horaire vit en mémoire.
"""
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Optional

from loguru import logger

from . import db, horloge, notifications, rappel, taches

PAR_ADRESSE_PAR_HEURE = 3
MAX_PAR_JOUR = 50

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_verrou = threading.Lock()
# Verrou propre au compteur par adresse : il se prend sur la boucle d'événements, qui
# porte l'audio des appels (même raison que dans rappel.py).
_verrou_adresses = threading.Lock()
_par_adresse: dict[str, Deque[float]] = defaultdict(deque)
_ADRESSES_MAX = 5000


@dataclass(frozen=True)
class Reponse:
    """Ce que la page reçoit : un code HTTP, un état lisible par le script, et la phrase
    affichée au visiteur."""
    code: int
    etat: str
    message: str


OK = Reponse(200, "envoye", "Message envoyé. Nous vous répondons par e-mail.")
TROP = Reponse(429, "trop", "Trop de messages depuis votre connexion. Réessayez dans une heure.")
PLAFOND = Reponse(503, "plafond", "Beaucoup de messages aujourd'hui, écrivez-nous plus tard.")
PANNE = Reponse(502, "echec", "Le message n'a pas pu partir. Réessayez dans un instant.")


def valider(corps: dict) -> tuple[Optional[dict], dict[str, str]]:
    """(les champs nettoyés, {}) ou (None, {champ: phrase française})."""
    erreurs: dict[str, str] = {}
    champs: dict[str, str] = {}
    for nom in ("nom", "email", "telephone", "etablissement", "message"):
        valeur = corps.get(nom)
        if valeur is not None and not isinstance(valeur, str):
            erreurs[nom] = "Ce champ est illisible."
        champs[nom] = valeur.strip() if isinstance(valeur, str) else ""
    for nom in ("nom", "email", "telephone", "etablissement"):
        if any(c in champs[nom] for c in "\r\n"):
            erreurs.setdefault(nom, "Ce champ tient sur une seule ligne.")
    if not 1 <= len(champs["nom"]) <= 100:
        erreurs.setdefault("nom", "Indiquez votre nom (100 caractères au plus).")
    if len(champs["email"]) > 254 or not _EMAIL.match(champs["email"]):
        erreurs.setdefault("email", "Cette adresse e-mail semble incorrecte.")
    if len(champs["telephone"]) > 30:
        erreurs.setdefault("telephone", "Ce numéro est trop long (30 caractères au plus).")
    if len(champs["etablissement"]) > 120:
        erreurs.setdefault("etablissement", "Ce nom est trop long (120 caractères au plus).")
    if not 10 <= len(champs["message"]) <= 2000:
        erreurs.setdefault("message", "Votre message doit faire entre 10 et 2000 caractères.")
    return (None, erreurs) if erreurs else (champs, {})


def adresse_bloquee(adresse: str) -> bool:
    """Compte ce message pour cette adresse, et dit si elle dépasse le plafond horaire.
    En mémoire : un redémarrage l'efface, le plafond du jour (en base) reste."""
    maintenant = time.monotonic()
    with _verrou_adresses:
        if len(_par_adresse) > _ADRESSES_MAX:
            # Un balayage depuis des milliers d'adresses ne doit pas faire grossir la
            # mémoire du processus qui porte les appels : on oublie les plus anciennes.
            for cle in [c for c, f in _par_adresse.items()
                        if not f or maintenant - f[-1] > 3600]:
                del _par_adresse[cle]
        file = _par_adresse[adresse]
        while file and maintenant - file[0] > 3600:
            file.popleft()
        if len(file) >= PAR_ADRESSE_PAR_HEURE:
            return True
        file.append(maintenant)
        return False


def enregistrer(champs: dict) -> Optional[int]:
    """Inscrit le message, sauf si le site en a déjà reçu `MAX_PAR_JOUR` en 24 h (None).
    Comptage et écriture sous un même verrou."""
    with _verrou, db.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM contacts WHERE created_at >= ?",
                             (horloge.il_y_a(1),)).fetchone()[0]
        if total >= MAX_PAR_JOUR:
            return None
        return conn.execute(
            """INSERT INTO contacts (nom, email, telephone, etablissement, message)
               VALUES (?, ?, ?, ?, ?)""",
            (champs["nom"], champs["email"], champs["telephone"] or None,
             champs["etablissement"] or None, champs["message"])).lastrowid


def lister() -> list[dict]:
    """Du plus récent au plus ancien."""
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM contacts ORDER BY created_at DESC, id DESC")]


def marquer_lu(identifiant: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE contacts SET lu_at = ? WHERE id = ? AND lu_at IS NULL",
                     (horloge.utc_iso(), identifiant))


def purger(jours: int) -> int:
    """Efface les messages plus vieux que la durée de conservation (app/rgpd.py)."""
    with db.get_conn() as conn:
        return max(0, conn.execute("DELETE FROM contacts WHERE created_at < ?",
                                   (horloge.il_y_a(jours),)).rowcount)


def reinitialiser() -> None:
    """Oublie les messages comptés par adresse — réservé aux tests."""
    with _verrou_adresses:
        _par_adresse.clear()


def _prevenir(champs: dict):
    """Un e-mail à Helmane. Le sujet est FIXE : rien de ce que le visiteur a écrit
    n'entre dans un en-tête (ni injection d'en-têtes, ni donnée dans les aperçus)."""
    destinataire = rappel.destinataire()
    if not notifications.actif() or "@" not in destinataire:
        return None
    lien = rappel._base_publique() + "/admin/contacts"
    corps = "\n".join([
        "Un visiteur a écrit depuis le site.", "",
        f"Nom : {champs['nom']}",
        f"E-mail : {champs['email']}",
        f"Téléphone : {champs['telephone'] or '—'}",
        f"Établissement : {champs['etablissement'] or '—'}", "",
        champs["message"], "",
        f"À lire dans l'admin : {lien}",
    ])
    return taches.lancer(notifications.envoyer([destinataire], "Message reçu depuis le site", corps),
                         nom="e-mail message du site")


async def demander(corps: dict, adresse: str) -> Reponse:
    """Un message venu du site. Rend toujours une réponse à afficher, ne lève jamais."""
    champs, erreurs = valider(corps)
    if champs is None:
        return Reponse(422, "champs", " ".join(erreurs.values()))
    if adresse_bloquee(adresse):
        logger.warning("[contact] trop de messages depuis une même adresse : refusé")
        return TROP
    try:
        identifiant = await db.hors_boucle(enregistrer, champs)
    except Exception as exc:
        logger.warning(f"[contact] message non inscrit ({type(exc).__name__}: {exc})")
        return PANNE
    if identifiant is None:
        logger.warning(f"[contact] plafond du jour atteint ({MAX_PAR_JOUR}) : refusé")
        return PLAFOND
    try:
        _prevenir(champs)
    except Exception as exc:
        logger.warning(f"[contact] alerte non lancée ({type(exc).__name__}: {exc})")
    return OK
