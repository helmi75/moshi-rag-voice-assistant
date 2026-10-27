"""Registre des appels en cours (ASSISTANTE-164) : la page « Appels en cours » de l'admin.

Mémoire du processus, comme la jauge de `metriques.py` : il n'y a qu'un worker uvicorn, et
un redémarrage coupe de toute façon les appels, donc rien à persister. Une entrée ne garde
ni le numéro de l'appelant ni la transcription : le support voit qu'un appel dure ou a
rencontré un incident, pas ce qui s'y dit.

Aucune fonction ne lève : elles sont appelées sur le chemin d'un appel, et une page de
supervision ne doit jamais en faire échouer un.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CLASSIQUE = "classique"
TEMPS_REEL = "temps_reel"

_appels: dict[str, dict] = {}


def ouvrir(call_sid: Optional[str], tenant_id: int) -> None:
    try:
        if not call_sid:
            return
        _appels[call_sid] = {"call_sid": call_sid, "tenant_id": tenant_id,
                             "debut": datetime.now(timezone.utc),
                             "moteur": CLASSIQUE, "incident": None}
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] ouvrir KO (sans conséquence): {exc}")


def moteur(call_sid: Optional[str], nom: str) -> None:
    try:
        if call_sid in _appels:
            _appels[call_sid]["moteur"] = nom
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] moteur KO (sans conséquence): {exc}")


def incident(call_sid: Optional[str], motif: str) -> None:
    try:
        if call_sid in _appels:
            _appels[call_sid]["incident"] = str(motif)[:80]
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] incident KO (sans conséquence): {exc}")


def fermer(call_sid: Optional[str]) -> None:
    try:
        _appels.pop(call_sid, None)
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] fermer KO (sans conséquence): {exc}")


def lister(tenant_id: Optional[int] = None) -> list[dict]:
    """Copies des appels en cours, du plus ancien au plus récent."""
    try:
        rows = [dict(e) for e in list(_appels.values())
                if tenant_id is None or e["tenant_id"] == tenant_id]
        return sorted(rows, key=lambda e: e["debut"])
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] lister KO (sans conséquence): {exc}")
        return []


def reinitialiser() -> None:
    """Pour les tests : le registre est global au processus."""
    _appels.clear()
