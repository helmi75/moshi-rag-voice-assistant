"""Ce que le banc d'essai a RÉELLEMENT coûté (#40).

Un banc technique qui ne chiffre pas ne sert qu'à moitié : savoir qu'on tient sept appels
simultanés ne dit pas si on peut se le permettre. Ce module relève les compteurs de
facturation avant et après, et rend la différence.

**Chaque chiffre porte sa nature — rien n'est deviné pour faire joli :**

| Poste      | Source                                             | Nature   |
|------------|----------------------------------------------------|----------|
| Modal      | `Workspace.billing.report`, résolution horaire     | **réel** |
| OpenRouter | `GET /api/v1/auth/key` → `usage`                   | **réel** |
| Deepgram   | durée × tarif public nova-3                        | calculé  |
| Twilio     | zéro pour le banc ; tarif relevé pour l'extrapoler | **réel** |

**Le piège corrigé ici (10/09/2026).** La première version relevait la facture 45 s après
le dernier appel. Or le GPU reste allumé 120 s après (`scaledown_window`), et ces deux
minutes d'inactivité sont facturées : elles appartiennent au coût du banc, comme à celui
d'un vrai appel isolé. Le relevé attend désormais que Modal ne liste plus AUCUN conteneur,
puis que la facture cesse de bouger d'un relevé à l'autre.

Deepgram est calculé faute de mieux : la clé peut lister les projets mais pas lire
`usage:read`. Le tarif est celui de nova-3 streaming à la carte, monolingue — le même que
dans `api/app/calls.py`, pour que l'admin et ce rapport racontent la même histoire.

Twilio vaut zéro pour le banc, et c'est exact : le banc entre par le websocket, aucun appel
téléphonique n'est passé. Pour extrapoler vers un vrai appel, on applique le tarif RELEVÉ
par l'API sur les vrais appels du compte : 0,0085 $ par minute ENTAMÉE — vérifié sur huit
appels (140 s → 3 minutes facturées, 81 s → 2 minutes).
"""
import datetime
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# nova-3 streaming, à la carte, monolingue (deepgram.com/pricing) — identique à calls.py.
TARIF_DEEPGRAM_PAR_MIN = float(os.getenv("COST_DEEPGRAM_PER_MIN", "0.0077"))
# Relevé par l'API Twilio sur les huit derniers vrais appels : facturé à la minute entamée.
TARIF_TWILIO_PAR_MIN_ENTAMEE = float(os.getenv("COST_TWILIO_PER_MIN", "0.0085"))

APP_MODAL = os.getenv("BANC_APP_MODAL", "moshi-server")
FICHIER = Path(os.getenv("BANC_COUTS_FICHIER", "banc-couts.json"))


def _maintenant() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _heure(t: datetime.datetime) -> datetime.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


# ─── Relevés ─────────────────────────────────────────────────────────────────

def _openrouter() -> Optional[float]:
    """Dépense cumulée de la clé, en dollars. Lue SUR LE SERVEUR : c'est là que vit le
    `.env`, et une clé d'API n'a aucune raison de transiter par le poste local."""
    lecture = (
        'K=$(grep "^OPENROUTER_API_KEY=" /opt/moshi-rag-voice-assistant/.env | cut -d= -f2-); '
        '[ -n "$K" ] && curl -s -H "Authorization: Bearer $K" '
        'https://openrouter.ai/api/v1/auth/key'
    )
    try:
        sortie = subprocess.run(
            ["ssh", "-i", os.path.expanduser(os.getenv("VPS_SSH_KEY", "~/.ssh/moshi-vps-deploy")),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             os.getenv("VPS_HOTE", "root@187.77.172.87"), lecture],
            capture_output=True, text=True, timeout=60)
        return float(json.loads(sortie.stdout)["data"]["usage"])
    except Exception:
        return None


def _modal_par_heure(debut: datetime.datetime, fin: datetime.datetime) -> Optional[dict]:
    """Coût Modal par seau horaire sur [debut, fin), par ressource. None si illisible.

    Résolution horaire : la plus fine que Modal accepte. Un banc qui chevauche 15 h et
    16 h doit lire LES DEUX seaux — la première version n'en lisait qu'un et déclarait le
    poste « indisponible » dès qu'on passait l'heure."""
    try:
        import modal
        lignes = modal.Workspace.from_context().billing.report(
            start=_heure(debut), end=_heure(fin) + datetime.timedelta(hours=1),
            resolution="h")
    except Exception:
        return None
    seaux: dict = {}
    for ligne in lignes:
        cle = ligne.interval_start.isoformat()
        seau = seaux.setdefault(cle, {"total": 0.0, "par_ressource": {}})
        seau["total"] += float(ligne.cost)
        for nom, valeur in (ligne.cost_by_resource or {}).items():
            seau["par_ressource"][nom] = seau["par_ressource"].get(nom, 0.0) + float(valeur)
    return seaux


