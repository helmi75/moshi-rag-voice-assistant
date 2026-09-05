#!/usr/bin/env python3
"""Banc d'essai : combien d'appels simultanés la ligne tient-elle ? (#40)

On ne sait pas combien d'appels le service encaisse. Les seuls chiffres disponibles sont
théoriques — 8 flux par conteneur GPU, 4 conteneurs, donc 32 — et ils décrivent le TTS,
pas la chaîne complète. Or le maillon le plus chargé n'est pas le GPU : c'est le CPU du
VPS, où tournent le VAD Silero et smart-turn v3 (tous deux en ONNX, sur 2 vCPU) pour
CHAQUE appel, dans un seul processus Python.

Ce script répond par la mesure. Il rejoue l'audio d'un VRAI appel enregistré dans N
connexions simultanées, en imitant le protocole Twilio Media Streams, puis on lit les
journaux de bord que le service a produits.

**Ce qui rend cette mesure honnête, c'est qu'on ne mesure rien ici.** Le service instrumente
déjà chaque appel (#88) : blanc ressenti, décomposition par étage, coupures. Le banc ne
fabrique que la charge ; les chiffres viennent de la production elle-même, du même capteur
que pour un vrai appelant. Un banc qui mesurerait lui-même mesurerait autre chose.

⚠️ CE SCRIPT PASSE DE VRAIS APPELS. Il consomme du GPU Modal, du Deepgram et du modèle de
langage, et il écrit dans la base de production. Trois conséquences :
  - ça coûte de l'argent (ordre de grandeur : 11 centimes par appel) ;
  - il crée des lignes dans `calls` — d'où l'établissement de test dédié, pour ne pas
    polluer les statistiques ni le compteur de forfait d'un vrai restaurant ;
  - pendant qu'il tourne, un vrai appelant subirait la charge. À ne pas lancer en service.

Usage :
    python3 scripts/banc_appels.py --n 4 --duree 60
    python3 scripts/banc_appels.py --paliers 1,2,4,8 --duree 45
"""
import argparse
import asyncio
import base64
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("Il manque `websockets` : pip install websockets")

# Twilio envoie 20 ms de µ-law par message, soit 160 octets à 8 kHz.
TRAME_OCTETS = 160
TRAME_SECONDES = 0.02

DEFAUT_URL = os.getenv("BANC_URL", "wss://app.helmane.fr/ws/voice")
# Numéro de l'établissement de test. Il DOIT exister en base, sinon le service ferme la
# connexion en 1008 — c'est d'ailleurs le premier contrôle que ce banc fait passer.
DEFAUT_TENANT = os.getenv("BANC_TENANT_TEL", "+33900000001")


def _sid(prefixe: str) -> str:
    """Identifiant au format Twilio. Le préfixe `BANC` rend les appels du banc
    reconnaissables en base d'un coup d'œil, sans colonne supplémentaire."""
    return prefixe + "BANC" + "".join(random.choices("0123456789abcdef", k=28))


