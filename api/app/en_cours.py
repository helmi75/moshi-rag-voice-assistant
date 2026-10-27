"""Registre des appels en cours (ASSISTANTE-164) : la page « Appels en cours » de l'admin.

Mémoire du processus, comme la jauge de `metriques.py` : il n'y a qu'un worker uvicorn, et
un redémarrage coupe de toute façon les appels, donc rien à persister. Une entrée ne garde
ni le numéro de l'appelant ni la transcription : le support voit qu'un appel dure, pas ce qui
s'y dit.

Aucune fonction ne lève : elles sont appelées sur le chemin d'un appel, et une page de
supervision ne doit jamais en faire échouer un.

Pas d'incident par appel pour l'instant : l'erreur qui termine un appel le retire du
registre l'instant d'après, et les pannes que voit `voice/vigie.py` n'y arrivent pas. Une
colonne « Incident » aurait affiché « Aucun » sans rien mesurer (relecture d'ASSISTANTE-164).
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

CLASSIQUE = "classique"
TEMPS_REEL = "temps_reel"

_appels: dict[str, dict] = {}


def ouvrir(call_sid: Optional[str], tenant_id: int) -> None:
    try:
        if not call_sid:
            return
        _appels[call_sid] = {"call_sid": call_sid, "tenant_id": tenant_id,
                             "debut": datetime.now(timezone.utc),
                             "moteur": CLASSIQUE}
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] ouvrir KO (sans conséquence): {exc}")


def moteur(call_sid: Optional[str], nom: str) -> None:
    try:
        if call_sid in _appels:
            _appels[call_sid]["moteur"] = nom
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning(f"[en_cours] moteur KO (sans conséquence): {exc}")


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
