#!/usr/bin/env python3
"""Coût de bout en bout d'un appel — données EXACTES (une seule entrée à tarif fourni).

Sources par poste :
- Twilio    : prix RÉEL par appel (API Calls, champ `price`).
- LLM       : coût RÉEL (diff d'usage OpenRouter avant/après).
- Modal GPU : coût RÉEL GPU/CPU/Mémoire (`modal billing report`, chiffre de Modal).
- Deepgram  : durée d'appel × tarif/min. L'API ne donne pas le $ (clé sans scope
              billing) → fournir DEEPGRAM_RATE_PER_MIN depuis le dashboard Deepgram.

Protocole :
    set -a; source .env; set +a
    python scripts/cost_report.py --snapshot        # AVANT la série d'appels
    #   ... passer N appels de test ...
    #   attendre ~2-3 min (agrégation Twilio/Modal)
    python scripts/cost_report.py --report          # APRÈS → coût total + moyenne/appel
"""
import argparse
import base64
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

SNAP_FILE = os.path.expanduser(
    os.getenv("COST_SNAPSHOT_FILE", "~/moshi-rag-voice-assistant/.cost_snapshot.json")
)
MODAL_BIN = os.path.expanduser("~/moshi-rag-voice-assistant/.modal-venv/bin/modal")
# Tarif Deepgram nova-2 streaming (À CONFIRMER sur ton dashboard : le seul chiffre non
# récupérable en API faute de scope billing sur la clé).
DEEPGRAM_RATE_PER_MIN = float(os.getenv("DEEPGRAM_RATE_PER_MIN", "0.0058"))
APP_NAME = "moshi-server"


def _get(url, headers=None, auth=None):
    req = urllib.request.Request(url, headers=headers or {})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(
            f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def openrouter_usage() -> float:
    d = _get("https://openrouter.ai/api/v1/auth/key",
             headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
    return float(d["data"]["usage"])


def modal_cost_today() -> dict:
    out = subprocess.check_output(
        [MODAL_BIN, "billing", "report", "--for", "today", "--show-resources", "--json"],
        text=True)
    res = {}
    for row in json.loads(out):
        if row.get("description") == APP_NAME:
            res[row["resource"]] = res.get(row["resource"], 0.0) + float(row["cost"])
    return res


def twilio_inbound_calls_since(iso_date: str, after_ts: str) -> list:
    from email.utils import parsedate_to_datetime

    sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    number = os.getenv("TWILIO_NUMBER", "")
    after = datetime.fromisoformat(after_ts)
    url = (f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
           f"?StartTime%3E={iso_date}&PageSize=100")
    d = _get(url, auth=(sid, tok))
    calls = []
    for c in d.get("calls", []):
        if number and c.get("to") != number:
            continue
        # Filtrage fin par horodatage (le filtre API est au jour près) : on ne garde
        # que les appels postérieurs au snapshot.
        try:
            if parsedate_to_datetime(c["start_time"]) < after:
                continue
        except (KeyError, TypeError, ValueError):
            pass
        calls.append({
            "sid": c["sid"],
            "duration": int(c.get("duration") or 0),
            "price": abs(float(c["price"])) if c.get("price") else None,
        })
    return calls


def cmd_snapshot():
    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "openrouter_usage": openrouter_usage(),
        "modal_today": modal_cost_today(),
    }
    with open(SNAP_FILE, "w") as f:
        json.dump(snap, f, indent=2)
    print(f"Snapshot enregistré → {SNAP_FILE}")
    print(f"  OpenRouter usage : ${snap['openrouter_usage']:.6f}")
    print(f"  Modal aujourd'hui: {snap['modal_today']}")


def cmd_report():
    with open(SNAP_FILE) as f:
        snap = json.load(f)

    llm = openrouter_usage() - snap["openrouter_usage"]
    modal_now = modal_cost_today()
    modal_delta = {k: modal_now.get(k, 0.0) - snap["modal_today"].get(k, 0.0)
                   for k in set(modal_now) | set(snap["modal_today"])}
    modal_total = sum(modal_delta.values())

    calls = twilio_inbound_calls_since(snap["date"], snap["ts"])
    # On ne garde que les appels postérieurs au snapshot (approx : même jour, prix connu).
    n = len(calls)
    twilio_total = sum(c["price"] for c in calls if c["price"] is not None)
    dur_sec = sum(c["duration"] for c in calls)
    deepgram_total = dur_sec / 60.0 * DEEPGRAM_RATE_PER_MIN

    grand = twilio_total + llm + modal_total + deepgram_total

    print(f"=== Coût depuis le snapshot ({snap['ts']}) ===")
    print(f"Appels entrants détectés : {n}  |  durée cumulée : {dur_sec}s ({dur_sec/60:.1f} min)")
    print()
    print(f"{'Poste':<22}{'Total':>12}{'/appel':>12}   source")
    def line(name, total, src):
        per = f"${total/n:.4f}" if n else "-"
        print(f"{name:<22}{'$'+format(total,'.4f'):>12}{per:>12}   {src}")
    line("Twilio (voix)", twilio_total, "EXACT (API)")
    line("LLM OpenRouter", llm, "EXACT (diff usage)")
    line("Modal GPU L4",   modal_delta.get("L4", 0.0), "EXACT (modal billing)")
    line("Modal CPU",      modal_delta.get("CPU", 0.0), "EXACT (modal billing)")
    line("Modal Mémoire",  modal_delta.get("Memory", 0.0), "EXACT (modal billing)")
    line("Deepgram STT", deepgram_total, f"tarif {DEEPGRAM_RATE_PER_MIN}$/min (À CONFIRMER)")
    print("-" * 58)
    line("TOTAL", grand, "")
    if n:
        print(f"\n➡  Coût moyen par appel : ${grand/n:.4f}  (~{grand/n*100:.1f} centimes)")
    print("\nRappel : passe --snapshot AVANT la série, --report APRÈS (~2-3 min de délai).")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Coût de bout en bout par appel (données exactes).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", action="store_true", help="Enregistre l'état avant la série.")
    g.add_argument("--report", action="store_true", help="Calcule le coût depuis le snapshot.")
    args = p.parse_args()
    cmd_snapshot() if args.snapshot else cmd_report()
