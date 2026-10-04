"""« Rappelez-moi » : Marie appelle le numéro laissé sur le site (ASSISTANTE-119).

Le restaurateur qui découvre Helmane laisse son numéro sur la page d'accueil ;
l'assistante l'appelle dans la minute et répond comme pour un restaurant. C'est une
démonstration, servie par l'établissement de démonstration (`RAPPEL_ETABLISSEMENT`,
sinon celui de `TWILIO_NUMBER`) : mêmes outils, même voix, même carnet qu'un vrai appel.

C'est aussi la seule route du produit qui fait COMPOSER un numéro à la demande d'un
inconnu, sans compte. Ce module est donc d'abord une suite de refus :

- seuls les numéros de métropole passent (01 à 07 et 09) : ni surtaxés (08), ni
  outre-mer (un autre indicatif, et un tarif Twilio que personne n'a relevé), ni
  étrangers ;
- un numéro n'est rappelé que `RAPPEL_PAR_NUMERO_PAR_JOUR` fois par 24 h, une adresse
  ne demande que `RAPPEL_PAR_ADRESSE_PAR_HEURE` rappels par heure, et le site entier ne
  passe que `RAPPEL_MAX_PAR_JOUR` appels par 24 h — c'est ce plafond qui borne la
  facture, quoi qu'il arrive ;
- on n'appelle qu'aux heures de `RAPPEL_HEURES` : le numéro saisi peut être celui d'un
  tiers, qui n'a rien demandé ;
- Twilio coupe l'appel au bout de `RAPPEL_DUREE_MAX_SECONDES`.

Les plafonds se comptent sur la table `rappels` (une ligne par demande acceptée), pas sur
le journal des appels : un numéro qui ne décroche pas n'y laisse aucune trace, et on
pourrait le faire sonner dix fois.
"""
import asyncio
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Deque, Optional

import httpx
from loguru import logger

from . import db, horloge, notifications, renvoi, taches, tenants, twilio_region

# Ce que devient une demande acceptée.
LANCE = "lance"        # Twilio compose le numéro
DECROCHE = "decroche"  # la personne a décroché, Marie lui parle
ECHEC = "echec"        # Twilio a refusé de composer : aucun appel n'a eu lieu

# Combien de temps le téléphone du restaurateur sonne avant qu'on renonce.
SONNERIE_SECONDES = 25

# Les indicatifs d'outre-mer, écrits comme on les compose depuis la métropole. Ils se
# joignent par un autre indicatif international (+262, +590…) : composés en +33, ils
# n'aboutissent pas, et leur tarif n'a pas été relevé.
_OUTRE_MER = ("262", "263", "269", "508", "590", "594", "596", "639", "690", "691", "692",
              "693", "694", "696", "697", "70")
_METROPOLE = re.compile(r"0[1-79]\d{8}")
_DELAI = httpx.Timeout(15.0, connect=5.0)

# Ce que Marie dit en premier. Elle appelle quelqu'un qui attend son appel mais ne sait
# pas quoi dire : la consigne tient dans l'accueil, sinon l'essai commence par un blanc.
ACCUEIL = ("Bonjour, c'est Marie, l'assistante vocale d'Helmane. Vous avez demandé à être "
           "rappelé depuis notre site. Pour m'essayer, faites comme si vous appeliez le "
           "restaurant {nom} : réservez une table, par exemple.")

_verrou = threading.Lock()
# Un verrou à part pour le compteur par adresse : il se prend sur la boucle d'événements,
# qui porte l'audio des appels. Avec celui de `reserver`, tenu pendant toute une écriture
# SQLite, une base occupée aurait figé la boucle jusqu'à cinq secondes.
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


OK = Reponse(200, "appel", "Votre téléphone va sonner dans quelques secondes.")
NUMERO = Reponse(422, "numero", "Ce numéro n'est pas un numéro français de métropole. "
                                "Exemple : 06 12 34 56 78.")
