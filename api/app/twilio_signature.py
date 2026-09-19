"""Vérification de la signature des requêtes Twilio (`X-Twilio-Signature`).

Le numéro d'un restaurant est public par définition. Sans cette vérification, n'importe
qui pouvait faire parler l'assistante : un POST forgé sur /twilio/sms payait un appel LLM
et créait réservations ou messages fantômes ; une connexion forgée sur /ws/voice réveillait
un GPU (0,80 $/h, jusqu'à quatre). Relevé par l'audit du 18/09/2026 — la ligne « valider la
signature » attendait dans ARCHITECTURE.md depuis juillet.

Comment Twilio signe : HMAC-SHA1, clé = Auth Token du compte, message = URL publique
complète (telle que configurée dans la console, requête comprise) suivie des paramètres
POST concaténés clé + valeur, triés par clé ; encodé en base64. Reproduit ici avec la
bibliothèque standard — vérifié contre le vecteur de la documentation Twilio.

Trois modes, par `TWILIO_SIGNATURE` :
- `enforce` : une signature absente ou fausse vaut 403 (ou refus de la poignée de main) ;
- `log` : on laisse passer mais on compte et on journalise — pour observer 48 h après
  un déploiement que l'URL publique reconstruite est bien celle que Twilio signe ;
- `off` : rien n'est vérifié (développement local, sans Twilio).
Défaut : `enforce` dès qu'un jeton est configuré, `log` sinon — le code est sûr par
défaut, et l'observation se demande explicitement.

Pourquoi reconstruire l'URL publique : derrière Caddy, uvicorn voit `http://api:8000/…`,
pas `https://app.helmane.fr/…`, et il ne fait confiance aux en-têtes de proxy que depuis
127.0.0.1 — or Caddy arrive par le réseau Docker. `PUBLIC_URL` tranche ; à défaut on
dérive de `PUBLIC_WS_URL`, puis des en-têtes `X-Forwarded-*`, puis de la requête.
"""
import base64
import hashlib
import hmac
import os
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request, WebSocket
from loguru import logger

EN_TETE = "x-twilio-signature"

# Compteurs depuis le démarrage du processus, lus par la supervision. En mémoire et
# rien d'autre : écrire en base à chaque requête refusée offrirait à l'attaquant un
# moyen de faire travailler SQLite en boucle.
_compteur: dict = {"acceptees": 0, "refusees": 0, "derniere_refusee": None}


def signature_attendue(jeton: str, url: str, params: Optional[dict] = None) -> str:
    """Ce que Twilio a dû envoyer pour cette URL et ces paramètres."""
    charge = url + "".join(cle + str(valeur) for cle, valeur in sorted((params or {}).items()))
    empreinte = hmac.new(jeton.encode("utf-8"), charge.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(empreinte).decode("ascii")


def _variantes(url: str) -> list[str]:
    """L'URL telle quelle, et sa jumelle avec ou sans le port par défaut.

    Le validateur officiel accepte les deux : Twilio signe l'URL configurée dans la
    console, qui peut porter `:443` ou non selon la façon dont elle a été saisie."""
    parties = urlsplit(url)
    hote = parties.hostname or ""
    defaut = {"https": 443, "http": 80, "wss": 443, "ws": 80}.get(parties.scheme)
    if not hote or defaut is None:
        return [url]
    if parties.port is None:
        jumelle = parties._replace(netloc=f"{hote}:{defaut}")
    elif parties.port == defaut:
        jumelle = parties._replace(netloc=hote)
    else:
        return [url]
    return [url, urlunsplit(jumelle)]


def valide(jeton: str, url: str, params: Optional[dict], fournie: str) -> bool:
    """Vrai si `fournie` est la signature de Twilio pour cette requête."""
    if not jeton or not fournie:
        return False
    for candidate in _variantes(url):
        if hmac.compare_digest(signature_attendue(jeton, candidate, params), fournie):
            return True
    return False


def mode() -> str:
    """`enforce`, `log` ou `off`. Toute valeur inconnue vaut `enforce` : une faute de
    frappe doit fermer la porte, pas l'ouvrir."""
    demande = os.getenv("TWILIO_SIGNATURE", "").strip().lower()
    if demande in ("log", "off"):
        return demande
    if demande == "enforce":
        return "enforce"
    jeton = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return "enforce" if jeton else "log"


def _base_publique(request: Request) -> str:
    explicite = "".join(os.getenv("PUBLIC_URL", "").split()).rstrip("/")
    if explicite:
        return explicite
    flux = "".join(os.getenv("PUBLIC_WS_URL", "").split())
    if flux:
        parties = urlsplit(flux)
        schema = {"wss": "https", "ws": "http"}.get(parties.scheme, parties.scheme)
        return f"{schema}://{parties.netloc}"
    proto = request.headers.get("x-forwarded-proto")
    hote = request.headers.get("x-forwarded-host")
    if proto and hote:
        return f"{proto}://{hote}"
    return f"{request.url.scheme}://{request.url.netloc}"


def url_publique(request: Request) -> str:
    """L'URL que Twilio a signée : base publique + chemin + requête, sans rien de local."""
    url = _base_publique(request) + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


def url_ws_publique(websocket: WebSocket) -> str:
    """L'URL du flux média, celle-là même que le TwiML a annoncée (`<Stream url=…>`)."""
    flux = "".join(os.getenv("PUBLIC_WS_URL", "").split())
    if flux:
        return flux
    return f"wss://{websocket.url.netloc}{websocket.url.path}"


def _compter(ok: bool, chemin: str, mode_courant: str) -> None:
    if ok:
        _compteur["acceptees"] += 1
        return
    _compteur["refusees"] += 1
    _compteur["derniere_refusee"] = {
        "chemin": chemin, "mode": mode_courant,
        "quand": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def compteurs() -> dict:
    return dict(_compteur)


def remettre_a_zero() -> None:
    """Réservé aux tests."""
    _compteur.update({"acceptees": 0, "refusees": 0, "derniere_refusee": None})


async def exiger(request: Request) -> None:
    """Dépendance des webhooks Twilio. Lit le formulaire (Starlette le met en cache :
    la route le relit sans coût) et applique le mode en vigueur."""
    mode_courant = mode()
    if mode_courant == "off":
        return
    jeton = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    params: dict = {}
    if request.method == "POST":
        formulaire = await request.form()
        params = {cle: valeur for cle, valeur in formulaire.items() if isinstance(valeur, str)}
    fournie = request.headers.get(EN_TETE, "")
    ok = valide(jeton, url_publique(request), params, fournie)
    _compter(ok, request.url.path, mode_courant)
    if ok:
        return
    if mode_courant == "enforce":
        raise HTTPException(status_code=403, detail="Signature Twilio invalide")
    logger.warning(f"signature Twilio absente ou invalide sur {request.url.path} "
                   f"(mode log : requête acceptée) — URL vérifiée : {url_publique(request)}")


def verifier_ws(websocket: WebSocket) -> bool:
    """Vrai si la poignée de main du flux média peut être acceptée."""
    mode_courant = mode()
    if mode_courant == "off":
        return True
    jeton = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    ok = valide(jeton, url_ws_publique(websocket), {}, websocket.headers.get(EN_TETE, ""))
    _compter(ok, websocket.url.path, mode_courant)
    if ok or mode_courant == "log":
        if not ok:
            logger.warning("signature Twilio absente ou invalide sur la poignée de main "
                           f"/ws/voice (mode log : acceptée) — URL vérifiée : "
                           f"{url_ws_publique(websocket)}")
        return True
    return False
