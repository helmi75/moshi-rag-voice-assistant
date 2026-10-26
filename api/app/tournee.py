"""La tournée quotidienne d'exploitation (ASSISTANTE-172) : chaque matin, en lecture seule,
ce qui demande la main de l'exploitant. Elle n'écrit aux super-admins que si un point est à
l'orange ou au rouge : un e-mail quotidien « tout va bien » finit par ne plus être lu.

Ce qu'elle regarde :
- tous les contrôles de la supervision, après une relève fraîche du crédit OpenRouter ;
- les exécutions de GitHub Actions en échec sur `main` depuis vingt-quatre heures (la CI
  de la production et la sonde planifiée). Le dépôt est public : l'API se lit sans jeton.

Ce qu'elle ne voit pas, faute d'API (vérifié le 24/10/2026) : le solde Mistral (console
seulement) et le solde Deepgram (la clé de production n'a pas le droit de le lire).

8 h, heure de Paris : décision de Helmi du 24/10/2026, avant le service du midi.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from . import db, horloge, notifications, supervision, users

DEPOT = "helmi75/moshi-rag-voice-assistant"

# Une tournée ratée (base verrouillée, réseau coupé) se retente dans la journée.
_NOUVEL_ESSAI_SECONDES = 3600


def heure() -> Optional[tuple[int, int]]:
    """(heure, minute) au fuseau du restaurant, ou None si la tournée est coupée (« non »).
    Une valeur illisible garde 8 h : une tournée qui s'éteint sur une faute de frappe ne
    préviendrait plus de rien, en silence."""
    brut = os.getenv("SUPERVISION_TOURNEE_HEURE", "08:00").strip().lower()
    if brut in ("non", "off", "0"):
        return None
    try:
        h, m = (int(x) for x in brut.split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return 8, 0


async def echecs_github(client, depuis: datetime) -> dict:
    """Les exécutions en échec sur `main` depuis `depuis`, regroupées par workflow :
    {"echecs": [{"workflow", "nombre", "dernier", "url"}]} ou {"erreur": …}. Pendant une
    panne de production, la sonde échoue toutes les quinze minutes : une ligne par
    workflow, pas une par exécution."""
    try:
        reponse = await client.get(
            f"https://api.github.com/repos/{DEPOT}/actions/runs",
            params={"branch": "main", "status": "failure", "per_page": 100,
                    "created": ">=" + depuis.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            headers={"Accept": "application/vnd.github+json"})
        if reponse.status_code >= 400:
            return {"erreur": f"HTTP {reponse.status_code}"}
        corps = reponse.json() or {}
        runs = corps.get("workflow_runs", [])
        groupes: dict[str, dict] = {}
        for run in runs:
            g = groupes.setdefault(run.get("name") or "?", {
                "workflow": run.get("name") or "?", "nombre": 0, "dernier": "", "url": ""})
            g["nombre"] += 1
            if (run.get("created_at") or "") > g["dernier"]:
                g["dernier"], g["url"] = run.get("created_at") or "", run.get("html_url") or ""
        # Une page de cent : une sonde en échec toute la journée en fait 96. Au-delà, le
        # total de GitHub fait foi, et le détail le dit partiel.
        total = max(int(corps.get("total_count") or 0), len(runs))
        return {"echecs": sorted(groupes.values(), key=lambda g: g["workflow"]),
                "total": total, "partiel": total > len(runs)}
    except Exception as exc:
        return {"erreur": type(exc).__name__}


def points(etat: dict, github: dict) -> list[dict]:
    """Ce qui est à l'orange ou au rouge, du plus grave au plus fin."""
    liste = [{"titre": c["titre"], "niveau": c["niveau"], "resume": c["resume"],
              "detail": c.get("detail", "")}
             for c in etat["controles"] if c["niveau"] != supervision.OK]
    if "erreur" in github:
        liste.append({"titre": "GitHub Actions", "niveau": supervision.ATTENTION,
                      "resume": f"Échecs illisibles ({github['erreur']}).",
                      "detail": "On ne sait pas si la CI ou la sonde ont échoué."})
    elif github.get("echecs"):
        lignes = []
        for g in github["echecs"]:
            dernier = horloge.au_restaurant(g["dernier"])
            quand = dernier.strftime("%d/%m à %H h %M") if dernier else "?"
            lignes.append(f"{g['workflow']} : {g['nombre']} échec(s), le dernier le {quand} — {g['url']}")
        if github.get("partiel"):
            lignes.append(f"(détail des {sum(g['nombre'] for g in github['echecs'])} plus récentes seulement)")
        total = github.get("total") or sum(g["nombre"] for g in github["echecs"])
        liste.append({"titre": "GitHub Actions", "niveau": supervision.ATTENTION,
                      "resume": f"{total} exécution(s) en échec sur main en 24 h.",
                      "detail": "\n".join(lignes)})
    return sorted(liste, key=lambda p: -supervision._RANG.get(p["niveau"], 0))


