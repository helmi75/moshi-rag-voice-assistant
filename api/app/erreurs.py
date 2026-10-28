"""Erreurs remontées à Sentry, avec leur établissement et leur appel (ASSISTANTE-163).

Jusqu'ici une erreur n'était qu'une ligne dans les journaux du conteneur, effacés au
redéploiement : on ne voyait pas « Deepgram a échoué 4 fois chez tel restaurant ».

Trois règles, dans le même esprit que `telegram.py` :

1. **Sans `SENTRY_DSN`, rien ne s'initialise et rien ne part.**
2. **Aucune donnée personnelle ne quitte le serveur** : ni corps de requête (les webhooks
   Twilio portent les numéros), ni cookies, ni variables locales (transcriptions, clés),
   ni numéro de téléphone dans un message. Ne partent que l'établissement, l'identifiant
   d'appel Twilio et la pile d'erreur (docs/RGPD.md).
3. **Un filtre qui lève ne casse jamais l'application, et ne laisse rien passer** :
   l'événement est jeté, jamais envoyé brut.
"""
import os
import re
from typing import Any, Optional

import sentry_sdk
from loguru import logger
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.loguru import LoguruIntegration

# Un numéro : un « + » facultatif, puis au moins neuf chiffres séparés ou non par des
# espaces, points ou tirets.
_NUMERO = re.compile(r"\+?\d[\d .-]{7,}\d")
_NOMS_SECRETS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN", "AUTH")
_LONGUEUR_MINI_SECRET = 6  # une valeur plus courte (« 1 », « on ») effacerait n'importe quoi


def actif() -> bool:
    return bool(os.getenv("SENTRY_DSN", "").strip())


def _secrets() -> list[str]:
    valeurs = {valeur.strip() for nom, valeur in os.environ.items()
               if any(m in nom.upper() for m in _NOMS_SECRETS)
               and len(valeur.strip()) >= _LONGUEUR_MINI_SECRET}
    return sorted(valeurs, key=len, reverse=True)  # la plus longue d'abord


def _nettoyer_texte(texte: Any) -> Any:
    if not isinstance(texte, str):
        return texte
    for secret in _secrets():
        texte = texte.replace(secret, "[secret]")
    return _NUMERO.sub("[numéro]", texte)


def _nettoyer_valeur(valeur: Any) -> Any:
    if isinstance(valeur, str):
        return _nettoyer_texte(valeur)
    if isinstance(valeur, dict):
        return {k: _nettoyer_valeur(v) for k, v in valeur.items()}
    if isinstance(valeur, (list, tuple)):
        return [_nettoyer_valeur(v) for v in valeur]
    return valeur


def _nettoyer_requete(requete: dict) -> None:
    for cle in ("data", "cookies", "query_string"):
        requete.pop(cle, None)
    # Seul l'agent utilisateur reste : les autres en-têtes portent signature et cookies.
    en_tetes = requete.get("headers") or {}
    requete["headers"] = {k: v for k, v in en_tetes.items() if k.lower() == "user-agent"}
    if "url" in requete:
        requete["url"] = _nettoyer_texte(requete["url"])


def _filtrer_fil(fil: dict) -> dict:
    if "message" in fil:
        fil["message"] = _nettoyer_texte(fil["message"])
    if "data" in fil:
        fil["data"] = _nettoyer_valeur(fil["data"])
    return fil


def _filtrer_evenement(event: dict) -> dict:
    if "request" in event:
        _nettoyer_requete(event["request"])
    event.pop("user", None)
    if "message" in event:
        event["message"] = _nettoyer_texte(event["message"])
    if "logentry" in event:
        event["logentry"] = _nettoyer_valeur(event["logentry"])
    for exception in (event.get("exception") or {}).get("values", []):
        if "value" in exception:
            exception["value"] = _nettoyer_texte(exception["value"])
        for cadre in (exception.get("stacktrace") or {}).get("frames", []):
            cadre.pop("vars", None)
    if "breadcrumbs" in event:
        fils = event["breadcrumbs"]
        for fil in (fils.get("values", []) if isinstance(fils, dict) else fils):
            _filtrer_fil(fil)
    if "extra" in event:
        event["extra"] = _nettoyer_valeur(event["extra"])
    return event


def avant_envoi(event: dict, _hint: Optional[dict] = None) -> Optional[dict]:
    try:
        return _filtrer_evenement(event)
    except Exception:  # jamais le contenu : il pourrait être ce qu'on n'a pas su nettoyer
        logger.warning("[erreurs] événement jeté : le filtre a échoué")
        return None


def avant_fil(fil: dict, _hint: Optional[dict] = None) -> Optional[dict]:
    try:
        return _filtrer_fil(fil)
    except Exception:
        return None


def initialiser(transport=None) -> bool:
    """À appeler une fois au démarrage. Vrai si Sentry est en service. Ne lève jamais."""
    if not actif():
        return False
    try:
        sentry_sdk.init(
            dsn=os.getenv("SENTRY_DSN", "").strip(),
            environment=os.getenv("SENTRY_ENVIRONMENT", "").strip() or "production",
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            traces_sample_rate=0,
            # Les journaux ne partent JAMAIS : sentry-sdk active seul ses intégrations
            # loguru et logging, qui feraient de chaque `logger.info` un fil d'Ariane et de
            # chaque `logger.error` (Pipecat compris) un événement. Or nos journaux recopient
            # la phrase de l'appelant (voice/rattrapage.py) et le résumé de l'appel
            # (resume.py) : le filtre n'ôte que numéros et secrets (relecture du sprint 5).
            disabled_integrations=[LoguruIntegration(), LoggingIntegration()],
            # Aucun fil d'Ariane, de quelque source : une requête httpx en laisserait un
            # avec son URL, et celle de Telegram porte le jeton du bot.
            max_breadcrumbs=0,
            before_send=avant_envoi,
            before_breadcrumb=avant_fil,
            transport=transport,
        )
    except Exception as exc:
        logger.warning(f"[erreurs] Sentry non initialisé ({type(exc).__name__})")
        return False
    return True


def contexte_appel(tenant_id: Any, call_sid: str) -> None:
    """Marque tout ce qui suit, dans cet appel, de son établissement et de son identifiant."""
    if not actif():
        return
    scope = sentry_sdk.get_isolation_scope()
    scope.set_tag("etablissement", str(tenant_id))
    scope.set_tag("appel", call_sid)


def capturer(exc: BaseException) -> None:
    """Remonte une exception déjà traitée ailleurs (le journal garde son rôle)."""
    if not actif():
        return
    try:
        sentry_sdk.capture_exception(exc)
    except Exception as echec:  # remonter une erreur ne doit jamais en faire une
        logger.warning(f"[erreurs] capture impossible ({type(echec).__name__})")
