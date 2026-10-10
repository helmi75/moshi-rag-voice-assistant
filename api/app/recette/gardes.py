"""Ce qui interdit de lancer une recette automatique, ou de composer un numéro.

Non négociables (Helmi, 10/10/2026) : jamais pendant un appel réel, jamais la nuit, un
passage par déploiement au plus, et seulement vers nos propres numéros.
"""
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import db, essai, horloge

DUREE_DU_JETON = 600        # secondes : un appel d'essai part dans la minute


def secret() -> str:
    """`RECETTE_JETON` : sans lui, rien de la recette automatique n'existe en production.
    Trop court, il vaut absent : un secret devinable ouvrirait `/ws/recette`."""
    valeur = os.getenv("RECETTE_JETON", "").strip()
    return valeur if len(valeur) >= 32 else ""


def active() -> bool:
    return bool(secret()) and essai.etablissement_id() is not None


def heures() -> tuple[int, int]:
    try:
        debut, fin = (int(h) for h in os.getenv("RECETTE_HEURES", "").strip().split("-"))
        if 0 <= debut < fin <= 24:
            return debut, fin
    except ValueError:
        pass
    return 8, 22


def la_nuit(instant: Optional[datetime] = None) -> Optional[str]:
    instant = instant or horloge.maintenant()
    debut, fin = heures()
    if debut <= instant.hour < fin:
        return None
    return f"il est {instant:%H h %M} : pas d'appel d'essai hors de {debut} h-{fin} h"


def appel_reel_en_cours() -> Optional[str]:
    """Un appel d'un vrai établissement ouvert depuis moins d'un quart d'heure et pas
    encore clos : la recette charge le même processus, elle attend."""
    depuis = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_conn() as conn:
        lignes = conn.execute(
            "SELECT id, tenant_id FROM calls WHERE ended_at IS NULL AND started_at >= ?", (depuis,)).fetchall()
    reels = [ligne["id"] for ligne in lignes if not essai.est_d_essai(ligne["tenant_id"])]
    return f"appel réel en cours (n° {reels[0]})" if reels else None


def demarrage() -> str:
    """Ce qui identifie le démarrage du conteneur : un redéploiement le recrée, donc le
    change. Sert à ne jouer qu'un passage par déploiement."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            hote = f.read().strip()
        with open("/proc/1/stat", encoding="ascii") as f:
            lance = f.read().rsplit(")", 1)[1].split()[19]
        return f"{hote[:8]}-{lance}"
    except (OSError, IndexError):
        return ""


def deja_joue(passages: list[dict], marque: Optional[str] = None) -> Optional[str]:
    marque = demarrage() if marque is None else marque
    if marque and any(p.get("demarrage") == marque and p.get("etage2") for p in passages):
        return "un passage de vrais appels a déjà été joué pour ce déploiement"
    return None


def destinataire_refuse(numero: str, nos_numeros: set[str], attendu: Optional[str]) -> Optional[str]:
    """On ne compose QUE le numéro de l'établissement d'essai, et seulement s'il est à nous
    chez Twilio. Tout autre destinataire est refusé, quoi qu'on demande au script."""
    if not numero or numero != attendu:
        return "le destinataire n'est pas le numéro de l'établissement d'essai"
    if numero not in nos_numeros:
        return "le destinataire n'est pas un de nos numéros chez Twilio"
    return None


# ---- Le jeton de la jambe qui appelle ---------------------------------------------------

def jeton(scenario: str, nonce: str) -> str:
    return hmac.new(secret().encode(), f"{scenario}:{nonce}".encode(), hashlib.sha256).hexdigest()


def nouveau_nonce() -> str:
    return f"{int(time.time())}-{os.urandom(8).hex()}"


_nonces_vus: dict[str, float] = {}


def jeton_valide(scenario: str, nonce: str, fourni: str) -> bool:
    """Le bon secret, un jeton de moins de dix minutes, et qui n'a jamais servi."""
    if not secret() or not scenario or not nonce or not fourni:
        return False
    if not hmac.compare_digest(jeton(scenario, nonce), fourni):
        return False
    try:
        age = time.time() - int(nonce.split("-", 1)[0])
    except ValueError:
        return False
    if not 0 <= age <= DUREE_DU_JETON or nonce in _nonces_vus:
        return False
    for vieux in [n for n, quand in _nonces_vus.items() if time.time() - quand > DUREE_DU_JETON]:
        del _nonces_vus[vieux]
    _nonces_vus[nonce] = time.time()
    return True
