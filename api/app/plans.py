"""Catalogue des formules commerciales — la grille tarifaire, arrêtée (#29).

Liste FERMÉE, pour la même raison que le catalogue de voix : ce qui est vendu doit
être décidé à un seul endroit. Une formule inventée ailleurs dans le code (une chaîne
en base éditée à la main, un import CSV) donnerait un plafond que personne n'a arrêté
et une facture que personne ne sait justifier.

**Les prix sont en euros, les coûts en dollars.** Ce n'est pas une négligence : les
fournisseurs (Twilio, Deepgram, Modal, OpenRouter) facturent en dollars et le produit
se vend en euros. Convertir dans le code figerait un taux de change qui dérive ; la
marge se calcule donc hors du code, dans `docs/TARIFS.md`, avec le taux du jour écrit
noir sur blanc.

Décidé le 30/08/2026 sur des coûts VÉRIFIÉS (tarif Deepgram nova-3 corrigé le même
jour). Marges à la durée d'appel mesurée : 78 % / 67 / 72 %. Voir `docs/TARIFS.md`
pour le raisonnement, les comparables et — surtout — le risque assumé.
"""
from dataclasses import dataclass
from typing import Optional

# Prix d'un appel au-delà du plafond, en euros. 60 % de marge au coût mesuré.
#
# ⚠️ Choix commercial assumé : ce tarif est INFÉRIEUR au tarif implicite d'Essentiel
# (0,59 €/appel). Un client Essentiel a donc intérêt à déborder plutôt qu'à passer en
# Service. On perd de l'upsell, pas de la marge : à 300 appels sur Essentiel, la marge
# reste à 72 %. Monter à 0,50 € inverserait l'incitation si on veut pousser l'upsell.
DEPASSEMENT_EUR = 0.30

# Part du plafond à partir de laquelle on prévient. Prévenir APRÈS coup ne sert à rien :
# le restaurateur découvrirait le dépassement sur sa facture.
SEUIL_ALERTE = 0.80


@dataclass(frozen=True)
class Formule:
    id: str  # stocké en base (tenants.plan) — ne jamais renommer un id livré
    label: str  # nom commercial affiché
    prix_mensuel_eur: int
    appels_inclus: int  # par mois, tous établissements confondus
    etablissements_inclus: int
    argument: str  # à qui elle s'adresse, en une ligne

    @property
    def tarif_implicite_eur(self) -> float:
        """Ce que coûte un appel inclus. Sert à vérifier que le dépassement reste
        cohérent avec la formule — et à repérer une grille qui s'inverse."""
        return self.prix_mensuel_eur / self.appels_inclus if self.appels_inclus else 0.0


CATALOGUE: tuple[Formule, ...] = (
    Formule(
        id="essentiel",
        label="Essentiel",
        prix_mensuel_eur=89,
        appels_inclus=150,
        etablissements_inclus=1,
        argument="Un restaurant qui rate ses appels au coup de feu.",
    ),
    Formule(
        id="service",
        label="Service",
        prix_mensuel_eur=149,
        appels_inclus=400,
        etablissements_inclus=1,
        argument="Un restaurant qui vit du téléphone, midi et soir.",
    ),
    Formule(
        id="maison",
        label="Maison",
        prix_mensuel_eur=349,
        appels_inclus=750,
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


def cout_depassement_eur(appels_hors_forfait: int) -> float:
    """Montant facturé pour les appels au-delà du plafond. Jamais négatif : un
    établissement sous son plafond ne génère pas d'avoir."""
    return round(max(0, appels_hors_forfait) * DEPASSEMENT_EUR, 2)