async def un_appel(index: int, url: str, audio: bytes, duree: float,
                   tenant_tel: str, resultats: list, depart: float = 0.0) -> None:
    """Joue un appel du début à la fin. N'échoue jamais bruyamment : un appel du banc
    qui casse est une donnée, pas une panne — on la note et on continue."""
    stream_sid = _sid("MZ")
    call_sid = _sid("CA")
    etat = {
        "n": index, "call_sid": call_sid, "erreur": None,
        "octets_recus": 0, "premier_son_s": None, "trames_envoyees": 0,
    }
    debut = time.monotonic()
    try:
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({
                "event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await ws.send(json.dumps({
                "event": "start",
                "sequenceNumber": "1",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                    "callSid": call_sid,
                    "accountSid": "ACbanc",
                    "tracks": ["inbound"],
                    # C'est ce que le service lit pour retrouver l'établissement.
                    "customParameters": {"To": tenant_tel, "From": "+33900000999"},
                    "mediaFormat": {"encoding": "audio/x-mulaw",
                                    "sampleRate": 8000, "channels": 1},
                },
            }))

            async def ecouter():
                """Consomme l'audio renvoyé par l'assistante. Sans ce lecteur, le tampon
                d'envoi du service se remplirait et on mesurerait notre propre blocage."""
                async for brut in ws:
                    try:
                        m = json.loads(brut)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if m.get("event") == "media":
                        taille = len(m["media"].get("payload") or "")
                        etat["octets_recus"] += taille
                        if etat["premier_son_s"] is None and taille > 0:
                            etat["premier_son_s"] = round(time.monotonic() - debut, 2)

            lecteur = asyncio.create_task(ecouter())

            # Émission EN TEMPS RÉEL. Envoyer l'audio plus vite que le temps réel
            # mesurerait la capacité d'un tampon, pas celle d'une ligne téléphonique.
            depart_horloge = time.monotonic()
            # On saute le début de l'enregistrement : l'appelant y est SILENCIEUX (il
            # écoute l'accueil). Rejouer ce silence ferait mesurer un appel où personne
            # ne parle — le pipeline tournerait à vide et le journal serait vide, ce qui
            # est exactement ce qui s'est passé au premier essai.
            debut_octets = int(depart * 8000) // TRAME_OCTETS * TRAME_OCTETS
            offset = min(debut_octets, max(0, len(audio) - TRAME_OCTETS))
            trame = 0
            while time.monotonic() - debut < duree:
                if offset + TRAME_OCTETS > len(audio):
                    offset = debut_octets  # on reboucle, toujours sur la partie parlée
                bloc = audio[offset:offset + TRAME_OCTETS]
                offset += TRAME_OCTETS
                trame += 1
                await ws.send(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"track": "inbound", "chunk": str(trame),
                              "timestamp": str(trame * 20),
                              "payload": base64.b64encode(bloc).decode()},
                }))
                etat["trames_envoyees"] = trame
                # On vise l'horloge absolue, pas un sleep fixe : un sleep accumulerait
                # la dérive et le flux prendrait du retard sur le temps réel.
                prochaine = depart_horloge + trame * TRAME_SECONDES
                retard = prochaine - time.monotonic()
                if retard > 0:
                    await asyncio.sleep(retard)

            await ws.send(json.dumps({"event": "stop", "streamSid": stream_sid}))
            lecteur.cancel()
    except Exception as exc:
        etat["erreur"] = f"{type(exc).__name__}: {exc}"
    etat["duree_s"] = round(time.monotonic() - debut, 1)
    resultats.append(etat)


async def palier(n: int, url: str, audio: bytes, duree: float, tenant_tel: str,
                 depart: float) -> list:
    print(f"\n── {n} appel(s) simultané(s), {duree:.0f} s ".ljust(66, "─"))
    resultats: list = []
    debut = time.monotonic()
    await asyncio.gather(*[
        un_appel(i + 1, url, audio, duree, tenant_tel, resultats, depart)
        for i in range(n)
    ])
    ecoule = time.monotonic() - debut

    ok = [r for r in resultats if not r["erreur"]]
    ko = [r for r in resultats if r["erreur"]]
    print(f"   terminé en {ecoule:.0f} s · {len(ok)} abouti(s), {len(ko)} en échec")
    if ko:
        for r in ko[:3]:
            print(f"     ✗ appel {r['n']} : {r['erreur']}")
    premiers = [r["premier_son_s"] for r in ok if r["premier_son_s"] is not None]
    if premiers:
        print(f"   premier son de l'assistante : médiane {statistics.median(premiers):.1f} s "
              f"(min {min(premiers):.1f} · max {max(premiers):.1f})")
    muets = [r for r in ok if r["premier_son_s"] is None]
    if muets:
        print(f"   ⚠️  {len(muets)} appel(s) n'ont RIEN reçu — l'assistante est restée muette")
    print("   identifiants : " + ", ".join(r["call_sid"][:14] for r in resultats[:4])
          + (" …" if len(resultats) > 4 else ""))
    return resultats


