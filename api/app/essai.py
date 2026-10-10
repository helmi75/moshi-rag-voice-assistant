"""L'établissement d'essai de la recette automatique.

Des appels automatiques arrivent en production sur UN établissement dédié, désigné par
`RECETTE_ETABLISSEMENT`. Ils portent de vrais identifiants Twilio (`CA…`) : le préfixe du
banc (`calls.PREFIXE_BANC`) ne suffit pas à les reconnaître. C'est donc l'établissement
qui les désigne. Ses appels ne comptent ni dans les chiffres du parc, ni dans les quotas,
ni dans la supervision, et `purger` les efface après chaque recette.

Pas d'import de `calls` au niveau du module : `calls` importe ce module.
"""
import logging
import os
from typing import Optional

from . import db

logger = logging.getLogger(__name__)


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


def purger() -> dict:
    """Efface appels, réservations, messages et enregistrements de l'établissement
    d'essai, et de lui SEUL : chaque suppression porte `tenant_id = ?`. Sans
    établissement d'essai configuré, ne fait rien."""
    from .voice import enregistrement

    comptes = {"appels": 0, "reservations": 0, "messages": 0, "enregistrements": 0}
    tenant_id = etablissement_id()
    if tenant_id is None:
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
    logger.info(f"essai : purge de l'établissement {tenant_id} {comptes}")
    return comptes
