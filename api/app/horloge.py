"""L'heure du restaurant, à un seul endroit.

Les dates sont STOCKÉES en UTC (`…Z`, format SQLite) ; mais « aujourd'hui », « ce mois-ci »,
« il y a sept jours » se comptent à l'heure du restaurant. Entre minuit et deux heures,
heure de Paris, les deux calendriers ne sont pas sur le même jour : `date.today()` côté
serveur se trompait de jour, et les fenêtres SQL en `date('now')` mettaient un appel de
00 h 30 le 1er du mois… dans le mois précédent, sur la facture.

D'où ce module : le fuseau, l'instant présent, et les BORNES de fenêtre calculées ici en
Python — au fuseau du restaurant — puis converties en UTC ISO pour le SQL. `llm.maintenant`
délègue ici et garde son nom : les tests le fixent pour figer le prompt et les outils.
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

FUSEAU = ZoneInfo(os.getenv("RESTAURANT_TIMEZONE", "Europe/Paris"))

# En toutes lettres : le modèle doit résoudre « vendredi prochain » sans rien deviner, et
# l'ISO seul ne dit pas quel jour de la semaine on est. Table figée plutôt que `locale` :
# les locales fr_FR ne sont pas installées dans l'image Docker.
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

_FORMAT_UTC = "%Y-%m-%dT%H:%M:%SZ"


def maintenant() -> datetime:
    """L'instant présent, au fuseau du restaurant."""
    return datetime.now(FUSEAU)


def aujourd_hui() -> date:
    """La date du jour, au fuseau du restaurant — pas celle du serveur."""
    return maintenant().date()


def en_toutes_lettres(jour: date) -> str:
    return f"{JOURS[jour.weekday()]} {jour.day} {MOIS[jour.month - 1]} {jour.year}"


def utc_iso(instant: Optional[datetime] = None) -> str:
    """Un instant au format des colonnes SQLite (`%Y-%m-%dT%H:%M:%SZ`, UTC).
    Sans argument : maintenant. Un instant naïf est réputé UTC."""
    if instant is None:
        instant = datetime.now(timezone.utc)
    elif instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).strftime(_FORMAT_UTC)


def lire_utc(brut) -> Optional[datetime]:
    """Relit une date écrite par SQLite. Tolérant : une date illisible vaut « inconnue »,
    jamais une exception — la sonde de supervision passe par ici."""
    if not brut:
        return None
    texte = str(brut).strip().replace(" ", "T")
    if texte.endswith("Z"):
        texte = texte[:-1] + "+00:00"
    try:
        lu = datetime.fromisoformat(texte)
    except ValueError:
        return None
    return lu if lu.tzinfo else lu.replace(tzinfo=timezone.utc)


def debut_du_jour(jour: date) -> datetime:
    """Minuit, heure du restaurant, de ce jour-là."""
    return datetime(jour.year, jour.month, jour.day, tzinfo=FUSEAU)


def il_y_a_jours(n: int) -> str:
    """La borne SQL « depuis n jours » : minuit du restaurant il y a n jours, en UTC ISO.
    `il_y_a_jours(0)` = le début de la journée en cours."""
    return utc_iso(debut_du_jour(aujourd_hui() - timedelta(days=int(n))))


def il_y_a(jours: float) -> str:
    """L'instant il y a n jours — pas minuit : pour les durées de conservation et les
    fenêtres glissantes de la sonde, qui se comptent en jours pleins depuis maintenant."""
    return utc_iso(datetime.now(timezone.utc) - timedelta(days=float(jours)))


def debut_du_mois() -> str:
    """La borne SQL du mois calendaire en cours — la maille de facturation."""
    return utc_iso(debut_du_jour(aujourd_hui().replace(day=1)))
