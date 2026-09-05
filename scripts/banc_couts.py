"""Ce que le banc d'essai a RÉELLEMENT coûté (#40).

Un banc technique qui ne chiffre pas ne sert qu'à moitié : savoir qu'on tient dix appels
simultanés ne dit pas si on peut se le permettre. Ce module relève les compteurs de
facturation avant et après, et rend la différence.

**Trois postes, trois niveaux de certitude — et ils sont étiquetés comme tels.** Rien
n'est deviné pour faire joli : quand un chiffre est calculé et non facturé, il le dit.

| Poste     | Source                                    | Nature   |
|-----------|-------------------------------------------|----------|
| Modal     | `Workspace.billing.report`, résolution 'h' | **réel** |
| OpenRouter| `GET /api/v1/auth/key` → `usage`           | **réel** |
| Deepgram  | durée × tarif public                       | calculé  |
| Twilio    | zéro                                       | **réel** |

Deepgram est calculé faute de mieux : la clé du projet peut lister les projets mais pas
lire `usage:read`. Le tarif employé est celui de nova-3 streaming à la carte, monolingue,
le même que celui déjà inscrit dans `calls.py` — s'ils divergeaient, l'admin et ce
rapport raconteraient deux histoires. Pour obtenir du réel, ajouter le scope `usage:read`
à la clé côté tableau de bord Deepgram.

Twilio est à zéro et c'est un fait, pas une approximation : le banc se connecte
directement au websocket, aucun appel téléphonique n'est passé. Pour un vrai appel, le
prix exact se lit sur `GET /Calls/{sid}.json`. **Il faut donc ajouter la ligne Twilio à
la main pour extrapoler vers un vrai appel** — c'est signalé dans le rapport.
"""
import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

# Tarif Deepgram nova-3 streaming, à la carte, monolingue (deepgram.com/pricing).
# Volontairement identique à `_COST_DEEPGRAM_PER_MIN` de api/app/calls.py.
TARIF_DEEPGRAM_PAR_MIN = float(os.getenv("COST_DEEPGRAM_PER_MIN", "0.0077"))

FICHIER = Path(os.getenv("BANC_COUTS_FICHIER", "banc-couts.json"))


def _maintenant() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _openrouter() -> Optional[float]:
    """Dépense cumulée de la clé, en dollars. None si la clé est absente ou muette.

    La lecture passe par le serveur : c'est là que vit le `.env`, et une clé d'API n'a
    aucune raison de transiter par le poste de développement pour être lue."""
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
            capture_output=True, text=True, timeout=60,
        )
        return float(json.loads(sortie.stdout)["data"]["usage"])
    except Exception:
        return None


def _modal(depuis: datetime.datetime) -> Optional[dict]:
    """Coût Modal de l'heure en cours, par ressource. None si le client n'est pas là.

    Résolution horaire : c'est la plus fine que Modal accepte. Le banc dure quelques
    minutes, donc on prend la différence entre deux relevés du MÊME seau horaire — ce qui
    isole exactement la consommation du banc, sans dépendre de la granularité."""
    try:
        import modal
    except ImportError:
        return None
    try:
        debut_heure = depuis.replace(minute=0, second=0, microsecond=0)
        lignes = modal.Workspace.from_context().billing.report(
            start=debut_heure,
            end=debut_heure + datetime.timedelta(hours=1),
            resolution="h",
        )
        total, par_ressource = 0.0, {}
        for ligne in lignes:
            total += float(ligne.cost)
            for nom, valeur in (ligne.cost_by_resource or {}).items():
                par_ressource[nom] = par_ressource.get(nom, 0.0) + float(valeur)
        return {"total_usd": total, "par_ressource": par_ressource,
                "heure": debut_heure.isoformat()}
    except Exception:
        return None


def instantane() -> dict:
    """Relève les compteurs. À appeler avant, puis après le banc."""
    quand = _maintenant()
    return {
        "quand": quand.isoformat(),
        "openrouter_usd": _openrouter(),
        "modal": _modal(quand),
    }


