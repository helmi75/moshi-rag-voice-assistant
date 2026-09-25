"""Prévenir le restaurateur : un e-mail à chaque réservation prise, modifiée, annulée, et à
chaque message pris au téléphone.

Jusqu'ici il fallait se connecter à l'admin pour savoir qu'une table venait d'être prise :
le produit travaillait, personne ne le voyait. Une réservation que la salle découvre à
l'heure du service, un rappel promis que personne ne passe — c'est le restaurateur qui
passe pour quelqu'un qui ne rappelle pas.

Deux règles, et elles priment sur tout :

1. **Jamais sur le chemin de l'appel.** `planifier` confie l'envoi au registre de tâches
   (app/taches.py) et rend la main : le résultat de l'outil revient au modèle sans
   attendre le SMTP, sinon chaque réservation coûterait 0,5 à 3 s de blanc au téléphone.
2. **Jamais d'exception vers l'appel.** Un SMTP en panne se journalise ; il ne fait pas
   s'excuser l'assistante d'un « problème technique » ni perdre la réservation.

Destinataires : les comptes restaurateurs de l'établissement (ils ont déjà un e-mail) et,
en plus, `tenants.notify_email` si le gérant veut une boîte partagée (salle@…).
SMTP générique (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`,
`SMTP_SSL`) : aucun fournisseur imposé. Sans `SMTP_HOST`, rien ne part et rien ne casse.

Le prompt continue de dire au CLIENT que l'assistante n'envoie ni SMS ni e-mail : c'est
vrai — ceux-ci vont au restaurateur.
"""
import asyncio
import os
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from typing import Optional

from loguru import logger

from . import horloge, taches, users

EVENEMENTS = ("reservation_creee", "reservation_modifiee", "reservation_annulee", "message_pris")


def _config() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "").strip() or 587),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from": os.getenv("SMTP_FROM", "").strip(),
        "ssl": os.getenv("SMTP_SSL", "0").strip().lower() in ("1", "true", "oui"),
        "timeout": float(os.getenv("SMTP_TIMEOUT", "").strip() or 10),
    }


def actif() -> bool:
    """Un serveur et un expéditeur : sans les deux, on n'envoie rien — et on ne casse rien."""
    cfg = _config()
    return bool(cfg["host"] and cfg["from"])


def destinataires(tenant) -> list[str]:
    """Les comptes restaurateurs de l'établissement, puis l'adresse de notification
    éventuelle ; sans doublon, dans cet ordre. Le super-admin n'y est jamais : il
    n'appartient à aucun établissement."""
    adresses: list[str] = []
    for compte in users.list_users(tenant.id):
        if compte.email and compte.email not in adresses:
            adresses.append(compte.email)
    supplement = (getattr(tenant, "notify_email", None) or "").strip().lower()
    if supplement and supplement not in adresses:
        adresses.append(supplement)
    return adresses


def _date_lettres(iso) -> str:
    try:
        return horloge.en_toutes_lettres(date.fromisoformat(str(iso)))
    except (TypeError, ValueError):
        return str(iso)


def _heure(hhmm) -> str:
    texte = str(hhmm or "")
    return texte.replace(":", "h", 1) if ":" in texte else texte


def _lien(chemin: str) -> str:
    base = "".join(os.getenv("PUBLIC_URL", "").split()).rstrip("/")
    return f"{base}{chemin}" if base else ""


def _telephone(numero) -> str:
    return numero or "numéro masqué (pas de rappel possible)"


def _resa_lignes(r: dict) -> list[str]:
    lignes = [f"Nom : {r.get('customer_name') or '—'}",
              f"Date : {_date_lettres(r.get('date'))} à {_heure(r.get('time'))}",
              f"Couverts : {r.get('party_size')}",
              f"Téléphone : {_telephone(r.get('customer_phone'))}"]
    if r.get("notes"):
        lignes.append(f"Demandes : {r['notes']}")
    return lignes


