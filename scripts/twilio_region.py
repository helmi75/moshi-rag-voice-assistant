#!/usr/bin/env python3
"""Fait traiter les appels d'un numéro Twilio en Irlande plutôt qu'aux États-Unis — et
sait revenir en arrière.

Pourquoi, et ce que la région change : api/app/twilio_region.py, docs/TWILIO_SETUP.md § 8.
Sans dépendance. Les identifiants se lisent dans l'environnement, jamais affichés :

    set -a; source .env; set +a
    python3 scripts/twilio_region.py +33523550580                  # état — ne modifie rien
    python3 scripts/twilio_region.py +33523550580 --preparer ie1   # recopie les webhooks en Irlande
    python3 scripts/twilio_region.py +33523550580 --basculer ie1   # les appels passent par l'Irlande
    python3 scripts/twilio_region.py +33523550580 --basculer us1   # retour arrière

`--preparer` est sans effet sur les appels tant que le numéro n'a pas basculé.
`--basculer` refuse si le numéro n'a pas, dans la région visée, les webhooks qu'il a
aujourd'hui, ou si l'application n'accepte pas une requête signée par le jeton de cette
région : basculer dans ces conditions ferait refuser TOUS les appels.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# Région → (morceau du nom d'hôte, variable qui porte son jeton).
REGIONS = {"us1": ("", "TWILIO_AUTH_TOKEN"), "ie1": ("dublin.ie1.", "TWILIO_AUTH_TOKEN_IE1")}

# Ce qu'un numéro porte région par région, et qu'il faut donc recopier : champ lu → champ écrit.
WEBHOOKS = {"voice_url": "VoiceUrl", "voice_method": "VoiceMethod",
            "voice_fallback_url": "VoiceFallbackUrl", "voice_fallback_method": "VoiceFallbackMethod",
            "status_callback": "StatusCallback", "status_callback_method": "StatusCallbackMethod"}

# Une route signée qui ne fait rien quand l'enregistrement n'est pas « completed » : la
# sonde vérifie la signature sans rien déclencher.
ROUTE_SONDE = "/twilio/repondeur/pret"


def _hote(produit: str, region: str) -> str:
    return f"https://{produit}.{REGIONS[region][0]}twilio.com"


def _jeton(region: str) -> str:
    return os.getenv(REGIONS[region][1], "").strip()


def _requete(methode: str, url: str, sid: str, jeton: str,
             donnees: Optional[dict] = None) -> tuple[int, dict]:
    corps = urllib.parse.urlencode(donnees).encode() if donnees else None
    requete = urllib.request.Request(url, data=corps, method=methode)
    requete.add_header("Authorization",
                       "Basic " + base64.b64encode(f"{sid}:{jeton}".encode()).decode())
    try:
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            return reponse.status, json.loads(reponse.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def routage(numero: str, sid: str) -> Optional[str]:
    """La région où Twilio traite aujourd'hui les appels de ce numéro."""
    code, reponse = _requete("GET", f"{_hote('routes', 'us1')}/v2/PhoneNumbers/"
                             + urllib.parse.quote(numero), sid, _jeton("us1"))
    return reponse.get("voice_region") if code == 200 else None


def reglages(numero: str, sid: str, region: str) -> tuple[Optional[dict], str]:
    """Les réglages du numéro dans cette région, ou None et la raison."""
    if not _jeton(region):
        return None, f"{REGIONS[region][1]} n'est pas dans l'environnement"
    code, reponse = _requete(
        "GET", f"{_hote('api', region)}/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json?"
        + urllib.parse.urlencode({"PhoneNumber": numero}), sid, _jeton(region))
    if code == 401:
        return None, (f"{REGIONS[region][1]} refusé par {region} : ce n'est pas l'Auth Token de "
                      "cette région (le secret d'une clé d'API SK… ne convient pas)")
    if code != 200:
        return None, f"HTTP {code} {reponse.get('message') or ''}".strip()
    numeros = reponse.get("incoming_phone_numbers") or []
    return (numeros[0], "") if numeros else (None, f"le numéro n'apparaît pas en {region}")


def signature(jeton: str, url: str, donnees: dict) -> str:
    charge = url + "".join(cle + valeur for cle, valeur in sorted(donnees.items()))
    return base64.b64encode(hmac.new(jeton.encode(), charge.encode(), hashlib.sha1).digest()).decode()


def sonde(base: str, jeton: str) -> int:
    """Envoie à l'application une requête signée par ce jeton, comme Twilio le ferait, et
    rend le code HTTP : 204 = acceptée, 403 = l'application ne connaît pas ce jeton."""
    url, donnees = base + ROUTE_SONDE, {"RecordingStatus": "sonde"}
    requete = urllib.request.Request(url, data=urllib.parse.urlencode(donnees).encode(), method="POST")
    requete.add_header("X-Twilio-Signature", signature(jeton, url, donnees))
    try:
        with urllib.request.urlopen(requete, timeout=20) as reponse:
            return reponse.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _webhooks(numero: Optional[dict]) -> dict:
    return {champ: (numero or {}).get(champ) or "" for champ in WEBHOOKS}


