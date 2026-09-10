#!/usr/bin/env python3
"""Lit ce que le service a mesuré pendant le banc d'essai (#40).

Le banc (`banc_appels.py`) ne fabrique que la charge. Les chiffres viennent du journal de
bord (#88), c'est-à-dire du MÊME capteur qui mesure les appels d'un vrai client. C'est ce
qui rend la mesure comparable : « à 8 appels simultanés, le blanc ressenti passe de 1,9 à
X secondes » se lit sur la même échelle que les appels de production.

La question à laquelle ce rapport répond est unique : **à partir de combien d'appels
simultanés la conversation se dégrade-t-elle ?** Pas « est-ce que ça tient » — tout tient,
plus ou moins mal. Le seuil est celui où l'appelant s'en aperçoit.

Usage (depuis le poste de développement) :
    python3 scripts/banc_rapport.py
    python3 scripts/banc_rapport.py --depuis 2026-09-05T18:00
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import banc_couts  # noqa: E402  (le chemin doit être posé avant l'import)

SSH_CLE = os.getenv("VPS_SSH_KEY", os.path.expanduser("~/.ssh/moshi-vps-deploy"))
VPS = os.getenv("VPS_HOTE", "root@187.77.172.87")
CONTENEUR = os.getenv("VPS_CONTENEUR", "moshi-rag-voice-assistant-api-1")

# Exécuté DANS le conteneur : c'est le seul endroit d'où la base est lisible.
LECTURE = r'''
import sqlite3, json, sys
from collections import defaultdict
import statistics as st

depuis = sys.argv[1] if len(sys.argv) > 1 else "1970-01-01"
c = sqlite3.connect("/app/data/app.db"); c.row_factory = sqlite3.Row

# Les appels du banc se reconnaissent à leur call_sid : le préfixe est posé par
# banc_appels.py. Aucune colonne ajoutée à la base pour un besoin de mesure.
lignes = c.execute(
    """SELECT id, call_sid, started_at, duration_seconds, status, journal
       FROM calls WHERE call_sid LIKE 'CABANC%' AND started_at >= ?
       ORDER BY started_at""", (depuis,)).fetchall()

if not lignes:
    print("Aucun appel de banc trouve. Le banc a-t-il tourne ?")
    sys.exit(0)

# Regroupement par palier : les appels d'un meme palier demarrent ensemble, a la
# seconde pres. On coupe des qu'un ecart depasse 15 s — un palier dure au moins 45 s,
# donc aucun risque de coller deux paliers.
from datetime import datetime

def _quand(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

paliers, courant, precedent = [], [], None
for r in lignes:
    if precedent is not None and (_quand(r["started_at"]) - _quand(precedent)).total_seconds() > 15:
        paliers.append(courant)
        courant = []
    courant.append(r)
    precedent = r["started_at"]
if courant:
    paliers.append(courant)

print(f"{len(lignes)} appels de banc, regroupes en {len(paliers)} palier(s)\n")
print("simultanes  appels  muets  blanc ressenti (mediane / p90)  attente  llm    tts    tours")
print("-" * 92)

for grp in paliers:
    blancs, att, llm, tts, tours, muets = [], [], [], [], 0, 0
    for r in grp:
        j = json.loads(r["journal"]) if r["journal"] else None
        if not j or not j.get("tours"):
            muets += 1
            continue
        for t in j["tours"]:
            if t.get("blanc_ressenti_ms"): blancs.append(t["blanc_ressenti_ms"])
            if t.get("attente_tour_ms"):   att.append(t["attente_tour_ms"])
            if t.get("llm_ms"):            llm.append(t["llm_ms"])
            if t.get("tts_ms"):            tts.append(t["tts_ms"])
        tours += len(j["tours"])
    def m(v): return f"{st.median(v):5.0f}" if v else "    -"
    def p90(v):
        return f"{sorted(v)[min(len(v)-1, int(len(v)*0.9))]:5.0f}" if v else "    -"
    print(f"{len(grp):9}  {len(grp):6}  {muets:5}   {m(blancs)} ms / {p90(blancs)} ms      "
          f"{m(att)}   {m(llm)}  {m(tts)}   {tours:4}")

print("\nReference production (appels reels, un seul a la fois) :")
print("  blanc ressenti median 1904 ms, p90 2668 ms | attente 570 | llm 529 | tts 610")
print("\nLecture : le palier ou le blanc median decroche est la limite reelle.")
print("Si `llm` et `tts` restent plats et que seule `attente` monte, c'est le CPU du VPS.")
print("Si `tts` monte, c'est le GPU (batch 8 par conteneur) qui sature.")

# Ligne lisible par la partie financiere du rapport : total des secondes d'audio et
# nombre d'appels, pour que le cout par appel porte sur les MEMES appels que la latence.
print(f"\n#TOTAUX {len(lignes)} {sum(float(r['duration_seconds'] or 0) for r in lignes):.0f}")
'''


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--depuis", default="1970-01-01",
                   help="ne lire que les appels de banc depuis cet horodatage ISO")
    args = p.parse_args()

    commande = (
        f"docker exec -i {shlex.quote(CONTENEUR)} python - "
        f"{shlex.quote(args.depuis)}"
    )
    try:
        sortie = subprocess.run(
            ["ssh", "-i", SSH_CLE, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             VPS, commande],
            input=LECTURE, text=True, capture_output=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"Connexion au serveur impossible : {exc}", file=sys.stderr)
        return 1
    # moshi-server et pipecat écrivent leur bannière sur la sortie : on la retire pour
    # que le rapport reste lisible.
    appels, secondes = 0, 0.0
    for ligne in sortie.stdout.splitlines():
        if ligne.startswith("#TOTAUX"):
            _, a, sec = ligne.split()
            appels, secondes = int(a), float(sec)
            continue
        if "Pipecat" not in ligne:
            print(ligne)
    if sortie.returncode != 0:
        print(sortie.stderr.strip()[:600], file=sys.stderr)
        return sortie.returncode

    # Volet financier. Un banc technique qui ne chiffre pas ne sert qu'à moitié : savoir
    # qu'on tient dix appels ne dit pas si on peut se le permettre.
    releves = banc_couts.lire()
    if releves and appels:
        banc_couts.afficher(banc_couts.calculer(
            releves["avant"], releves["apres"], secondes, appels))
    elif appels:
        print("\n(pas de relevé de facturation : relancer banc_appels.py pour en produire un)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
