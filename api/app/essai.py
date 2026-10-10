"""L'établissement d'essai de la recette automatique.

Des appels automatiques arrivent en production sur UN établissement dédié, désigné par
`RECETTE_ETABLISSEMENT`. Ils portent de vrais identifiants Twilio (`CA…`) : le préfixe du
banc (`calls.PREFIXE_BANC`) ne suffit pas à les reconnaître. C'est donc l'établissement
qui les désigne. Ses appels ne comptent ni dans les chiffres du parc, ni dans les quotas,
ni dans la supervision, et `purger` les efface après chaque recette.

Pas d'import de `calls` au niveau du module : `calls` importe ce module.
"""
import os
from typing import Optional

from loguru import logger

from . import db


def etablissement_id() -> Optional[int]:
    """L'établissement d'essai, ou None. Une valeur vide ou illisible vaut « aucun » :
    se tromper dans ce sens ne fait rien disparaître, l'inverse effacerait des données."""
    try:
        return int(os.getenv("RECETTE_ETABLISSEMENT", "").strip())
    except ValueError:
        return None


def est_d_essai(tenant_id) -> bool:
    identifiant = etablissement_id()
    return identifiant is not None and tenant_id == identifiant


def refus(tenant_id: Optional[int] = None) -> Optional[str]:
    """Pourquoi cet établissement ne peut pas servir d'établissement d'essai, ou None. Une
    faute de frappe dans `RECETTE_ETABLISSEMENT` désignerait un vrai restaurant : il serait
    appelé, sa fiche réécrite, ses appels et ses réservations effacés. Son NOM doit donc
    contenir « essai » — celui de la production s'appelle « Banc d'essai (ne pas facturer) »."""
    tenant_id = etablissement_id() if tenant_id is None else tenant_id
    if tenant_id is None:
        return "aucun établissement d'essai (RECETTE_ETABLISSEMENT)"
    with db.get_conn() as conn:
        ligne = conn.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if ligne is None:
        return f"l'établissement d'essai n° {tenant_id} n'existe pas"
    if "essai" not in (ligne["name"] or "").lower():
        return (f"l'établissement n° {tenant_id} ne s'appelle pas « … essai … » : "
                "refus de le traiter comme établissement d'essai")
    return None


def purger() -> dict:
    """Efface appels, réservations, messages et enregistrements de l'établissement
    d'essai, et de lui SEUL : chaque suppression porte `tenant_id = ?`. Sans
    établissement d'essai configuré, ne fait rien."""
    from .voice import enregistrement

    comptes = {"appels": 0, "reservations": 0, "messages": 0, "enregistrements": 0}
    tenant_id = etablissement_id()
    if tenant_id is None:
        return comptes
    motif = refus(tenant_id)
    if motif:
        logger.warning(f"[recette] purge refusée : {motif}")
        return comptes
    with db.get_conn() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM calls WHERE tenant_id = ?", (tenant_id,)).fetchall()]
    for call_id in ids:
        comptes["enregistrements"] += enregistrement.supprimer(tenant_id, call_id)
    with db.get_conn() as conn:
        comptes["messages"] = conn.execute(
            "DELETE FROM messages WHERE tenant_id = ?", (tenant_id,)).rowcount
        comptes["appels"] = conn.execute(
            "DELETE FROM calls WHERE tenant_id = ?", (tenant_id,)).rowcount
        comptes["reservations"] = conn.execute(
            "DELETE FROM reservations WHERE tenant_id = ?", (tenant_id,)).rowcount
    logger.info(f"[recette] purge de l'établissement {tenant_id} {comptes}")
    return comptes