def sujet_et_corps(evenement: str, tenant, donnees: dict) -> tuple[str, str]:
    """Texte brut, en français, lisible sur un téléphone entre deux services."""
    nom_resto = tenant.name
    pied = []
    if _lien("/admin"):
        pied.append(f"Réservations : {_lien('/admin/reservations')}")
        if donnees.get("appel_id"):
            pied.append(f"Réécouter l'appel : {_lien(f'/admin/calls/{donnees['appel_id']}')}")
    pied.append(f"— L'assistante téléphonique de {nom_resto}")

    if evenement == "reservation_creee":
        r = donnees["reservation"]
        sujet = (f"Nouvelle réservation — {r.get('customer_name')}, "
                 f"{_date_lettres(r.get('date'))} à {_heure(r.get('time'))}, {r.get('party_size')} couverts")
        corps = [f"Une réservation vient d'être prise au téléphone pour {nom_resto}.", ""]
        corps += _resa_lignes(r)
    elif evenement == "reservation_modifiee":
        avant, r = donnees.get("avant") or {}, donnees["reservation"]
        sujet = (f"Réservation modifiée — {r.get('customer_name')}, "
                 f"{_date_lettres(r.get('date'))} à {_heure(r.get('time'))}")
        corps = [f"Un client a modifié sa réservation par téléphone ({nom_resto}).", ""]
        if avant:
            corps.append(f"Avant : {_date_lettres(avant.get('date'))} à {_heure(avant.get('time'))}, "
                         f"{avant.get('party_size')} couverts")
        corps.append("Maintenant :")
        corps += _resa_lignes(r)
    elif evenement == "reservation_annulee":
        r = donnees["reservation"]
        sujet = (f"Réservation annulée — {r.get('customer_name')}, "
                 f"{_date_lettres(r.get('date'))} à {_heure(r.get('time'))}")
        corps = [f"Un client a annulé sa réservation par téléphone ({nom_resto}). La table est libérée.", ""]
        corps += _resa_lignes(r)
    elif evenement == "message_pris":
        sujet = f"Rappel promis — {donnees.get('subject') or 'message pris au téléphone'}"
        corps = [f"L'assistante a pris un message et a promis un rappel ({nom_resto}).", "",
                 f"Objet : {donnees.get('subject') or '—'}"]
        if donnees.get("details"):
            corps.append(f"Message : {donnees['details']}")
        if donnees.get("customer_name"):
            corps.append(f"De la part de : {donnees['customer_name']}")
        corps.append(f"Rappeler le : {_telephone(donnees.get('caller_number'))}")
    elif evenement == "carnet_injoignable":
        # SCRUM-87 : pendant la panne, l'assistante n'enregistre rien et prend des
        # messages. Le restaurateur doit le savoir avant le service, pas après.
        sujet = "resOS ne répond pas — les réservations téléphoniques deviennent des messages"
        corps = [f"Pendant un appel pour {nom_resto}, resOS n'a pas répondu "
                 f"({donnees.get('erreur') or 'sans détail'}).", "",
                 "L'assistante n'a annoncé AUCUNE réservation comme enregistrée : elle prend",
                 "un message et promet un rappel. Tant que resOS ne répond pas, chaque demande",
                 "de réservation arrivera ainsi, en message.", "",
                 "À faire : vérifier que resOS fonctionne, et rappeler les clients des messages.",
                 "Pas d'autre e-mail pour cette panne avant 30 minutes."]
    else:
        raise ValueError(f"évènement inconnu : {evenement!r}")
    return sujet, "\n".join(corps + [""] + pied)


def _envoyer_sync(adresses: list[str], sujet: str, corps: str) -> None:
    cfg = _config()
    message = EmailMessage()
    message["Subject"] = sujet
    message["From"] = cfg["from"]
    message["To"] = ", ".join(adresses)
    message.set_content(corps)
    contexte = ssl.create_default_context()
    if cfg["ssl"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=contexte) as serveur:
            if cfg["user"]:
                serveur.login(cfg["user"], cfg["password"])
            serveur.send_message(message)
        return
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as serveur:
        serveur.ehlo()
        if serveur.has_extn("starttls"):
            serveur.starttls(context=contexte)
            serveur.ehlo()
        if cfg["user"]:
            serveur.login(cfg["user"], cfg["password"])
        serveur.send_message(message)


async def notifier(tenant, evenement: str, donnees: dict) -> bool:
    """Envoie l'e-mail, dans un thread. Vrai s'il est parti. NE LÈVE JAMAIS : un SMTP en
    panne se journalise, il ne remonte pas jusqu'à l'appel en cours."""
    if not actif():
        return False
    try:
        adresses = await asyncio.to_thread(destinataires, tenant)
        if not adresses:
            logger.info(f"notification {evenement} : aucun destinataire pour {tenant.name}")
            return False
        sujet, corps = sujet_et_corps(evenement, tenant, donnees)
        await asyncio.to_thread(_envoyer_sync, adresses, sujet, corps)
        logger.info(f"notification {evenement} envoyée à {len(adresses)} adresse(s) ({tenant.name})")
        return True
    except Exception as exc:
        logger.warning(f"notification {evenement} non envoyée ({type(exc).__name__}: {exc}) "
                       "— l'appel n'en sait rien, c'est voulu")
        return False


def planifier(tenant, evenement: str, donnees: dict) -> Optional[asyncio.Task]:
    """Ce que `run_tool` appelle : lance l'envoi en tâche de fond et rend la main tout de
    suite. Rien n'est lancé si le SMTP n'est pas configuré."""
    if not actif():
        return None
    return taches.lancer(notifier(tenant, evenement, donnees),
                         nom=f"e-mail {evenement} → {tenant.name}")