def enregistrer(avant: dict, apres: dict) -> Path:
    FICHIER.write_text(json.dumps({"avant": avant, "apres": apres},
                                  ensure_ascii=False, indent=2))
    return FICHIER


def lire() -> Optional[dict]:
    try:
        return json.loads(FICHIER.read_text())
    except (OSError, ValueError):
        return None


def _ecart(avant: Optional[float], apres: Optional[float]) -> Optional[float]:
    if avant is None or apres is None:
        return None
    # Un écart négatif n'a pas de sens sur un compteur cumulé : il signale un relevé
    # pris à cheval sur deux périodes. On le rend tel quel plutôt que de le masquer.
    return apres - avant


def calculer(avant: dict, apres: dict, secondes_audio: float,
             nb_appels: int) -> dict:
    """La facture du banc, poste par poste, avec la nature de chaque chiffre."""
    llm = _ecart(avant.get("openrouter_usd"), apres.get("openrouter_usd"))

    gpu = None
    detail_gpu = {}
    a, b = avant.get("modal"), apres.get("modal")
    if a and b and a.get("heure") == b.get("heure"):
        gpu = _ecart(a.get("total_usd"), b.get("total_usd"))
        for nom, apres_v in (b.get("par_ressource") or {}).items():
            detail_gpu[nom] = apres_v - (a.get("par_ressource") or {}).get(nom, 0.0)

    stt = secondes_audio / 60.0 * TARIF_DEEPGRAM_PAR_MIN

    postes = [
        {"poste": "Modal (GPU L4, CPU, mémoire)", "usd": gpu,
         "nature": "réel" if gpu is not None else "indisponible",
         "source": "Workspace.billing.report, résolution horaire", "detail": detail_gpu},
        {"poste": "Modèle de langage (OpenRouter)", "usd": llm,
         "nature": "réel" if llm is not None else "indisponible",
         "source": "GET /api/v1/auth/key → usage"},
        {"poste": "Transcription (Deepgram)", "usd": stt, "nature": "calculé",
         "source": f"{secondes_audio / 60:.1f} min × {TARIF_DEEPGRAM_PAR_MIN} $/min "
                   "— la clé n'a pas le scope usage:read"},
        {"poste": "Téléphonie (Twilio)", "usd": 0.0, "nature": "réel",
         "source": "le banc se connecte au websocket : aucun appel téléphonique"},
    ]
    connus = [p["usd"] for p in postes if p["usd"] is not None]
    total = sum(connus)
    return {
        "postes": postes,
        "total_usd": total,
        "par_appel_usd": total / nb_appels if nb_appels else None,
        "appels": nb_appels,
        "minutes_audio": secondes_audio / 60.0,
        "complet": all(p["usd"] is not None for p in postes),
    }


def afficher(facture: dict) -> None:
    print("\n" + "=" * 74)
    print("CE QUE LE BANC A COÛTÉ")
    print("=" * 74)
    print(f"{'poste':38} {'USD':>10}  nature")
    print("-" * 74)
    for p in facture["postes"]:
        montant = "     n/d" if p["usd"] is None else f"{p['usd']:8.4f}"
        print(f"{p['poste']:38} {montant:>10}  {p['nature']}")
        print(f"{'':38} {'':>10}  ↳ {p['source']}")
        for nom, valeur in (p.get("detail") or {}).items():
            print(f"{'':38} {'':>10}    {nom} : {valeur:.4f}")
    print("-" * 74)
    print(f"{'TOTAL':38} {facture['total_usd']:8.4f}")
    if facture["par_appel_usd"] is not None:
        print(f"{'par appel':38} {facture['par_appel_usd']:8.4f}   "
              f"sur {facture['appels']} appels, {facture['minutes_audio']:.1f} min d'audio")
    if not facture["complet"]:
        print("\n⚠️  Un poste au moins n'a pas pu être relevé : le total est INCOMPLET.")
    print("\n⚠️  Twilio est à zéro parce que le banc ne passe pas par le téléphone.")
    print("    Un vrai appel entrant ajoute environ 0,0085 $/min — à ajouter pour")
    print("    extrapoler vers le coût d'un appel de production.")
