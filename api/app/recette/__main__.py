"""`python -m app.recette <commande>` — lancé par `scripts/recette_appels.py`.

    etat              ce que le mois a déjà coûté, les plafonds, ce qui interdit un passage
    etage1            les scénarios sans Twilio (conteneur jetable)
    etage2            les vrais appels, puis la purge et le carnet (conteneur de production)
    preparer --oui    règle la fiche, l'accueil et les horaires de l'établissement d'essai

Chaque commande finit par une ligne `RESULTAT {json}` que le script relit.
"""
import argparse
import asyncio
import json
import sys


def _propre(lignes: list[dict]) -> list[dict]:
    return [{c: v for c, v in ligne.items() if c != "appel"} for ligne in lignes]


def _rendre(resultat: dict) -> None:
    print("RESULTAT " + json.dumps(resultat, ensure_ascii=False))


def tableau(lignes: list[dict]) -> str:
    """Le tableau « Scénario / État / Preuve / Coût », tel que Helmi le lit."""
    marques = {"ok": "✅", "echec": "❌", "non_joue": "⚪", "a_regarder": "⚠️"}
    sortie = ["| Scénario | État | Preuve | Coût |", "|---|---|---|---|"]
    for ligne in lignes:
        ou = "banc" if ligne.get("etage") == 1 else "vrai appel"
        sortie.append(f"| {ligne.get('recette')} · {ligne.get('titre')} ({ou}, {ligne.get('moteur')}) "
                      f"| {marques.get(ligne.get('etat'), '❓')} | {ligne.get('preuve')} "
                      f"| {float(ligne.get('cout') or 0):.4f} $ |")
    return "\n".join(sortie)


def _etat() -> dict:
    from .. import essai
    from . import budget, gardes

    try:
        passages = budget.lire()
    except budget.CarnetIllisible as exc:
        return {"active": gardes.active(), "etablissement": essai.etablissement_id(), "du_mois": 0.0,
                "plafond_recette": budget.plafond_par_recette(), "plafond_mois": budget.plafond_par_mois(),
                "interdit": str(exc), "deja_joue": None, "demarrage": gardes.demarrage()}
    return {"active": gardes.active(), "etablissement": essai.etablissement_id(),
            "du_mois": budget.du_mois(passages), "plafond_recette": budget.plafond_par_recette(),
            "plafond_mois": budget.plafond_par_mois(),
            "interdit": gardes.la_nuit() or gardes.appel_reel_en_cours(),
            "deja_joue": gardes.deja_joue(passages), "demarrage": gardes.demarrage()}


async def _etage1(seulement, moteurs) -> dict:
    from . import etage1, verdicts

    lignes = await etage1.jouer(moteurs=moteurs, seulement=seulement)
    return {"lignes": _propre(lignes),
            "cerveau": verdicts.cerveau([l["appel"] for l in lignes if l.get("appel")])}


async def _etage2(avant: dict, seulement, sans_appels: bool) -> dict:
    from .. import essai, horloge
    from . import budget, etage2, gardes, verdicts

    lignes1 = avant.get("lignes") or []
    depense = sum(float(l.get("cout") or 0) for l in lignes1)
    lignes2, refus, purge = [], "vrais appels non demandés", {}
    try:
        if not sans_appels:
            lignes2, refus = await etage2.jouer(depense, seulement)
        # Les appels d'essai ne restent pas : ni dans les comptes, ni au carnet de réservations.
        purge = essai.purger() if lignes2 else {}
    finally:
        # Quoi qu'il arrive ensuite, ce qui a été dépensé est noté : un passage oublié
        # ferait sauter le plafond du mois et rejouer de vrais appels pour ce déploiement.
        lignes = lignes1 + _propre(lignes2)
        total = round(sum(float(l.get("cout") or 0) for l in lignes), 6)
        budget.noter({"le": horloge.maintenant().strftime("%Y-%m-%dT%H:%M"), "cout": total,
                      "demarrage": gardes.demarrage(), "etage2": bool(lignes2),
                      "scenarios": len(lignes), "echecs": sum(l.get("etat") == "echec" for l in lignes)})
    cerveau = verdicts.cerveau([l["appel"] for l in lignes2 if l.get("appel")])
    return {"lignes": lignes, "refus_etage2": refus, "cout": total, "du_mois": budget.du_mois(),
            "cerveau": {"etage1": avant.get("cerveau"), "etage2": cerveau}, "purge": purge}


def _preparer() -> dict:
    from .. import essai, tenants
    from . import scenarios

    identifiant = essai.etablissement_id()
    tenant = tenants.get_by_id(identifiant) if identifiant else None
    if tenant is None or essai.refus(tenant.id):
        return {"erreur": essai.refus(identifiant)}
    tenants.update_tenant(tenant.id, greeting=scenarios.ACCUEIL, greeting_customized=1,
                          knowledge_base=scenarios.FICHE, opening_hours=json.dumps(scenarios.HORAIRES))
    return {"etablissement": tenant.id, "nom": tenant.name, "regle": ["accueil", "fiche", "horaires"]}


def main(arguments=None) -> int:
    analyse = argparse.ArgumentParser(prog="python -m app.recette")
    analyse.add_argument("commande", choices=("etat", "etage1", "etage2", "preparer"))
    analyse.add_argument("--scenarios", default="", help="clés séparées par des virgules ; vide : tous")
    analyse.add_argument("--moteurs", default="", help="etage1 : gpt_live, classique, ou les deux (défaut)")
    analyse.add_argument("--etage1", default="", help="le RESULTAT de l'étage 1, en JSON")
    analyse.add_argument("--sans-appels", action="store_true", help="étage 2 : ne compose aucun numéro")
    analyse.add_argument("--oui", action="store_true", help="preparer : écrit dans la base")
    choix = analyse.parse_args(arguments)
    seulement = {c.strip() for c in choix.scenarios.split(",") if c.strip()} or None

    if choix.commande == "etage1":
        from . import etage1

        etage1.preparer_l_environnement()     # AVANT tout import de l'application
        moteurs = tuple(m for m in etage1.MOTEURS if m in choix.moteurs.split(",")) or etage1.MOTEURS
        resultat = asyncio.run(_etage1(seulement, moteurs))
    elif choix.commande == "etage2":
        from . import budget

        try:
            with budget.verrou():
                budget.lire()                  # illisible : on refuse avant de dépenser
                resultat = asyncio.run(_etage2(json.loads(choix.etage1 or "{}"), seulement, choix.sans_appels))
        except BlockingIOError:
            print("Un autre passage de recette est en cours : refus.")
            return 2
        except budget.CarnetIllisible as exc:
            print(f"Recette refusée : {exc}")
            return 2
        print(tableau(resultat["lignes"]))
    elif choix.commande == "preparer":
        if not choix.oui:
            print("preparer écrit dans la base de l'établissement d'essai : relancer avec --oui")
            return 2
        resultat = _preparer()
    else:
        resultat = _etat()
    _rendre(resultat)
    return 1 if any(l.get("etat") == "echec" for l in resultat.get("lignes", [])) else 0


if __name__ == "__main__":
    sys.exit(main())