def charger_audio(chemin: Path) -> bytes:
    if not chemin.exists():
        sys.exit(
            f"Audio introuvable : {chemin}\n"
            "Récupère une piste appelant depuis le serveur, par exemple :\n"
            "  scp -i ~/.ssh/moshi-vps-deploy root@187.77.172.87:"
            "/var/lib/docker/volumes/*_api_data/_data/enregistrements/tenant1/"
            "appel47-appelant.ulaw ./banc.ulaw"
        )
    audio = chemin.read_bytes()
    print(f"Audio  : {chemin.name} — {len(audio) / 8000:.0f} s de parole réelle")
    return audio


async def principal() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, help="nombre d'appels simultanés (un seul palier)")
    p.add_argument("--paliers", type=str,
                   help="paliers croissants, ex. 1,2,4,8 — la courbe de dégradation")
    p.add_argument("--duree", type=float, default=60, help="durée d'un appel, en secondes")
    p.add_argument("--audio", type=Path, default=Path("banc.ulaw"),
                   help="piste appelant en µ-law brut (défaut : banc.ulaw)")
    p.add_argument("--url", default=DEFAUT_URL)
    p.add_argument("--tenant-tel", default=DEFAUT_TENANT,
                   help="numéro de l'établissement de test")
    p.add_argument("--depart", type=float, default=22,
                   help="secondes de silence à sauter au début de l'enregistrement "
                        "(l'appelant y écoute l'accueil ; défaut 22)")
    p.add_argument("--sans-prechauffage", action="store_true",
                   help="ne pas réveiller le GPU avant de mesurer — le premier palier "
                        "portera alors le démarrage à froid (~50 s)")
    p.add_argument("--je-sais-que-ca-coute", action="store_true",
                   help="confirme que ces appels sont réels et facturés")
    args = p.parse_args()

    paliers = ([int(x) for x in args.paliers.split(",")] if args.paliers
               else [args.n or 1])
    total = sum(paliers)

    print("=" * 66)
    print("BANC D'ESSAI — appels simultanés")
    print("=" * 66)
    audio = charger_audio(args.audio)
    print(f"Cible  : {args.url}  (établissement {args.tenant_tel})")
    print(f"Paliers: {paliers} — {total} appels de {args.duree:.0f} s au total")
    print(f"Départ : à {args.depart:.0f} s dans l'enregistrement (on saute le silence)")
    factures = total + (0 if args.sans_prechauffage else 1)
    print(f"Coût   : ordre de grandeur {factures * 0.11:.2f} € de GPU, STT et modèle")

    if not args.je_sais_que_ca_coute:
        print("\nCe sont de VRAIS appels : ils consomment du GPU et écrivent en base.")
        print("Relance avec --je-sais-que-ca-coute pour confirmer.")
        return 1

    if not args.sans_prechauffage:
        # Un GPU endormi met ~50 s à répondre : sans ce réveil, le premier palier
        # mesurerait le démarrage à froid et non la concurrence. L'appel de
        # préchauffage n'est pas compté dans les paliers, mais il est bien facturé.
        print("\n── préchauffage (réveil du GPU, ~60 s) ".ljust(66, "─"))
        chauffe: list = []
        await un_appel(0, args.url, audio, 60, args.tenant_tel, chauffe, args.depart)
        etat = chauffe[0] if chauffe else {}
        print(f"   GPU réveillé · premier son à {etat.get('premier_son_s')} s"
              f"{' · ' + etat['erreur'] if etat.get('erreur') else ''}")
        await asyncio.sleep(5)

    tous: list = []
    for i, n in enumerate(paliers):
        tous += await palier(n, args.url, audio, args.duree, args.tenant_tel, args.depart)
        if i < len(paliers) - 1:
            # Laisse le service redescendre : sans ce répit, le palier suivant
            # mesurerait la queue du précédent.
            print("   (pause de 20 s avant le palier suivant)")
            await asyncio.sleep(20)

    print("\n" + "=" * 66)
    print(f"{len(tous)} appels passés. Les chiffres qui comptent sont dans les journaux")
    print("de bord côté serveur — blanc ressenti, décomposition par étage, coupures :")
    print("\n  python3 scripts/banc_rapport.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
