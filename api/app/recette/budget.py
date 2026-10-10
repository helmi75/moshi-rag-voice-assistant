"""Le plafond de dépense de la recette automatique, et le carnet de ce qu'elle a coûté.

Décision de Helmi du 10/10/2026 : 3 $ par recette, 30 $ par mois. Un passage refuse de
commencer s'il peut dépasser l'un des deux, et un scénario n'est pas joué s'il ferait
dépasser. Le carnet garde ce que chaque passage a RÉELLEMENT coûté : ce que l'application a
chiffré à la clôture de chaque appel (tarifs relevés, `calls.py`) et ce que Twilio a facturé
pour la jambe qui appelle.
"""
import json
import os
from pathlib import Path
from typing import Optional

from .. import horloge


def _nombre(nom: str, defaut: float) -> float:
    try:
        return max(0.0, float(os.getenv(nom, "").strip() or defaut))
    except ValueError:
        return defaut


def plafond_par_recette() -> float:
    return _nombre("RECETTE_PLAFOND_PAR_RECETTE", 3.0)


def plafond_par_mois() -> float:
    return _nombre("RECETTE_PLAFOND_PAR_MOIS", 30.0)


def chemin() -> Path:
    return Path(os.getenv("RECETTE_CARNET", "/app/data/recette/depenses.json"))


class CarnetIllisible(RuntimeError):
    """Le carnet existe mais ne se lit pas : on ne sait plus ce que le mois a coûté."""


def lire() -> list[dict]:
    """Les passages notés. Un carnet ABSENT est un carnet vide ; un carnet illisible lève :
    le prendre pour vide remettrait le mois à zéro, ferait oublier le passage déjà joué
    pour ce déploiement, et le prochain `noter` l'écraserait."""
    fichier = chemin()
    if not fichier.exists():
        return []
    try:
        passages = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CarnetIllisible(f"carnet des dépenses illisible ({fichier}) : {exc}") from exc
    if not isinstance(passages, list):
        raise CarnetIllisible(f"carnet des dépenses illisible ({fichier}) : ce n'est pas une liste")
    return passages


class verrou:
    """Un seul passage à la fois : deux passages simultanés franchiraient tous deux le
    plafond et le « un par déploiement ». Lève `BlockingIOError` si un autre tourne."""

    def __enter__(self):
        import fcntl

        chemin().parent.mkdir(parents=True, exist_ok=True)
        self._fichier = open(chemin().with_suffix(".verrou"), "w", encoding="ascii")
        fcntl.flock(self._fichier, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        self._fichier.close()


def du_mois(passages: Optional[list[dict]] = None, mois: Optional[str] = None) -> float:
    mois = mois or horloge.maintenant().strftime("%Y-%m")
    if passages is None and os.getenv("RECETTE_DEJA_CE_MOIS", "").strip():
        # L'étage 1 tourne dans un conteneur jetable, sans le carnet : le script lui dit
        # ce que le mois a déjà coûté.
        return _nombre("RECETTE_DEJA_CE_MOIS", 0.0)
    return round(sum(float(p.get("cout") or 0) for p in (lire() if passages is None else passages)
                     if str(p.get("le", ""))[:7] == mois), 6)


def refus(a_venir: float, deja_ce_passage: float = 0.0) -> Optional[str]:
    """Pourquoi on ne peut pas dépenser `a_venir` dollars de plus, ou None si on le peut.
    `a_venir` est une BORNE HAUTE (durée maximale × tarifs) : on refuse avant, pas après."""
    if deja_ce_passage + a_venir > plafond_par_recette():
        return (f"plafond par recette : {deja_ce_passage:.2f} $ déjà dépensés, jusqu'à {a_venir:.2f} $ "
                f"de plus dépasseraient {plafond_par_recette():.2f} $")
    mois = du_mois()
    if mois + deja_ce_passage + a_venir > plafond_par_mois():
        return (f"plafond du mois : {mois + deja_ce_passage:.2f} $ déjà dépensés, jusqu'à "
                f"{a_venir:.2f} $ de plus dépasseraient {plafond_par_mois():.2f} $")
    return None


def noter(passage: dict) -> None:
    """Ajoute un passage au carnet. Écrit à côté puis renomme : un carnet à moitié écrit
    ferait oublier la dépense du mois."""
    passages = lire() + [passage]
    fichier = chemin()
    fichier.parent.mkdir(parents=True, exist_ok=True)
    provisoire = fichier.with_suffix(".tmp")
    provisoire.write_text(json.dumps(passages, ensure_ascii=False, indent=1), encoding="utf-8")
    provisoire.replace(fichier)