def _conteneurs_actifs() -> Optional[int]:
    """Combien de conteneurs GPU tournent encore. None si la question est sans réponse."""
    try:
        import modal
        app_id = modal.App.lookup(APP_MODAL).app_id
        # Par l'interpréteur courant, pas par le nom `modal` : lancé depuis un venv non
        # activé, la commande n'est pas dans le PATH, et le suivi d'extinction échouerait
        # en silence — on relèverait alors la facture GPU avant qu'elle soit complète.
        sortie = subprocess.run(
            [sys.executable, "-m", "modal", "container", "list", "--json", "--app-id", app_id],
            capture_output=True, text=True, timeout=90)
        return len(json.loads(sortie.stdout or "[]"))
    except Exception:
        return None


def instantane(depuis: Optional[datetime.datetime] = None) -> dict:
    """Relève les compteurs. `depuis` fixe le premier seau horaire lu (défaut : maintenant)."""
    quand = _maintenant()
    return {
        "quand": quand.isoformat(),
        "openrouter_usd": _openrouter(),
        "modal_heures": _modal_par_heure(depuis or quand, quand),
    }


def attendre_facture_stable(depuis: Optional[datetime.datetime] = None,
                            delai_max: float = 900) -> dict:
    """Relevé « après », pris seulement quand il ne peut plus bouger.

    Deux conditions, dans l'ordre : le GPU est éteint (sinon ses 120 s d'inactivité
    n'ont pas encore été facturées), puis deux relevés successifs identiques (sinon la
    facture n'a pas fini de remonter). Si le délai expire, le relevé est rendu quand
    même, marqué non stable — un chiffre incomplet signalé vaut mieux qu'un blanc."""
    debut = time.monotonic()
    gpu_eteint = False
    zeros = 0
    while time.monotonic() - debut < min(delai_max, 400):
        n = _conteneurs_actifs()
        if n == 0:
            zeros += 1
            if zeros >= 2:
                gpu_eteint = True
                break
        else:
            zeros = 0
        time.sleep(15)

    precedent = None
    while True:
        releve = instantane(depuis)
        empreinte = (releve["openrouter_usd"],
                     round(sum(s["total"] for s in (releve["modal_heures"] or {}).values()), 6))
        if precedent is not None and empreinte == precedent:
            releve.update(stable=True, gpu_eteint=gpu_eteint,
                          attente_s=round(time.monotonic() - debut))
            return releve
        if time.monotonic() - debut > delai_max:
            releve.update(stable=False, gpu_eteint=gpu_eteint,
                          attente_s=round(time.monotonic() - debut))
            return releve
        precedent = empreinte
        time.sleep(45)


def enregistrer(avant: dict, apres: dict) -> Path:
    FICHIER.write_text(json.dumps({"avant": avant, "apres": apres},
                                  ensure_ascii=False, indent=2))
    return FICHIER


def lire() -> Optional[dict]:
    try:
        return json.loads(FICHIER.read_text())
    except (OSError, ValueError):
        return None


# ─── Calcul ──────────────────────────────────────────────────────────────────

def _somme_modal(releve: dict) -> tuple[Optional[float], dict]:
    seaux = releve.get("modal_heures")
    if seaux is None:
        # Format de la première version (un seul seau) : relu pour les anciens fichiers.
        ancien = releve.get("modal")
        if not ancien:
            return None, {}
        return ancien.get("total_usd"), dict(ancien.get("par_ressource") or {})
    total, detail = 0.0, {}
    for seau in seaux.values():
        total += seau["total"]
        for nom, valeur in seau["par_ressource"].items():
            detail[nom] = detail.get(nom, 0.0) + valeur
    return total, detail


