"""Le calendrier des réservations (SCRUM-112) : ce qui se calcule sans base ni réseau.

Mois, semaine, jour : les bornes, la navigation, les titres en français, le regroupement
par jour et par heure, et un statut COMMUN aux deux carnets — le nôtre (confirmée ou
annulée) et resOS (à valider, validée, liste d'attente, refusée…). La page ne connaît que
ce statut commun : elle n'a pas à savoir d'où vient une réservation pour la dessiner.
"""
import calendar
from datetime import date, timedelta
from typing import Iterable, Optional

from .. import horloge

VUES = ("mois", "semaine", "jour", "liste")

# Statut resOS → (état commun, libellé, pastille). « Annulée » regroupe tout ce qui ne
# viendra pas : ces réservations restent visibles, barrées, mais ne comptent pas.
_RESOS = {
    "request": ("a_valider", "À valider", "chip-warn"),
    "approved": ("confirmee", "Validée", "chip-good"),
    "waitlist": ("attente", "Liste d'attente", "chip-warn"),
    "arrived": ("confirmee", "Arrivée", "chip-good"),
    "seated": ("confirmee", "Installée", "chip-good"),
    "left": ("confirmee", "Partie", ""),
    "declined": ("annulee", "Refusée", "chip-bad"),
    "canceled": ("annulee", "Annulée", "chip-bad"),
    "no_show": ("annulee", "Absente", "chip-bad"),
}

# Plage d'heures affichée en vues semaine et jour, élargie si une réservation en sort.
HEURE_MIN, HEURE_MAX = 11, 23


def interne(r: dict, tenant_id: int) -> dict:
    annulee = bool(r.get("cancelled_at"))
    return {**r, "tenant_id": tenant_id, "source": "interne", "modifiable": not annulee,
            "etat": "annulee" if annulee else "confirmee",
            "libelle": "Annulée" if annulee else "Confirmée",
            "chip": "chip-bad" if annulee else "chip-good"}


def resos(r: dict, tenant_id: int) -> dict:
    etat, libelle, chip = _RESOS.get(r.get("statut"), ("confirmee", r.get("statut") or "—", ""))
    return {**r, "tenant_id": tenant_id, "source": "resos", "modifiable": False,
            "etat": etat, "libelle": libelle, "chip": chip}


def ancre(brut: Optional[str], aujourd_hui: date) -> date:
    try:
        return date.fromisoformat(brut) if brut else aujourd_hui
    except ValueError:
        return aujourd_hui


def _lundi(jour: date) -> date:
    return jour - timedelta(days=jour.weekday())


def bornes(vue: str, jour: date) -> tuple[date, date]:
    """Premier et dernier jour affichés. Le mois s'affiche en semaines entières : les
    jours du mois voisin en tête et en fin de grille comptent aussi."""
    if vue == "jour":
        return jour, jour
    if vue == "semaine":
        return _lundi(jour), _lundi(jour) + timedelta(days=6)
    premier = jour.replace(day=1)
    dernier = jour.replace(day=calendar.monthrange(jour.year, jour.month)[1])
    return _lundi(premier), _lundi(dernier) + timedelta(days=6)


def voisins(vue: str, jour: date) -> tuple[date, date]:
    """(précédent, suivant) pour les flèches."""
    if vue == "jour":
        return jour - timedelta(days=1), jour + timedelta(days=1)
    if vue == "semaine":
        return jour - timedelta(days=7), jour + timedelta(days=7)
    premier = jour.replace(day=1)
    avant = (premier - timedelta(days=1)).replace(day=1)
    apres = (premier + timedelta(days=32)).replace(day=1)
    return avant, apres


def titre(vue: str, jour: date) -> str:
    mois = horloge.MOIS
    if vue == "jour":
        return horloge.en_toutes_lettres(jour).capitalize()
    if vue == "semaine":
        debut, fin = bornes("semaine", jour)
        if debut.month == fin.month:
            return f"Du {debut.day} au {fin.day} {mois[fin.month - 1]} {fin.year}"
        if debut.year == fin.year:
            return (f"Du {debut.day} {mois[debut.month - 1]} au {fin.day} "
                    f"{mois[fin.month - 1]} {fin.year}")
        return (f"Du {debut.day} {mois[debut.month - 1]} {debut.year} au {fin.day} "
                f"{mois[fin.month - 1]} {fin.year}")
    return f"{mois[jour.month - 1].capitalize()} {jour.year}"


def jours(debut: date, fin: date) -> list[date]:
    return [debut + timedelta(days=n) for n in range((fin - debut).days + 1)]


def resume(resas: Iterable[dict]) -> dict:
    """Ce qui se compte : les réservations qui viendront. Une annulée gonflerait la
    journée — on prépare des couverts pour personne."""
    actives = [r for r in resas if r["etat"] != "annulee"]
    return {"reservations": len(actives),
            "couverts": sum(int(r.get("party_size") or 0) for r in actives),
            "a_valider": sum(1 for r in actives if r["etat"] == "a_valider"),
            "annulees": sum(1 for r in resas if r["etat"] == "annulee")}


def par_jour(resas: Iterable[dict]) -> dict[str, list[dict]]:
    groupes: dict[str, list[dict]] = {}
    for r in sorted(resas, key=lambda r: (r.get("date") or "", r.get("time") or "")):
        groupes.setdefault(r.get("date") or "", []).append(r)
    return groupes


def _heure(r: dict) -> Optional[int]:
    try:
        return int(str(r.get("time") or "")[:2])
    except ValueError:
        return None


def heures(resas: Iterable[dict]) -> list[int]:
    """Les lignes des vues semaine et jour : le service habituel, élargi pour qu'aucune
    réservation (un petit-déjeuner, un souper à 0 h 30) ne disparaisse de la grille."""
    vues = [h for h in map(_heure, resas) if h is not None]
    return list(range(min([HEURE_MIN, *vues]), max([HEURE_MAX, *vues]) + 1))


def par_heure(resas: Iterable[dict]) -> dict[int, list[dict]]:
    groupes: dict[int, list[dict]] = {}
    for r in sorted(resas, key=lambda r: r.get("time") or ""):
        h = _heure(r)
        if h is not None:
            groupes.setdefault(h, []).append(r)
    return groupes
