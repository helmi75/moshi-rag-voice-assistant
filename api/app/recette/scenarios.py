"""Les scénarios joués par la machine, repris de `docs/RECETTE.md`.

Chaque scénario est ce que DIT le client, et ce qu'on doit retrouver en base ensuite. Les
répliques sont fixes : le client n'improvise pas, il attend seulement son tour. Les jours
sont dits par leur nom (« samedi ») : la date exacte est celle que le serveur en déduit, et
le verdict la relit dans la réservation plutôt que de la recalculer.

Chaque scénario appelle depuis un numéro différent à l'étage 1 ; à l'étage 2 c'est le même
(le nôtre), d'où des créneaux tous différents : le serveur refuse une seconde réservation
du même numéro au même créneau.

Les premières répliques sont d'un seul souffle, sans « Bonjour, » ni virgule : la voix de
synthèse marque une pause à chaque virgule, et la chaîne classique prenait cette pause pour
la fin de la phrase — elle répondait à « Bonjour » ou perdait l'heure (essai du
10/10/2026). Un client de script ne se reprend pas comme le ferait une personne.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Scenario:
    cle: str
    recette: str                  # la ligne de docs/RECETTE.md que ce scénario joue
    titre: str
    repliques: tuple[str, ...]
    langue: str = "fr"
    # Ce que la réservation créée doit porter ; None : aucune réservation ne doit exister.
    attendu: Optional[dict] = field(default=None, hash=False)
    # Des mots que l'assistante doit avoir dits (un seul suffit), en minuscules.
    doit_dire: tuple[str, ...] = ()
    # Réglages du client d'essai propres à ce scénario (`client.Client`) ; vide : ceux par défaut.
    client: dict = field(default_factory=dict, hash=False)


_FIN = "Non merci, c'est tout. Au revoir."

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("t17a", "T17", "Réservation confirmée par un simple « oui » (1/3)", (
        "Je voudrais réserver une table pour deux personnes samedi soir à vingt heures.",
        "Au nom de Martin, s'il vous plaît.", "Oui.", _FIN),
        attendu={"party_size": 2, "time": "20:00", "nom": "martin", "jour": "samedi"}),
    Scenario("t17b", "T17", "Réservation confirmée par un simple « oui » (2/3)", (
        "Je voudrais une table pour quatre personnes vendredi à midi et demi.",
        "C'est au nom de Bernard.", "Oui.", _FIN),
        attendu={"party_size": 4, "time": "12:30", "nom": "bernard", "jour": "vendredi"}),
    Scenario("t17c", "T17", "Réservation confirmée par un simple « oui » (3/3)", (
        "Je souhaite réserver une table pour six personnes jeudi à dix-neuf heures trente.",
        "Au nom de Petit.", "Oui.", _FIN),
        attendu={"party_size": 6, "time": "19:30", "nom": "petit", "jour": "jeudi"}),
    Scenario("c8", "C8", "Le nom corrigé en cours d'appel : une seule réservation, au bon nom", (
        "Je voudrais une table pour trois personnes mercredi à vingt heures trente.",
        "Au nom de Dupont.",
        "Non, pardon, je me suis trompé : c'est au nom de Lambert. Lambert.",
        "Oui, c'est bien ça.", _FIN),
        attendu={"party_size": 3, "time": "20:30", "nom": "lambert", "jour": "mercredi"}),
    Scenario("t6", "T6", "Un jour de fermeture est refusé", (
        "Je voudrais réserver une table pour deux personnes dimanche à midi.",
        "Non, tant pis, merci. Au revoir."),
        attendu=None, doit_dire=("fermé", "fermés", "fermée", "ferme")),
    Scenario("t7", "T7", "Un appel en anglais est servi en anglais", (
        "I would like to book a table for two people on Tuesday at eight p.m.",
        "The name is Smith.", "Yes, that's right.", "No, thank you. Goodbye."),
        langue="en", attendu={"party_size": 2, "time": "20:00", "nom": "smith", "jour": "mardi"},
        doit_dire=("table", "thank", "booked", "reservation", "name")),
)

# L'établissement d'essai, tel que l'étage 1 le fabrique dans sa base jetable, et tel que
# `python -m app.recette preparer` le règle en production : ouvert midi et soir, fermé le
# dimanche (c'est ce que le scénario T6 éprouve).
HORAIRES = {"semaine": {jour: [["12:00", "14:30"], ["19:00", "22:30"]]
                        for jour in ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi")}
            | {"dimanche": []},
            "fermetures": []}
NOM = "Banc d'essai (ne pas facturer)"
ACCUEIL = "Bonjour, Le Banc d'essai."
FICHE = ("## Le restaurant\nLe Banc d'essai est un restaurant fictif qui sert à éprouver l'assistante. "
         "Cuisine française.\n\n## Horaires\nDu lundi au samedi, midi et soir. Fermé le dimanche.\n")


# S10 : raccrocher à la première sonnerie, puis rappeler. Le second appel joue ce scénario.
S10 = Scenario(
    "s10", "S10", "Raccrocher à la première sonnerie puis rappeler : le second appel est servi", (
        "Je voudrais réserver une table pour deux personnes lundi à dix-neuf heures.",
        "Au nom de Garnier.", "Oui.", "Non merci, c'est tout. Au revoir."),
    # « Garnier » et non « Roux » : le 12/10/2026 la table a été notée « Rou », qui se
    # prononce pareil, et la ligne sortait rouge pour une orthographe.
    attendu={"party_size": 2, "time": "19:00", "nom": "garnier", "jour": "lundi"})

# S6/S7 : l'établissement d'essai est mis sur répondeur (`recette/secours.py`), le client
# écoute l'annonce et le bip, dicte son message et se tait. Joué à l'étage 2 seulement :
# c'est Twilio qui enregistre. `<Record timeout="5">` conclut « pas de message » si l'on
# ne parle pas dans les cinq secondes du bip : le client attend donc 1,5 s de silence, pas
# les six secondes (ni les douze après une phrase courte) qui laissent la parole à
# l'assistante sur un appel normal.
REPONDEUR = Scenario(
    "repondeur", "S6 S7", "Le répondeur prend un message quand l'assistante est mise de côté", (
        "Bonjour, c'est Garnier, rappelez-moi pour une table samedi.",),
    client={"silence": 1.5, "silence_court": 0.0})

VRAIS_APPELS = (REPONDEUR, S10)


def par_cle(cle: str) -> Optional[Scenario]:
    """Le scénario que le client d'essai doit jouer. S10 compris : le 11/10/2026, au premier
    passage de vrais appels, `/ws/recette` ne le connaissait pas et refusait son flux."""
    return next((s for s in SCENARIOS + VRAIS_APPELS if s.cle == cle), None)