def calculer(avant: dict, apres: dict, secondes_audio: float, nb_appels: int,
             durees_appels: Optional[list[float]] = None) -> dict:
    """La facture du banc, poste par poste, avec la nature de chaque chiffre."""
    llm = None
    if avant.get("openrouter_usd") is not None and apres.get("openrouter_usd") is not None:
        llm = apres["openrouter_usd"] - avant["openrouter_usd"]

    m_avant, d_avant = _somme_modal(avant)
    m_apres, d_apres = _somme_modal(apres)
    gpu = None if m_avant is None or m_apres is None else m_apres - m_avant
    detail_gpu = {k: v - d_avant.get(k, 0.0) for k, v in d_apres.items()}

    stt = secondes_audio / 60.0 * TARIF_DEEPGRAM_PAR_MIN
    stable = apres.get("stable")
    postes = [
        {"poste": "Modal (GPU L4, CPU, mémoire)", "usd": gpu,
         "nature": ("réel" if stable is not False else "réel, INCOMPLET")
                   if gpu is not None else "indisponible",
         "source": "Workspace.billing.report, résolution horaire"
                   + ("" if apres.get("gpu_eteint") is not False
                      else " — GPU pas encore éteint au relevé"),
         "detail": detail_gpu},
        {"poste": "Modèle de langage (OpenRouter)", "usd": llm,
         "nature": "réel" if llm is not None else "indisponible",
         "source": "GET /api/v1/auth/key → usage (avant / après)"},
        {"poste": "Transcription (Deepgram)", "usd": stt, "nature": "calculé",
         "source": f"{secondes_audio / 60:.1f} min × {TARIF_DEEPGRAM_PAR_MIN} $/min"
                   " — la clé n'a pas le scope usage:read"},
        {"poste": "Téléphonie (Twilio)", "usd": 0.0, "nature": "réel",
         "source": "le banc entre par le websocket : aucun appel téléphonique"},
    ]
    total = sum(p["usd"] for p in postes if p["usd"] is not None)

    # Extrapolation vers la production : la seule ligne qui manque au banc est la
    # téléphonie, et son tarif est connu exactement (minute entamée).
    twilio_prod = None
    if durees_appels:
        twilio_prod = sum(math.ceil(max(d, 1) / 60) for d in durees_appels) \
            * TARIF_TWILIO_PAR_MIN_ENTAMEE
    return {
        "postes": postes,
        "total_usd": total,
        "par_appel_usd": total / nb_appels if nb_appels else None,
        "par_minute_usd": total / (secondes_audio / 60) if secondes_audio else None,
        "appels": nb_appels,
        "minutes_audio": secondes_audio / 60.0,
        "twilio_extrapole_usd": twilio_prod,
        "par_appel_production_usd": ((total + twilio_prod) / nb_appels
                                     if twilio_prod is not None and nb_appels else None),
        "complet": all(p["usd"] is not None for p in postes) and stable is not False,
        "releve": {"stable": stable, "gpu_eteint": apres.get("gpu_eteint"),
                   "attente_s": apres.get("attente_s")},
    }


def afficher(facture: dict) -> None:
    print("\n" + "=" * 78)
    print("VOLET FINANCIER — ce que le banc a coûté, relevé et non estimé")
    print("=" * 78)
    r = facture.get("releve") or {}
    if r.get("stable") is not None:
        print(f"Relevé pris après {r.get('attente_s')} s d'attente · GPU éteint : "
              f"{'oui' if r.get('gpu_eteint') else 'NON'} · facture stable : "
              f"{'oui' if r.get('stable') else 'NON'}\n")
    print(f"{'poste':38} {'USD':>10}  nature")
    print("-" * 78)
    for p in facture["postes"]:
        montant = "     n/d" if p["usd"] is None else f"{p['usd']:8.4f}"
        print(f"{p['poste']:38} {montant:>10}  {p['nature']}")
        print(f"{'':38} {'':>10}  ↳ {p['source']}")
        for nom, valeur in (p.get("detail") or {}).items():
            print(f"{'':38} {'':>10}    {nom} : {valeur:.4f}")
    print("-" * 78)
    print(f"{'TOTAL DU BANC':38} {facture['total_usd']:8.4f}")
    if facture["par_appel_usd"] is not None:
        print(f"{'par appel':38} {facture['par_appel_usd']:8.4f}   "
              f"({facture['appels']} appels, {facture['minutes_audio']:.1f} min)")
    if facture.get("par_minute_usd") is not None:
        print(f"{'par minute d appel':38} {facture['par_minute_usd']:8.4f}")
    if facture.get("twilio_extrapole_usd") is not None:
        print(f"\nExtrapolation production — ajouter la téléphonie au tarif RELEVÉ "
              f"({TARIF_TWILIO_PAR_MIN_ENTAMEE} $ la minute entamée) :")
        print(f"{'  + Twilio pour ces mêmes appels':38} {facture['twilio_extrapole_usd']:8.4f}")
        print(f"{'  = coût par appel en production':38} "
              f"{facture['par_appel_production_usd']:8.4f}")
    if not facture["complet"]:
        print("\n⚠️  Un poste au moins est incomplet ou illisible : le total est un MINIMUM.")