DEJA = Reponse(429, "deja", "Marie a déjà rappelé ce numéro aujourd'hui. Réessayez demain.")
TROP = Reponse(429, "trop", "Trop de demandes depuis votre connexion. Réessayez dans une heure.")
PLAFOND = Reponse(503, "plafond", "Marie a déjà beaucoup rappelé aujourd'hui. Réessayez demain.")
INACTIF = Reponse(503, "inactif", "Le rappel n'est pas disponible pour le moment.")
PANNE = Reponse(502, "echec", "L'appel n'a pas pu partir. Réessayez dans un instant.")
SOUFFRANTE = Reponse(503, "panne", "Marie n'est pas disponible pour le moment. "
                                   "Réessayez dans quelques minutes.")


def _entier(nom: str, defaut: int) -> int:
    try:
        return max(0, int(os.getenv(nom, "").strip() or defaut))
    except ValueError:
        return defaut


def par_numero() -> int:
    return _entier("RAPPEL_PAR_NUMERO_PAR_JOUR", 2)


def par_adresse() -> int:
    return _entier("RAPPEL_PAR_ADRESSE_PAR_HEURE", 3)


def max_par_jour() -> int:
    """Le plafond du site entier, sur 24 h glissantes. Quinze appels de quatre minutes
    vers un portable : moins de quatre dollars par jour au tarif relevé le 01/10/2026,
    même si quelqu'un s'acharne."""
    return _entier("RAPPEL_MAX_PAR_JOUR", 15)


def duree_max() -> int:
    return _entier("RAPPEL_DUREE_MAX_SECONDES", 240) or 240


def heures() -> tuple[int, int]:
    """La plage où Marie rappelle, à l'heure du restaurant : « 8-22 » = de 8 h à 22 h."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", os.getenv("RAPPEL_HEURES", ""))
    if m and 0 <= int(m.group(1)) < int(m.group(2)) <= 24:
        return int(m.group(1)), int(m.group(2))
    return 8, 22


def ferme(instant: Optional[datetime] = None) -> Optional[Reponse]:
    """La réponse à donner hors de la plage de rappel, None si on peut appeler."""
    debut, fin = heures()
    heure = (instant or horloge.maintenant()).astimezone(horloge.FUSEAU).hour
    if debut <= heure < fin:
        return None
    return Reponse(503, "ferme", f"Marie rappelle entre {debut} h et {fin} h. "
                                 "Revenez à ce moment-là.")


def normaliser(brut) -> Optional[str]:
    """Le numéro au format international (`+33612345678`) si c'est un numéro de
    métropole qu'on accepte de composer, sinon None."""
    numero = re.sub(r"[\s.\-()  ]", "", str(brut or ""))
    for indicatif in ("+33", "0033"):
        if numero.startswith(indicatif):
            # « +33 (0)6 12 34 56 78 » : le zéro entre parenthèses s'écrit, il ne se
            # compose pas. On en retire un seul.
            reste = numero[len(indicatif):]
            numero = "0" + (reste[1:] if reste.startswith("0") else reste)
            break
    if not _METROPOLE.fullmatch(numero) or numero[1:].startswith(_OUTRE_MER):
        return None
    return "+33" + numero[1:]


def lisible(numero: str) -> str:
    """`+33612345678` → `06 12 34 56 78`."""
    national = "0" + numero[3:] if numero.startswith("+33") else numero
    return " ".join(national[i:i + 2] for i in range(0, len(national), 2))


def _base_publique() -> str:
    return "".join(os.getenv("PUBLIC_URL", "").split()).rstrip("/")


def actif() -> bool:
    """Le rappel peut-il partir ? Il lui faut l'accord (`RAPPEL_ACTIF`), les identifiants
    Twilio, et l'adresse publique où Twilio viendra chercher la suite de l'appel."""
    if os.getenv("RAPPEL_ACTIF", "1").strip().lower() in ("0", "false", "non", "off"):
        return False
    return bool(twilio_region.identifiants() and _base_publique())


