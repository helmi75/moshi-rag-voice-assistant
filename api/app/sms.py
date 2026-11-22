"""SMS de confirmation au client, après une réservation CONFIRMÉE (ASSISTANTE-154).

Le client qui réserve au téléphone ne garde aucune trace écrite. Décisions du 22/11/2026 :
- le SMS est signé du NOM DU RESTAURANT (expéditeur alphanumérique : sans enregistrement
  préalable en France, 11 caractères au plus) ;
- le 05 ne peut pas envoyer de SMS (capacité absente chez Twilio) : on ne répond jamais
  au numéro qui reçoit l'appel ;
- tarif relevé le 22/11/2026 : 0,0798 $ par segment (`COST_SMS_PAR_SEGMENT`).

Quatre règles, dans l'ordre où elles comptent :

1. **Éteint par défaut** : `tenants.sms_actif` vaut 0 tant que le super-admin n'a pas allumé
   l'établissement. Jamais pour une démonstration ni pour l'établissement d'essai : on y
   fait essayer des réservations, le numéro saisi n'est pas celui d'un vrai client.
2. **Jamais sur le chemin de l'appel** : `planifier` confie l'envoi au registre de tâches
   (app/taches.py) et rend la main, comme les e-mails du restaurateur.
3. **Jamais d'exception vers l'appel** : un refus de Twilio (21614 : numéro qui ne reçoit
   pas de SMS), un délai dépassé, un jeton absent se journalisent ; la réservation reste.
4. **Rien de personnel dans le journal** : ni le numéro (ses deux derniers chiffres au
   plus), ni le texte, ni le jeton.

Les SMS sont traités en `us1` (voir app/twilio_region.py), quelle que soit `TWILIO_REGION`.
"""
import asyncio
import os
import re
import unicodedata
from typing import Optional

import httpx
from loguru import logger

from . import db, taches

# Délai de l'appel à Twilio. Hors du chemin de l'appel, mais une tâche ne reste pas pendue.
_DELAI = httpx.Timeout(10.0)

LONGUEUR_EXPEDITEUR = 11
LONGUEUR_CORPS = 160   # un segment GSM-7 : au-delà, Twilio facture un deuxième segment

# Les caractères d'un segment GSM-7 que le texte peut contenir. Le reste (ê, â, î, ç, œ…) fait
# basculer le message en UCS-2 : 70 caractères par segment au lieu de 160, soit le double ou
# le triple du prix pour une confirmation. Volontairement étroit : pas de caractère de
# l'alphabet étendu (€, [, ]…), qui compte pour deux.
_GSM7 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            " .,:;!?'\"()-/+%&*#@=<>àéèùìòÉÇ")


def cout_par_segment() -> float:
    try:
        return float(os.getenv("COST_SMS_PAR_SEGMENT", "").strip() or 0.0798)
    except ValueError:
        return 0.0798


def _sans_accent(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in decompose if not unicodedata.combining(c))


def nom_expediteur(brut) -> Optional[str]:
    """Un nom d'expéditeur alphanumérique valable pour Twilio, ou None.

    Accents retirés, seulement `[A-Za-z0-9]`, les mots collés en CamelCase (« Le Bouchon
    Doré » → « LeBouchonDor »), 11 caractères au plus, au moins une lettre : un expéditeur
    fait de chiffres seuls serait pris pour un numéro de téléphone."""
    mots = re.split(r"[^A-Za-z0-9]+", _sans_accent(str(brut or "")))
    colle = "".join(m[:1].upper() + m[1:] for m in mots if m)[:LONGUEUR_EXPEDITEUR]
    return colle if re.search(r"[A-Za-z]", colle) else None


def expediteur(tenant) -> Optional[str]:
    """`sms_expediteur` s'il est posé et valable, sinon le nom du restaurant. None : rien
    ne part. Une valeur posée mais invalide ne se rabat pas sur le nom : c'est l'admin qui
    la refuse à l'enregistrement, et un réglage cassé doit se voir, pas se contourner."""
    pose = (getattr(tenant, "sms_expediteur", None) or "").strip()
    if pose:
        return nom_expediteur(pose)
    return nom_expediteur(getattr(tenant, "name", ""))


_MOBILE_FR = re.compile(r"\+33[67]\d{8}")


def destinataire(numero) -> Optional[str]:
    """Le numéro si c'est un mobile français en E.164 (`+336…`, `+337…`), sinon None. Un
    appel masqué, un fixe (qui ne lit pas un SMS) ou un numéro étranger (tarif et
    réglementation autres) ne reçoivent rien."""
    brut = str(numero or "").strip()
    return brut if _MOBILE_FR.fullmatch(brut) else None


