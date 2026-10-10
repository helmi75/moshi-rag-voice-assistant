#!/usr/bin/env python3
"""La recette des appels, jouée par la machine : une commande, un tableau, un code de sortie.

    python3 scripts/recette_appels.py                 # étage 1 puis étage 2
    python3 scripts/recette_appels.py --etage 1       # les bancs seulement (aucun appel Twilio)
    python3 scripts/recette_appels.py --scenarios t17a,c8

Tout se joue SUR LE SERVEUR, par ssh : aucune clé de production n'est copiée ici.
- étage 1 : dans un conteneur jetable (image déployée, base jetable, aucun e-mail), avec les
  clés lues dans l'environnement du conteneur en service, sans les afficher ;
- étage 2 : dans le conteneur en service (`docker compose exec`), qui compose le numéro de
  l'établissement d'essai, lit le verdict en base, purge les appels d'essai et tient le
  carnet des dépenses.

Le plafond (par recette, par mois), l'heure, un appel réel en cours, un passage déjà joué
pour ce déploiement : c'est l'application qui refuse (`api/app/recette/`), pas ce script.
Sort en erreur si un scénario échoue. Rend le coût réel du passage.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

HOTE = os.getenv("DEPLOY_HOST", "root@187.77.172.87")
CLE = os.path.expanduser(os.getenv("DEPLOY_KEY", "~/.ssh/moshi-vps-deploy"))
CHEMIN = os.getenv("DEPLOY_PATH", "/opt/moshi-rag-voice-assistant")
# Ce que l'étage 1 a le droit de recevoir de l'environnement de production : de quoi parler
# aux fournisseurs, rien de Twilio, rien de la messagerie, rien de resOS, rien de l'admin.
PERMISES = ("OPENAI_API_KEY", "OPENROUTER_", "MISTRAL_", "DEEPGRAM_", "LLM_", "GPT_LIVE_", "COST_",
            "VOXTRAL_", "USER_TURN_", "INTERRUPTION", "RECETTE_VOIX_", "RECETTE_PLAFOND_")


def sur_le_serveur(commande: str, attente: int) -> str:
    fait = subprocess.run(["ssh", "-i", CLE, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOTE,
                           f"cd {shlex.quote(CHEMIN)} && {commande}"],
                          capture_output=True, text=True, timeout=attente)
    return fait.stdout + fait.stderr


def resultat(sortie: str) -> dict:
    """La dernière ligne `RESULTAT {json}` d'une commande du paquet."""
    for ligne in reversed(sortie.splitlines()):
        if ligne.startswith("RESULTAT "):
            return json.loads(ligne[len("RESULTAT "):])
    raise SystemExit("la commande n'a rien rendu :\n" + "\n".join(sortie.splitlines()[-15:]))


def main() -> int:
    analyse = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    analyse.add_argument("--etage", choices=("1", "2", "tous"), default="tous")
    analyse.add_argument("--scenarios", default="", help="clés séparées par des virgules ; vide : tous")
    choix = analyse.parse_args()
    filtre = f" --scenarios {shlex.quote(choix.scenarios)}" if choix.scenarios else ""

    etat = resultat(sur_le_serveur("docker compose exec -T api python -m app.recette etat", 60))
    print(f"Dépensé ce mois : {etat['du_mois']:.2f} $ sur {etat['plafond_mois']:.2f} $ ; "
          f"plafond par recette : {etat['plafond_recette']:.2f} $.")
    if etat.get("interdit"):
        print(f"Recette refusée : {etat['interdit']}.")
        return 2

    avant: dict = {}
    if choix.etage in ("1", "tous"):
        motif = "|".join(PERMISES)
        commande = (
            "IMG=$(docker compose images -q api | head -1) && "
            f"docker run --rm --env-file <(docker compose exec -T api env | grep -E '^({motif})') "
            f"-e RECETTE_DEJA_CE_MOIS={etat['du_mois']} -e RECETTE_REPLIQUES_DIR=/repliques "
            f"-v recette_repliques:/repliques \"$IMG\" python -m app.recette etage1{filtre}")
        avant = resultat(sur_le_serveur("bash -c " + shlex.quote(commande), 3600))

    sans_appels = " --sans-appels" if choix.etage == "1" or not etat.get("active") else ""
    if choix.etage != "1" and not etat.get("active"):
        print("Étage 2 non joué : la recette automatique n'est pas activée sur le serveur "
              "(RECETTE_JETON et RECETTE_ETABLISSEMENT dans le .env).")
    sortie = sur_le_serveur("docker compose exec -T api python -m app.recette etage2 --etage1 "
                            + shlex.quote(json.dumps(avant, ensure_ascii=False)) + sans_appels + filtre, 3600)
    final = resultat(sortie)
    print("\n".join(ligne for ligne in sortie.splitlines() if ligne.startswith("|")))
    if final.get("refus_etage2") and not sans_appels:
        print(f"\nÉtage 2 non joué : {final['refus_etage2']}.")
    print(f"\nRequêtes au cerveau (T19) : {json.dumps(final.get('cerveau'), ensure_ascii=False)}")
    if final.get("purge"):
        print(f"Appels d'essai purgés : {json.dumps(final['purge'], ensure_ascii=False)}")
    print(f"Coût réel de ce passage : {final['cout']:.4f} $ ; ce mois : {final['du_mois']:.2f} $.")
    echecs = [l for l in final["lignes"] if l.get("etat") == "echec"]
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