def etablissement() -> Optional[tenants.Tenant]:
    """L'établissement que Marie représente pendant une démonstration, et dont la ligne
    s'affiche chez la personne rappelée."""
    choisi = os.getenv("RAPPEL_ETABLISSEMENT", "").strip()
    if choisi.isascii() and choisi.isdigit():
        return tenants.get_by_id(int(choisi))
    return tenants.get_by_phone(os.getenv("TWILIO_NUMBER", "").strip())


def pour_la_demonstration(tenant: tenants.Tenant) -> tenants.Tenant:
    """Le même établissement, avec l'accueil du rappel. L'accueil d'un restaurant
    (« Bonjour, Le Bouchon Doré ») n'a pas de sens quand c'est Marie qui appelle."""
    return replace(tenant, greeting=ACCUEIL.format(nom=tenant.name))


# ---- Les plafonds ---------------------------------------------------------------------

def adresse_bloquee(adresse: str) -> bool:
    """Compte cette demande pour cette adresse, et dit si elle dépasse le plafond horaire.
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
        if len(file) >= par_adresse():
            return True
        file.append(maintenant)
        return False


def reserver(numero: str) -> tuple[Optional[int], Optional[Reponse]]:
    """Inscrit la demande si les plafonds le permettent : (identifiant, None), sinon
    (None, le refus). Vérification et écriture sous un même verrou — deux demandes
    simultanées ne passent pas toutes les deux sous le plafond."""
    depuis = horloge.il_y_a(1)
    with _verrou, db.get_conn() as conn:
        total, du_numero = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(numero = ?), 0) FROM rappels
               WHERE created_at >= ? AND statut != ?""",
            (numero, depuis, ECHEC)).fetchone()
        if du_numero >= par_numero():
            return None, DEJA
        if total >= max_par_jour():
            return None, PLAFOND
        curseur = conn.execute("INSERT INTO rappels (numero, statut) VALUES (?, ?)",
                               (numero, LANCE))
        return curseur.lastrowid, None


def marquer(identifiant: int, statut: str, call_sid: Optional[str] = None) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE rappels SET statut = ?, call_sid = COALESCE(?, call_sid) WHERE id = ?",
                     (statut, call_sid, identifiant))


def noter_decroche(call_sid: Optional[str]) -> None:
    """La personne rappelée a décroché : le flux de l'appel vient de s'ouvrir."""
    if not call_sid:
        return
    with db.get_conn() as conn:
        conn.execute("UPDATE rappels SET statut = ? WHERE call_sid = ?", (DECROCHE, call_sid))


def compte_du_jour() -> int:
    """Les rappels passés sur 24 h — ce que le plafond du jour compare à sa limite."""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM rappels WHERE created_at >= ? AND statut != ?",
            (horloge.il_y_a(1), ECHEC)).fetchone()[0]


def purger(jours: int) -> int:
    """Efface les demandes plus vieilles que la durée de conservation (app/rgpd.py)."""
    with db.get_conn() as conn:
        return max(0, conn.execute("DELETE FROM rappels WHERE created_at < ?",
                                   (horloge.il_y_a(jours),)).rowcount)


def oublier(numero: str) -> int:
    """Efface les demandes de ce numéro (droit à l'effacement, app/rgpd.py)."""
    with db.get_conn() as conn:
        return max(0, conn.execute("DELETE FROM rappels WHERE numero = ?", (numero,)).rowcount)


def reinitialiser() -> None:
    """Oublie les demandes comptées par adresse — réservé aux tests."""
    with _verrou_adresses:
        _par_adresse.clear()


# ---- L'appel --------------------------------------------------------------------------

async def _composer(numero: str, ligne: str) -> str:
    """Demande à Twilio d'appeler `numero` en présentant `ligne`, et rend l'identifiant de
    l'appel. Quand la personne décroche, Twilio vient lire `/twilio/rappel`.

    L'appel est traité dans la région à laquelle on le demande (app/twilio_region.py) :
    c'est elle qui ouvrira le flux média, donc elle qui fait la latence de la démonstration."""
    sid, jeton = twilio_region.identifiants()
    async with httpx.AsyncClient(timeout=_DELAI, auth=(sid, jeton)) as client:
        reponse = await client.post(
            f"{twilio_region.hote()}/2010-04-01/Accounts/{sid}/Calls.json",
            data={"To": numero, "From": ligne, "Method": "POST",
                  "Url": _base_publique() + "/twilio/rappel",
                  "Timeout": str(SONNERIE_SECONDES), "TimeLimit": str(duree_max())})
        reponse.raise_for_status()
        return str(reponse.json().get("sid") or "")


