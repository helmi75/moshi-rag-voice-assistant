#!/usr/bin/env python3
"""Achète et configure un numéro Twilio pour l'assistant vocal.

Sans dépendance (stdlib uniquement). Usage :

    export TWILIO_ACCOUNT_SID=ACxxxxxxxx
    export TWILIO_AUTH_TOKEN=xxxxxxxx
    python3 scripts/twilio_setup_number.py                  # état du compte + numéros
    python3 scripts/twilio_setup_number.py --buy-us         # achète un numéro US (+1)
    python3 scripts/twilio_setup_number.py --webhook https://mondomaine.fr/twilio/webhook

Les options se combinent : --buy-us --webhook ... achète puis configure.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

API = "https://api.twilio.com/2010-04-01"


def _request(method: str, url: str, sid: str, token: str, data: Optional[dict] = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"Erreur Twilio {exc.code} sur {method} {url}\n{detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buy-us", action="store_true",
                        help="achète un numéro US (+1) vocal si le compte n'en a pas")
    parser.add_argument("--webhook", metavar="URL",
                        help="configure l'URL de webhook vocal/SMS sur le(s) numéro(s)")
    parser.add_argument("--area-code", default=None,
                        help="indicatif régional US souhaité (ex. 415)")
    args = parser.parse_args()

    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid.startswith("AC") or not token:
        sys.exit("Définissez TWILIO_ACCOUNT_SID (AC...) et TWILIO_AUTH_TOKEN dans l'environnement.")

    account = _request("GET", f"{API}/Accounts/{sid}.json", sid, token)
    balance = _request("GET", f"{API}/Accounts/{sid}/Balance.json", sid, token)
    print(f"Compte  : {account['friendly_name']} — statut {account['status']}, type {account['type']}")
    print(f"Solde   : {balance['balance']} {balance['currency']}")

    numbers = _request(
        "GET", f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json?PageSize=20", sid, token
    )["incoming_phone_numbers"]
    print(f"Numéros : {len(numbers)}")
    for n in numbers:
        print(f"  {n['phone_number']}  (voice: {n['voice_url'] or 'non configuré'})")

    if args.buy_us and not numbers:
        query = {"VoiceEnabled": "true", "PageSize": "5"}
        if args.area_code:
            query["AreaCode"] = args.area_code
        available = _request(
            "GET",
            f"{API}/Accounts/{sid}/AvailablePhoneNumbers/US/Local.json?"
            + urllib.parse.urlencode(query),
            sid, token,
        )["available_phone_numbers"]
        if not available:
            sys.exit("Aucun numéro US disponible avec ces critères.")
        candidate = available[0]["phone_number"]
        print(f"\nAchat du numéro {candidate}...")
        bought = _request(
            "POST", f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json", sid, token,
            {"PhoneNumber": candidate},
        )
        numbers = [bought]
        print(f"✅ Numéro acheté : {bought['phone_number']}")
    elif args.buy_us:
        print("\nLe compte a déjà un numéro — pas d'achat.")

    if args.webhook:
        for n in numbers:
            _request(
                "POST", f"{API}/Accounts/{sid}/IncomingPhoneNumbers/{n['sid']}.json",
                sid, token,
                {"VoiceUrl": args.webhook, "VoiceMethod": "POST",
                 "SmsUrl": args.webhook, "SmsMethod": "POST"},
            )
            print(f"✅ Webhook configuré sur {n['phone_number']} → {args.webhook}")

    if numbers:
        print("\nÀ mettre dans votre .env :")
        print(f"  TWILIO_NUMBER={numbers[0]['phone_number']}")
    if account["type"] == "Trial":
        print("\n⚠️  Compte d'essai : les appels ne fonctionnent qu'avec des numéros")
        print("   vérifiés (Console → Phone Numbers → Verified Caller IDs) et Twilio")
        print("   joue un message d'annonce avant chaque appel.")


if __name__ == "__main__":
    main()
