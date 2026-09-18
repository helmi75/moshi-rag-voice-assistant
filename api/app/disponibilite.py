"""Horaires d'ouverture structurés : ce que l'assistante peut REFUSER, et pourquoi.

Jusqu'ici `check_availability` répondait toujours « disponible » : ni horaires ni jours de
fermeture, seulement le texte libre de la fiche « Horaires » relu par le modèle à chaque
tour. L'assistante pouvait enregistrer quatorze couverts un lundi de fermeture à trois
heures du matin — une table promise qu'on ne peut pas honorer, pire qu'un appel manqué.

Ce module ne fait aucune entrée-sortie : il lit un JSON, répond « ouvert » ou « voici
pourquoi c'est fermé », et écrit les horaires en toutes lettres pour le prompt. Décision
du 18/09/2026 : horaires seulement, pas de capacité — assez pour refuser un jour fermé ou
une heure hors service, simple à régler dans l'admin.

Format stocké (`tenants.opening_hours`, JSON) :
    {"semaine": {"lundi": [], "mardi": [["12:00", "14:30"], ["19:00", "22:30"]], …},
     "fermetures": ["2026-12-25", "2026-08-03/2026-08-17"]}
Une plage = les heures auxquelles une réservation est ACCEPTÉE, bornes incluses : le
restaurateur saisit « 22:30 » s'il prend encore une table à 22 h 30. Une fin avant le début
(« 19:00 »–« 01:00 ») déborde sur le lendemain matin. Jour absent ou liste vide = fermé.

NULL = non renseigné : on ne refuse RIEN (compatibilité avec le parc existant), et l'alerte
du parc le signale. Un JSON illisible vaut pareil — refuser toutes les tables sur une donnée
corrompue serait pire — et se voit au journal.
"""
import json
from datetime import date, datetime, time, timedelta
from typing import Mapping, Optional

from loguru import logger

from . import horloge

JOURS = horloge.JOURS
# Au-delà de cette heure, une « fin » plus petite que le « début » n'est plus un service
# de nuit mais une faute de saisie (14 h → 12 h).
_FIN_DE_NUIT_MAX = time(6, 0)


def _heure(texte) -> Optional[time]:
    try:
        return time.fromisoformat(str(texte).strip())
    except (TypeError, ValueError):
        return None


def _h(t: time) -> str:
    return f"{t.hour}h{t.minute:02d}"


def charger(brut: Optional[str]) -> Optional[dict]:
    """JSON stocké → horaires normalisés, ou None (non renseigné, ou illisible)."""
    if not brut or not str(brut).strip():
        return None
    try:
        donnees = json.loads(brut)
        semaine_brute = donnees.get("semaine") or {}
        semaine = {}
        for jour in JOURS:
            plages = []
            for debut, fin in semaine_brute.get(jour) or []:
                d, f = _heure(debut), _heure(fin)
                if d is None or f is None:
                    raise ValueError(f"plage illisible le {jour}")
                plages.append((d, f))
            semaine[jour] = plages
        fermetures = []
        for entree in donnees.get("fermetures") or []:
            debut, _, fin = str(entree).partition("/")
            d = date.fromisoformat(debut.strip())
            f = date.fromisoformat(fin.strip()) if fin else d
            fermetures.append((min(d, f), max(d, f)))
        return {"semaine": semaine, "fermetures": fermetures}
    except (TypeError, ValueError, AttributeError) as exc:
        # Fail-open délibéré : sur une donnée corrompue, mieux vaut accepter comme avant
        # que refuser toutes les tables. Le journal le dit ; l'alerte du parc aussi.
        logger.warning(f"horaires d'ouverture illisibles, ignorés ({exc})")
        return None


def est_configure(horaires: Optional[dict]) -> bool:
    return horaires is not None


def _ferme_exceptionnellement(horaires: dict, jour: date) -> bool:
    return any(debut <= jour <= fin for debut, fin in horaires["fermetures"])


def _dans_plage(t: time, debut: time, fin: time) -> bool:
    if fin >= debut:
        return debut <= t <= fin
    return t >= debut  # service de nuit : la soirée, avant minuit


def _ouvert(horaires: dict, jour: date, t: time) -> bool:
    if any(_dans_plage(t, d, f) for d, f in horaires["semaine"][JOURS[jour.weekday()]]):
        return True
    # Le petit matin appartient au service de nuit de la VEILLE (« 19h–1h » du mardi
    # couvre le mercredi jusqu'à 1 h).
    veille = JOURS[(jour - timedelta(days=1)).weekday()]
    return any(f < d and t <= f for d, f in horaires["semaine"][veille])


def _plages_en_lettres(plages) -> str:
    return " et ".join(f"{_h(d)}-{_h(f)}" for d, f in plages) if plages else "fermé"


def motif_de_fermeture(horaires: Optional[dict], creneau: datetime) -> Optional[str]:
    """None si une réservation est possible à cet instant ; sinon le motif, écrit POUR LE
    MODÈLE : il cite les plages ouvertes du jour, pour qu'il propose une alternative au
    lieu d'un « je ne peux pas »."""
    if horaires is None:
        return None
    jour, t = creneau.date(), creneau.time().replace(second=0, microsecond=0)
    nom = JOURS[jour.weekday()]
    if _ferme_exceptionnellement(horaires, jour):
        return (f"Fermeture exceptionnelle le {horloge.en_toutes_lettres(jour)} : aucune "
                "réservation ce jour-là. Propose un autre jour.")
    if _ouvert(horaires, jour, t):
        return None
    plages = horaires["semaine"][nom]
    if not plages:
        ouverts = [j for j in JOURS if horaires["semaine"][j]]
        return (f"Fermé le {nom} : aucune réservation ce jour-là. Jours ouverts : "
                f"{', '.join(ouverts) if ouverts else 'aucun'}. Propose un autre jour.")
    return (f"À {_h(t)} le {nom}, le restaurant ne prend pas de réservation : il ouvre "
            f"{_plages_en_lettres(plages)}. Propose un horaire dans ces plages.")