def message(liste: list[dict], quand: datetime) -> tuple[str, str]:
    """`quand` : l'heure réelle de la tournée. Le 24/10, faite à 20 h après un
    déploiement, elle disait « ce matin »."""
    rouges = sum(1 for p in liste if p["niveau"] == supervision.PANNE)
    sujet = (f"Helmane — tournée du {quand.strftime('%d/%m')} : {len(liste)} point(s) à voir"
             + (f", dont {rouges} en panne" if rouges else ""))
    corps = [f"La tournée du {quand.strftime('%d/%m à %H h %M')} a relevé {len(liste)} point(s) :", ""]
    for p in liste:
        corps.append(f"[{p['niveau'].upper()}] {p['titre']} — {p['resume']}")
        if p["detail"]:
            corps += [f"    {ligne}" for ligne in p["detail"].splitlines()]
        corps.append("")
    corps += ["L'état complet : page « Santé & coûts » de l'admin.",
              "Pas d'e-mail les jours où tout est vert."]
    return sujet, "\n".join(corps)


def _superadmins() -> list[str]:
    adresses: list[str] = []
    for compte in users.list_users():
        if compte.is_superadmin and compte.email and compte.email not in adresses:
            adresses.append(compte.email)
    return adresses


async def faire(client) -> dict:
    """Une tournée. N'écrit rien ailleurs que sur l'ardoise (`tournee`) et dans l'e-mail."""
    await supervision.relever_credit_openrouter(client)
    etat = await db.hors_boucle(supervision.etat, force=True)
    github = await echecs_github(client, horloge.maintenant() - timedelta(hours=24))
    liste = points(etat, github)
    envoye = False
    if liste:
        sujet, corps = message(liste, horloge.maintenant())
        envoye = await notifications.envoyer(await db.hors_boucle(_superadmins), sujet, corps)
    # Un e-mail qui n'est pas parti (messagerie en panne à 8 h) ne compte pas la tournée
    # comme faite : elle se retente dans l'heure, sinon l'alerte du jour serait perdue. Sans
    # messagerie configurée, rien ne partira jamais : la tournée est faite.
    a_refaire = bool(liste) and not envoye and notifications.actif()
    bilan = {"jour": None if a_refaire else horloge.aujourd_hui().isoformat(),
             "points": len(liste), "envoye": envoye}
    # Si cette écriture échoue après l'envoi, l'essai suivant renverra l'e-mail : un doublon
    # plutôt qu'un silence.
    await db.hors_boucle(supervision.noter, "tournee", bilan)
    logger.info(f"tournée : {len(liste)} point(s), e-mail {'envoyé' if envoye else 'non envoyé'}"
                + (" ; nouvel essai dans une heure." if a_refaire else "."))
    return bilan


def _faite_aujourd_hui() -> bool:
    memo = supervision.relire("tournee")
    return bool(memo) and memo[0].get("jour") == horloge.aujourd_hui().isoformat()


def attente_secondes(maintenant: datetime, h: int, m: int) -> float:
    """Jusqu'à la prochaine heure de tournée, au fuseau du restaurant (le passage à l'heure
    d'hiver ne la décale pas)."""
    cible = maintenant.replace(hour=h, minute=m, second=0, microsecond=0)
    if cible <= maintenant:
        cible = (cible + timedelta(days=1)).replace(hour=h, minute=m)
    return max(1.0, (cible.astimezone(timezone.utc) - maintenant.astimezone(timezone.utc)
                     ).total_seconds())


async def boucle() -> None:
    """Lancée au démarrage. Un redémarrage après l'heure, un jour où la tournée n'a pas
    eu lieu (un déploiement à 8 h), la fait aussitôt : sinon ce jour-là n'en aurait pas."""
    import httpx

    reglage = heure()
    if reglage is None:
        return
    h, m = reglage
    logger.info(f"tournée quotidienne à {h:02d} h {m:02d}.")
    while True:
        attente = None
        try:
            maintenant = horloge.maintenant()
            passee = (maintenant.hour, maintenant.minute) >= (h, m)
            if passee and not await db.hors_boucle(_faite_aujourd_hui):
                async with httpx.AsyncClient(timeout=20) as client:
                    bilan = await faire(client)
                if bilan["jour"] is None:
                    attente = _NOUVEL_ESSAI_SECONDES
        except Exception as exc:  # la boucle ne doit jamais mourir
            logger.warning(f"tournée échouée ({type(exc).__name__}: {exc}) ; nouvel essai dans une heure.")
            attente = _NOUVEL_ESSAI_SECONDES
        await asyncio.sleep(attente or attente_secondes(horloge.maintenant(), h, m))
