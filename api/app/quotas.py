"""Consommation mensuelle et plafond par établissement (#31).

Le plafond est un **argument de vente assumé** et la seule protection contre
l'établissement à 800 appels par mois qui mange la marge GPU de tous les autres.

**Il ne coupe JAMAIS la ligne.** Décision produit du 30/08/2026 : un restaurant qui perd
ses réservations un samedi soir résilie le lundi, et le dépassement facturé rapporte
davantage que l'appel refusé n'économise (0,30 € encaissés contre 0,12 € de coût). Le
plafond compte, prévient et facture — il ne bloque pas. Ce module n'expose donc aucune
fonction capable de refuser un appel : la tentation ne doit même pas exister dans l'API.

Le mois est le mois **calendaire**, parce que c'est la maille de facturation. Un mois
glissant donnerait un plafond que le client ne saurait pas lire sur sa facture.
"""
from dataclasses import dataclass
from typing import Optional

from . import db, plans

OK = "ok"
ALERTE = "alerte"
DEPASSEMENT = "depassement"


@dataclass(frozen=True)
class Consommation:
    """État de consommation d'un établissement pour le mois en cours."""
    formule: plans.Formule
    appels: int
    inclus: int
    hors_forfait: int
    montant_eur: float
    part: float  # 0.0 → 1.0 et au-delà ; jamais borné, sinon on masque l'ampleur
    niveau: str
    # Vrai quand la formule couvre plusieurs établissements alors que rien, dans le
    # modèle de données, ne les regroupe. Voir la note en bas de fichier.
    groupement_manquant: bool

    @property
    def part_affichable(self) -> int:
        """Pourcentage borné à 100 pour une barre de progression — la barre ne peut pas
        dépasser sa largeur, mais `part` reste exacte pour le texte à côté."""
        return min(100, round(100 * self.part))


def _appels_du_mois(tenant_id: int) -> int:
    """Appels du mois calendaire en cours pour cet établissement.

    On compte les appels ENTRÉS (une ligne dans `calls`), pas ceux qui ont abouti : un
    appel où l'assistante a répondu consomme du GPU même si le client raccroche sans
    réserver. Facturer autrement reviendrait à offrir les appels ratés, qui coûtent
    exactement le même prix.
    """
    with db.get_conn() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM calls
               WHERE tenant_id = ?
                 AND strftime('%Y-%m', started_at) = strftime('%Y-%m', 'now')""",
            (tenant_id,),
        ).fetchone()[0]


def _consommation(formule: plans.Formule, appels: int) -> Consommation:
    hors_forfait = max(0, appels - formule.appels_inclus)
    part = appels / formule.appels_inclus if formule.appels_inclus else 0.0
    if hors_forfait:
        niveau = DEPASSEMENT
    elif part >= plans.SEUIL_ALERTE:
        niveau = ALERTE
    else:
        niveau = OK
    return Consommation(
        formule=formule,
        appels=appels,
        inclus=formule.appels_inclus,
        hors_forfait=hors_forfait,
        montant_eur=plans.cout_depassement_eur(hors_forfait),
        part=part,
        niveau=niveau,
        groupement_manquant=formule.etablissements_inclus > 1,
    )


def etat(tenant) -> Consommation:
    """Consommation du mois en cours pour cet établissement."""
    return _consommation(plans.resolve(tenant), _appels_du_mois(tenant.id))


def etat_par_tenant(tenants_liste) -> dict[int, Consommation]:
    """Consommation de tous les établissements, en UNE requête.

    La vue du parc affiche N établissements : une requête par établissement ferait
    N requêtes pour une information que SQLite sait agréger d'un coup.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT tenant_id, COUNT(*) AS n FROM calls
               WHERE strftime('%Y-%m', started_at) = strftime('%Y-%m', 'now')
               GROUP BY tenant_id"""
        ).fetchall()
    par_id = {row["tenant_id"]: row["n"] for row in rows}
    return {
        t.id: _consommation(plans.resolve(t), par_id.get(t.id, 0))
        for t in tenants_liste
    }


def alertes(tenants_liste) -> list[dict]:
    """Alertes de quota, au format attendu par la vue du parc.

    Prévenir APRÈS le dépassement ne prévient de rien : le restaurateur découvrirait la
    facture. D'où le seuil à 80 %, franchi avant que l'argent ne soit engagé.
    """
    etats = etat_par_tenant(tenants_liste)
    resultat = []
    for tenant in tenants_liste:
        c = etats[tenant.id]
        if c.niveau == DEPASSEMENT:
            resultat.append({
                "level": "warn",
                "title": f"{tenant.name} · plafond dépassé de {c.hors_forfait} appel(s)",
                "detail": f"Formule {c.formule.label} : {c.appels} appels ce mois-ci pour "
                          f"{c.inclus} inclus. À facturer : {c.montant_eur:.2f} € "
                          f"({plans.DEPASSEMENT_EUR:.2f} € par appel). La ligne n'a pas "
                          "été coupée — c'est délibéré.",
            })
        elif c.niveau == ALERTE:
            resultat.append({
                "level": "info",
                "title": f"{tenant.name} · {c.part_affichable} % du forfait consommé",
                "detail": f"{c.appels} appels sur {c.inclus} inclus en formule "
                          f"{c.formule.label}. Au-delà, chaque appel sera facturé "
                          f"{plans.DEPASSEMENT_EUR:.2f} €.",
            })
    return resultat


# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ LIMITE CONNUE — les formules multi-établissements ne sont pas applicables
#
# « Maison » vend 750 appels pour CINQ établissements. Or rien, dans le modèle de
# données, ne regroupe cinq établissements sous un même client : `tenants` est une liste
# plate, et un compte client n'existe pas. Le compteur ci-dessus est donc PAR
# établissement — ce qui, sur une formule Maison, accorderait 750 appels à chacun des
# cinq, soit cinq fois le forfait vendu.
#
# On ne bricole pas une règle de répartition ici (diviser par cinq serait inventé, et
# personne ne l'a vendue). `Consommation.groupement_manquant` porte le fait, l'admin
# l'affiche, et la formule Maison ne doit pas être vendue avant qu'un vrai regroupement
# existe. Mieux vaut une formule invendable qu'un plafond faux appliqué en silence.
# ─────────────────────────────────────────────────────────────────────────────