def en_toutes_lettres(horaires: Optional[dict], aujourd_hui: Optional[date] = None) -> str:
    """Les horaires tels que le prompt les reçoit : jours identiques groupés, fermetures
    à venir. Court, parce que le prompt part au modèle à chaque tour."""
    if horaires is None:
        return ""
    groupes: list[tuple[list[str], list]] = []
    for jour in JOURS:
        plages = horaires["semaine"][jour]
        if groupes and groupes[-1][1] == plages:
            groupes[-1][0].append(jour)
        else:
            groupes.append(([jour], plages))
    lignes = []
    for jours, plages in groupes:
        if len(jours) == 1:
            tete = jours[0].capitalize()
        elif len(jours) == 2:
            tete = f"{jours[0].capitalize()} et {jours[1]}"
        else:
            tete = f"Du {jours[0]} au {jours[-1]}"
        lignes.append(f"{tete} : {_plages_en_lettres(plages)}.")
    reference = aujourd_hui or horloge.aujourd_hui()
    a_venir = [(d, f) for d, f in horaires["fermetures"] if f >= reference]
    if a_venir:
        morceaux = []
        for d, f in a_venir[:5]:
            if d == f:
                morceaux.append(f"le {horloge.en_toutes_lettres(d)}")
            else:
                morceaux.append(f"du {horloge.en_toutes_lettres(d)} au {horloge.en_toutes_lettres(f)}")
        lignes.append(f"Fermetures exceptionnelles : {', '.join(morceaux)}.")
    return "\n".join(lignes)


def section_prompt(brut: Optional[str]) -> str:
    """Le bloc à insérer dans le prompt système ; vide si rien n'est renseigné (la fiche
    « Horaires » de la base de connaissances reste alors la seule source, comme avant)."""
    horaires = charger(brut)
    if not est_configure(horaires):
        return ""
    return ("# Horaires de réservation\n"
            f"{en_toutes_lettres(horaires)}\n"
            "Ne propose JAMAIS un créneau hors de ces horaires : l'outil le refuserait. Jour "
            "fermé ou heure hors service : dis-le simplement et propose le créneau ouvert le "
            "plus proche.\n\n")


def depuis_formulaire(form: Mapping) -> tuple[Optional[dict], list[str]]:
    """Le formulaire de l'admin → (JSON prêt à stocker, erreurs). Sept jours, deux plages
    chacun (`mardi_1_debut`, `mardi_1_fin`, `mardi_2_debut`…), une case `mardi_ferme`, et
    un champ `fermetures` (une date ou un intervalle `AAAA-MM-JJ/AAAA-MM-JJ` par ligne)."""
    erreurs: list[str] = []
    semaine: dict = {}
    for jour in JOURS:
        plages = []
        if str(form.get(f"{jour}_ferme", "")).lower() not in ("on", "1", "true"):
            for n in (1, 2):
                debut = str(form.get(f"{jour}_{n}_debut", "") or "").strip()
                fin = str(form.get(f"{jour}_{n}_fin", "") or "").strip()
                if not debut and not fin:
                    continue
                d, f = _heure(debut), _heure(fin)
                if d is None or f is None:
                    erreurs.append(f"{jour.capitalize()}, plage {n} : les deux heures sont "
                                   "nécessaires (HH:MM).")
                    continue
                if f < d and f > _FIN_DE_NUIT_MAX:
                    erreurs.append(f"{jour.capitalize()}, plage {n} : la fin ({_h(f)}) précède "
                                   f"le début ({_h(d)}).")
                    continue
                plages.append([debut, fin])
            if len(plages) == 2:
                (d1, f1), (d2, _) = (_heure(plages[0][0]), _heure(plages[0][1])), (_heure(plages[1][0]), None)
                if d2 <= d1 or (f1 >= d1 and d2 <= f1):
                    erreurs.append(f"{jour.capitalize()} : la deuxième plage doit commencer "
                                   "après la fin de la première.")
        semaine[jour] = plages
    fermetures = []
    for ligne in str(form.get("fermetures", "") or "").splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        debut, _, fin = ligne.partition("/")
        try:
            d = date.fromisoformat(debut.strip())
            f = date.fromisoformat(fin.strip()) if fin else d
        except ValueError:
            erreurs.append(f"Fermeture « {ligne} » : une date AAAA-MM-JJ, ou un intervalle "
                           "AAAA-MM-JJ/AAAA-MM-JJ.")
            continue
        fermetures.append(f"{min(d, f).isoformat()}/{max(d, f).isoformat()}" if f != d else d.isoformat())
    if erreurs:
        return None, erreurs
    return {"semaine": semaine, "fermetures": fermetures}, []


def grille(horaires: Optional[dict]) -> list[dict]:
    """Ce que le gabarit affiche : sept lignes, deux plages en texte, la case « fermé »."""
    lignes = []
    for jour in JOURS:
        plages = horaires["semaine"][jour] if horaires else []
        textes = [(d.strftime("%H:%M"), f.strftime("%H:%M")) for d, f in plages] + [("", ""), ("", "")]
        lignes.append({"nom": jour, "label": jour.capitalize(),
                       "ferme": horaires is not None and not plages, "plages": textes[:2]})
    return lignes


def fermetures_en_texte(horaires: Optional[dict]) -> str:
    if not horaires:
        return ""
    return "\n".join(d.isoformat() if d == f else f"{d.isoformat()}/{f.isoformat()}"
                     for d, f in horaires["fermetures"])
