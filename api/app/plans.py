"""Catalogue des formules commerciales — la grille tarifaire, arrêtée (#29).

Liste FERMÉE, pour la même raison que le catalogue de voix : ce qui est vendu doit
être décidé à un seul endroit. Une formule inventée ailleurs dans le code (une chaîne
en base éditée à la main, un import CSV) donnerait un plafond que personne n'a arrêté
et une facture que personne ne sait justifier.

**On vend des minutes, pas des appels** (ASSISTANTE-120, grille validée le 02/10/2026).
Les concurrents comptent tous en minutes : un restaurateur ne pouvait pas comparer
« 150 appels » à « 150 minutes ». Et un forfait en appels nous faisait porter la durée
de l'appel : à 8 minutes de moyenne, la formule Service tombait à 26 % de marge.

**Les prix sont en euros hors taxes, les coûts en dollars.** Ce n'est pas une
négligence : les fournisseurs (Twilio, Deepgram, Mistral, OpenRouter) facturent en
dollars et le produit se vend en euros. Convertir dans le code figerait un taux de
change qui dérive ; la marge se calcule donc hors du code, dans `docs/TARIFS.md`, avec
le taux du jour écrit noir sur blanc.

Voir `docs/TARIFS.md` pour le raisonnement, le relevé des concurrents et ce qui reste à
mesurer.
"""
from dataclasses import dataclass
from typing import Optional

# Part du forfait à partir de laquelle on prévient. Prévenir APRÈS coup ne sert à rien :
# le restaurateur découvrirait le dépassement sur sa facture.
SEUIL_ALERTE = 0.80


@dataclass(frozen=True)
class Formule:
    id: str  # stocké en base (tenants.plan) — ne jamais renommer un id livré
    label: str  # nom commercial affiché
    prix_mensuel_eur: int  # 0 = sans abonnement
    minutes_incluses: int  # par mois, tous établissements confondus ; 0 = sans forfait
    # La minute au-delà du forfait — ou chaque minute, quand il n'y a pas de forfait.
    # Propre à chaque formule : elle baisse quand on monte, c'est ce qui rend la formule
    # du dessus intéressante avant d'avoir doublé sa facture.
    minute_supp_eur: float
    etablissements_inclus: int
    argument: str  # à qui elle s'adresse, en une ligne
    mise_en_service_eur: int = 0  # une fois, par numéro

    @property
    def sans_forfait(self) -> bool:
        """Rien d'inclus : il n'y a ni plafond ni alerte, seulement des minutes facturées."""
        return self.minutes_incluses == 0

    @property
    def tarif_implicite_eur(self) -> float:
        """Ce que coûte une minute incluse — ou la minute tout court, sans forfait. Sert
        à vérifier que la grille ne s'inverse pas : payer plus cher par mois doit faire
        baisser le prix de la minute."""
        if self.sans_forfait:
            return self.minute_supp_eur
        return self.prix_mensuel_eur / self.minutes_incluses


CATALOGUE: tuple[Formule, ...] = (
    Formule(
        id="liberte",
        label="Liberté",
        prix_mensuel_eur=0,
        minutes_incluses=0,
        minute_supp_eur=0.45,
        etablissements_inclus=1,
        argument="Sans abonnement : vous ne payez que les minutes d'appel.",
        mise_en_service_eur=10,
    ),
    Formule(
        id="essentiel",
        label="Essentiel",
        prix_mensuel_eur=89,
        minutes_incluses=250,
        minute_supp_eur=0.35,
        etablissements_inclus=1,
        argument="Un restaurant qui rate ses appels au coup de feu.",
    ),
    Formule(
        id="service",
        label="Service",
        prix_mensuel_eur=149,
        minutes_incluses=600,
        minute_supp_eur=0.25,
        etablissements_inclus=1,
        argument="Un restaurant qui vit du téléphone, midi et soir.",
    ),
    Formule(
        id="maison",
        label="Maison",
        prix_mensuel_eur=349,
        minutes_incluses=1500,
        minute_supp_eur=0.20,
        etablissements_inclus=5,
        argument="Un groupe ou une enseigne à plusieurs adresses.",
    ),
)

# Formule d'un établissement dont le champ `plan` est vide : le parc actuel n'a pas
# encore de formule attribuée, et refuser de servir un appel faute de formule serait
# une panne créée par la facturation.
DEFAUT = "essentiel"


def catalogue() -> tuple[Formule, ...]:
    return CATALOGUE


def get(plan_id: Optional[str]) -> Optional[Formule]:
    """La formule du catalogue, ou None si l'identifiant n'y figure pas."""
    return next((f for f in CATALOGUE if f.id == plan_id), None)


def defaut() -> Formule:
    formule = get(DEFAUT)
    if formule is None:  # pragma: no cover — DEFAUT est un id du catalogue
        raise RuntimeError(f"DEFAUT={DEFAUT!r} ne figure pas au catalogue")
    return formule


def resolve(tenant=None) -> Formule:
    """Formule RÉELLEMENT appliquée à cet établissement.

    Hors catalogue (formule retirée depuis, base éditée à la main) -> formule par
    défaut. Comme pour les voix, c'est le seul endroit qui décide : le plafond, la
    facture et l'affichage doivent lire la même chose, sinon un client se voit
    refuser un appel au nom d'un plafond que sa facture ne mentionne pas."""
    choisie = getattr(tenant, "plan", None)
    return get(choisie) or defaut()


def cout_depassement_eur(formule: Formule, minutes_hors_forfait: float) -> float:
    """Montant facturé pour les minutes au-delà du forfait, au tarif de la formule.
    Jamais négatif : un établissement sous son forfait ne génère pas d'avoir."""
    return round(max(0.0, minutes_hors_forfait) * formule.minute_supp_eur, 2)