def etat(numero: str, sid: str) -> Optional[str]:
    actuelle = routage(numero, sid)
    print(f"{numero} — appels traités en : {actuelle or 'inconnu (API Routes muette)'}")
    for region in REGIONS:
        trouve, raison = reglages(numero, sid, region)
        if trouve is None:
            print(f"  {region} : {raison}")
            continue
        webhooks = _webhooks(trouve)
        print(f"  {region} : appel → {webhooks['voice_url'] or '(aucun)'}"
              f" · secours → {webhooks['voice_fallback_url'] or '(aucun)'}")
    return actuelle


def preparer(numero: str, sid: str, cible: str) -> None:
    source = next(region for region in REGIONS if region != cible)
    modele, raison = reglages(numero, sid, source)
    if modele is None:
        sys.exit(f"Impossible de lire les réglages en {source} : {raison}.")
    voulu = _webhooks(modele)
    if not voulu["voice_url"]:
        sys.exit(f"Le numéro n'a aucun webhook d'appel en {source} : rien à recopier.")
    trouve, raison = reglages(numero, sid, cible)
    if trouve is None:
        sys.exit(f"Impossible de régler le numéro en {cible} : {raison}.")
    code, reponse = _requete(
        "POST", f"{_hote('api', cible)}/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{trouve['sid']}.json",
        sid, _jeton(cible), {WEBHOOKS[champ]: valeur for champ, valeur in voulu.items()})
    if code != 200:
        sys.exit(f"Twilio a refusé le réglage en {cible} : HTTP {code} {reponse.get('message') or ''}")
    print(f"✅ Webhooks recopiés de {source} vers {cible} (sans effet tant que le numéro n'a pas basculé).")


def basculer(numero: str, sid: str, cible: str) -> None:
    actuelle = routage(numero, sid)
    if actuelle == cible:
        print(f"Le numéro est déjà traité en {cible} : rien à faire.")
        return
    if actuelle not in REGIONS:
        sys.exit(f"Région actuelle illisible ou inconnue ({actuelle}) : on ne bascule pas à l'aveugle.")
    ici, raison = reglages(numero, sid, actuelle)
    if ici is None:
        sys.exit(f"Impossible de lire les réglages en {actuelle} : {raison}.")
    la_bas, raison = reglages(numero, sid, cible)
    if la_bas is None:
        sys.exit(f"Refus : {raison}.")
    if not _webhooks(la_bas)["voice_url"] or _webhooks(la_bas) != _webhooks(ici):
        sys.exit(f"Refus : en {cible}, le numéro n'a pas les mêmes webhooks qu'en {actuelle}. "
                 f"Lancer d'abord --preparer {cible}.")
    adresse = urllib.parse.urlsplit(_webhooks(la_bas)["voice_url"])
    code = sonde(f"{adresse.scheme}://{adresse.netloc}", _jeton(cible))
    if code != 204:
        sys.exit(f"Refus : l'application répond {code} à une requête signée par le jeton {cible} "
                 f"(204 attendu). Elle refuserait tous les appels : poser {REGIONS[cible][1]} "
                 "dans son .env et la redéployer d'abord.")
    code, reponse = _requete("POST", f"{_hote('routes', 'us1')}/v2/PhoneNumbers/"
                             + urllib.parse.quote(numero), sid, _jeton("us1"), {"VoiceRegion": cible})
    if code != 200 or reponse.get("voice_region") != cible:
        sys.exit(f"Twilio a refusé la bascule : HTTP {code} {reponse.get('message') or ''}")
    print(f"✅ {numero} : appels traités en {cible} (Twilio annonce jusqu'à cinq minutes de délai).")
    print(f"   Poser TWILIO_REGION={cible} dans le .env de l'application, puis la redéployer :")
    print("   raccrochage, messages vocaux et rappels du site s'adresseront à la même région.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("numero", help="le numéro, au format +33…")
    parser.add_argument("--preparer", choices=list(REGIONS), metavar="REGION",
                        help="recopie dans cette région les webhooks de l'autre")
    parser.add_argument("--basculer", choices=list(REGIONS), metavar="REGION",
                        help="fait traiter les appels du numéro dans cette région")
    args = parser.parse_args()

    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    if not sid.startswith("AC") or not _jeton("us1"):
        sys.exit("Définir TWILIO_ACCOUNT_SID (AC…) et TWILIO_AUTH_TOKEN dans l'environnement.")
    numero = "".join(args.numero.split())
    if args.preparer:
        preparer(numero, sid, args.preparer)
    if args.basculer:
        basculer(numero, sid, args.basculer)
    etat(numero, sid)


if __name__ == "__main__":
    main()
