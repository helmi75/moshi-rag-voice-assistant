"""Le verdict d'un scénario, lu en base et au journal de l'appel — jamais à l'oreille.

Pur : on lui donne la ligne de l'appel et les réservations du numéro, il rend un état et sa
preuve. Qui l'appelle lit la base.
"""
import json
import unicodedata
from typing import Optional

from .. import annonce
from .scenarios import Scenario

OK, ECHEC, NON_JOUE = "ok", "echec", "non_joue"
# La machine ne peut pas conclure : à lire par quelqu'un. Ne fait pas échouer la recette.
A_REGARDER = "a_regarder"


def sur_la_chaine_classique(etat: str, preuve: str) -> tuple[str, str]:
    """Un échec sur la chaîne classique n'est pas un verdict. Elle rend la parole au
    moindre blanc et pose ses questions une à une ; un client de script, qui ne répond pas
    à ce qu'on lui demande, y perd son heure ou son nom là où une personne les redirait
    (essai du 10/10/2026 : quatre réservations sur cinq manquées, toutes par le script).
    On garde la conversation, et on le dit."""
    if etat != ECHEC:
        return etat, preuve
    return A_REGARDER, "client de script dépassé, pas un verdict — " + preuve


def _plat(texte) -> str:
    sans = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    return " ".join(sans.lower().split())


def _json(brut, defaut):
    try:
        lu = json.loads(brut) if isinstance(brut, str) and brut else brut
    except ValueError:
        return defaut
    return lu if isinstance(lu, type(defaut)) else defaut


def dit_par_l_assistante(appel: dict) -> str:
    return " ".join(str(t.get("content") or "") for t in _json(appel.get("transcript"), [])
                    if isinstance(t, dict) and t.get("role") == "assistant")


def conversation(appel: Optional[dict], tours: int = 16) -> list[str]:
    """La fin de ce qui s'est dit, pour comprendre un échec sans rouvrir la base."""
    lignes = []
    for tour in _json((appel or {}).get("transcript"), [])[-tours:]:
        if isinstance(tour, dict) and tour.get("role") in ("user", "assistant"):
            qui = "client" if tour["role"] == "user" else "elle"
            lignes.append(f"{qui} : {' '.join(str(tour.get('content') or '').split())[:160]}")
    return lignes


def juger(scenario: Scenario, appel: Optional[dict], reservations: list[dict]) -> tuple[str, str]:
    """(état, preuve). `reservations` : celles du numéro du client, créées depuis le début
    de l'appel, annulées comprises."""
    if appel is None:
        return ECHEC, "aucun appel au journal : l'application ne l'a pas reçu"
    numero = f"appel {appel.get('id')}"
    if appel.get("secours_motif"):
        return ECHEC, f"{numero} passé en secours ({appel['secours_motif']})"
    if appel.get("status") != "completed" or not appel.get("ended_at"):
        return ECHEC, f"{numero} non clos normalement (statut {appel.get('status')})"
    journal = _json(appel.get("journal"), {})
    if journal.get("a_verifier"):
        return ECHEC, f"{numero} marqué « À vérifier » : « {journal['a_verifier'].get('phrase')} »"
    dit = dit_par_l_assistante(appel)
    if scenario.doit_dire and not any(mot in _plat(dit) for mot in map(_plat, scenario.doit_dire)):
        return ECHEC, f"{numero} : l'assistante n'a dit aucun de {list(scenario.doit_dire)}"
    actives = [r for r in reservations if not r.get("cancelled_at")]

    if scenario.attendu is None:
        if actives or appel.get("reservation_id") or appel.get("reservation_externe"):
            return ECHEC, f"{numero} : une réservation existe alors qu'aucune n'était attendue"
        return OK, f"{numero} : aucune réservation, et elle a dit ce qu'il fallait"

    if len(actives) != 1:
        return ECHEC, f"{numero} : {len(actives)} réservation(s) active(s) pour ce client, 1 attendue"
    resa, attendu = actives[0], scenario.attendu
    if appel.get("reservation_id") != resa.get("id"):
        return ECHEC, f"{numero} : la réservation {resa.get('id')} n'est pas rattachée à l'appel"
    ecarts = []
    if int(resa.get("party_size") or 0) != attendu["party_size"]:
        ecarts.append(f"{resa.get('party_size')} couverts au lieu de {attendu['party_size']}")
    if str(resa.get("time") or "")[:5] != attendu["time"]:
        ecarts.append(f"{resa.get('time')} au lieu de {attendu['time']}")
    if attendu["nom"] not in _plat(resa.get("customer_name")):
        ecarts.append(f"au nom de « {resa.get('customer_name')} » au lieu de « {attendu['nom']} »")
    if ecarts:
        return ECHEC, f"{numero} : réservation {resa.get('id')} — " + ", ".join(ecarts)
    if scenario.langue == "fr" and not any(annonce.phrase_d_annonce(str(t.get("content") or ""))
                                           for t in _json(appel.get("transcript"), [])
                                           if isinstance(t, dict) and t.get("role") == "assistant"):
        # La table est là, mais elle ne l'a pas dit : le client repart sans savoir.
        return ECHEC, f"{numero} : réservation {resa.get('id')} créée, mais jamais annoncée au client"
    return OK, (f"{numero} : réservation {resa.get('id')}, {resa.get('party_size')} couverts, "
                f"{resa.get('date')} à {str(resa.get('time'))[:5]}, « {resa.get('customer_name')} »")


def cerveau(appels: list[dict]) -> dict:
    """T19 : les durées des requêtes au cerveau relevées dans les journaux des appels
    GPT-Live, et combien sont restées sans réponse."""
    durees, sans_reponse, travaux = [], 0, 0
    for appel in appels:
        for confie in _json(appel.get("journal"), {}).get("delegations") or []:
            travaux += 1
            for duree in confie.get("generations_ms") or []:
                if duree is None:
                    sans_reponse += 1
                else:
                    durees.append(int(duree))
    durees.sort()
    return {"travaux": travaux, "generations": len(durees), "sans_reponse": sans_reponse,
            "mediane_ms": durees[len(durees) // 2] if durees else None,
            "max_ms": durees[-1] if durees else None}