def _gsm7(texte: str) -> str:
    """Le texte réduit aux caractères d'un segment GSM-7 : accents inconnus retirés, le
    reste d'un caractère inconnu écarté."""
    sortie = []
    for c in str(texte or ""):
        if c in _GSM7:
            sortie.append(c)
            continue
        simple = _sans_accent(c)
        sortie.append("".join(x for x in simple if x in _GSM7))
    return " ".join("".join(sortie).split())


def _numero_lisible(numero) -> Optional[str]:
    """`+33199000444` → `01 99 00 04 44`. None si ce n'est pas un numéro français."""
    brut = str(numero or "").strip()
    if not re.fullmatch(r"\+33[1-9]\d{8}", brut):
        return None
    chiffres = "0" + brut[3:]
    return " ".join(chiffres[i:i + 2] for i in range(0, 10, 2))


def corps(tenant, reservation: dict) -> str:
    """Le texte du SMS : GSM-7 seulement, 160 caractères au plus. Le nom du client est
    tronqué s'il le faut (c'est lui qui varie), puis la phrase finale est abandonnée en
    dernier recours."""
    resto = _gsm7(getattr(tenant, "name", ""))[:30].strip() or "Restaurant"
    nom = _gsm7(reservation.get("customer_name"))
    jour = str(reservation.get("date") or "")[:10]
    try:
        aaaa, mm, jj = jour.split("-")
        quand = f"{int(jj):02d}/{int(mm):02d}"
    except ValueError:
        quand = jour
    heure = str(reservation.get("time") or "")[:5].replace(":", "h")
    couverts = reservation.get("party_size")
    numero = _numero_lisible(getattr(tenant, "phone_number", None))
    suite = f" Pour modifier ou annuler, appelez le {numero}." if numero else ""

    def construire(nom_court: str, avec_suite: bool) -> str:
        return (f"{resto} : table pour {couverts} le {quand} à {heure} au nom de {nom_court}."
                + (suite if avec_suite else ""))

    for avec_suite in (True, False):
        for longueur in range(len(nom), 0, -1):
            texte = construire(nom[:longueur].strip(), avec_suite)
            if len(texte) <= LONGUEUR_CORPS:
                return texte
    return construire("", False)[:LONGUEUR_CORPS]


def _masque(numero: str) -> str:
    return "…" + numero[-2:] if numero else "?"


def _ajouter_cout(call_id: int, montant: float) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET cout_sms = COALESCE(cout_sms, 0) + ? WHERE id = ?",
                     (round(montant, 6), call_id))


async def envoyer(tenant, reservation: dict, call_id: Optional[int], numero: str,
                  expediteur_sms: str) -> bool:
    """Envoie le SMS et note son coût sur l'appel. NE LÈVE JAMAIS : rend vrai si Twilio
    l'a accepté."""
    from . import twilio_region

    try:
        acces = twilio_region.identifiants("us1")
        if acces is None:
            logger.warning("[sms] identifiants Twilio absents : aucun SMS envoyé")
            return False
        sid, jeton = acces
        async with httpx.AsyncClient(timeout=_DELAI, auth=(sid, jeton)) as client:
            reponse = await client.post(
                f"{twilio_region.hote('us1')}/2010-04-01/Accounts/{sid}/Messages.json",
                data={"From": expediteur_sms, "To": numero,
                      "Body": corps(tenant, reservation)})
        if reponse.status_code >= 400:
            try:
                code = reponse.json().get("code")
            except ValueError:
                code = None
            logger.warning(f"[sms] refusé par Twilio (HTTP {reponse.status_code}, "
                           f"code {code}) pour …{numero[-2:]}")
            return False
        try:
            segments = int(reponse.json().get("num_segments") or 1)
        except (ValueError, TypeError):
            segments = 1
        if call_id:
            await db.hors_boucle(_ajouter_cout, call_id, segments * cout_par_segment())
        logger.info(f"[sms] confirmation envoyée à {_masque(numero)} ({segments} segment)")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:   # noqa: BLE001 — un SMS raté ne casse jamais la réservation
        logger.warning(f"[sms] échec de l'envoi pour {_masque(numero)} : {type(exc).__name__}")
        return False


def planifier(tenant, reservation: dict, call_id: Optional[int], numero: str,
              expediteur_sms: str) -> asyncio.Task:
    """Ce que `llm.run_tool` appelle : lance l'envoi en tâche de fond et rend la main tout
    de suite. Les conditions (SMS allumé, pas de démonstration, mobile valide) sont
    vérifiées par l'appelant."""
    return taches.lancer(envoyer(tenant, reservation, call_id, numero, expediteur_sms),
                         nom=f"sms confirmation → {tenant.name}")