async def demander(brut, adresse: str, instant: Optional[datetime] = None) -> Reponse:
    """Une demande de rappel venue du site. Rend toujours une réponse à afficher, ne lève
    jamais : la page ne doit pas montrer une erreur brute à un restaurateur."""
    if not actif():
        return INACTIF
    numero = normaliser(brut)
    if numero is None:
        return NUMERO
    if adresse_bloquee(adresse):
        logger.warning("[rappel] trop de demandes depuis une même adresse : refusée")
        return TROP
    hors_plage = ferme(instant)
    if hors_plage is not None:
        return hors_plage
    try:
        tenant = await db.hors_boucle(etablissement)
        if tenant is None:
            logger.warning("[rappel] aucun établissement de démonstration : rappel impossible")
            return INACTIF
        if renvoi.panne_recente(tenant.id):
            # L'assistante vient de tomber en panne (app/renvoi.py) : on n'appelle pas un
            # restaurateur pour lui faire entendre un silence. Rien n'est inscrit, son
            # essai du jour reste entier.
            logger.warning("[rappel] assistante en panne : demande refusée")
            return SOUFFRANTE
        identifiant, refus = await db.hors_boucle(reserver, numero)
        if refus is not None:
            if refus is PLAFOND:
                logger.warning(f"[rappel] plafond du jour atteint ({max_par_jour()}) : refusée")
            return refus
    except Exception as exc:
        logger.warning(f"[rappel] demande non inscrite ({type(exc).__name__}: {exc})")
        return PANNE
    try:
        call_sid = await _composer(numero, tenant.phone_number)
    except Exception as exc:
        # Le type seulement : le message d'une erreur HTTP porte l'adresse appelée.
        logger.warning(f"[rappel] Twilio n'a pas composé le numéro ({type(exc).__name__})")
        await _marquer_sans_lever(identifiant, ECHEC)
        return PANNE
    await _marquer_sans_lever(identifiant, LANCE, call_sid)
    logger.info(f"[rappel] Marie appelle un numéro en …{numero[-2:]} (appel {call_sid})")
    _prevenir(numero)
    return OK


async def _marquer_sans_lever(identifiant: int, statut: str, call_sid: Optional[str] = None) -> None:
    try:
        await db.hors_boucle(marquer, identifiant, statut, call_sid)
    except Exception as exc:
        logger.warning(f"[rappel] statut non noté ({type(exc).__name__}: {exc})")


def destinataire() -> str:
    """Qui est prévenu d'une demande de rappel : `RAPPEL_NOTIFIER`, sinon le compte
    super-admin."""
    return (os.getenv("RAPPEL_NOTIFIER", "").strip() or os.getenv("ADMIN_EMAIL", "").strip()).lower()


def _prevenir(numero: str) -> Optional[asyncio.Task]:
    """Un e-mail à Helmane : quelqu'un essaie Marie en ce moment. Sans SMTP, rien."""
    adresse = destinataire()
    if not notifications.actif() or "@" not in adresse:
        return None
    lien = _base_publique() + "/admin/calls"
    corps = "\n".join([
        f"Un visiteur du site a demandé à être rappelé au {lisible(numero)}.",
        "Marie l'appelle maintenant, pour une démonstration.", "",
        f"L'appel, s'il a décroché, est au journal : {lien}", "",
        "Ce numéro a été laissé pour ce rappel seulement.",
    ])
    return taches.lancer(notifications.envoyer([adresse], "Demande de rappel depuis le site", corps),
                         nom="e-mail demande de rappel")
