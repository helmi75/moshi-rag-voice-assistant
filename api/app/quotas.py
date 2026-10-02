"""Consommation mensuelle et forfait par établissement (#31, ASSISTANTE-120).

Le forfait est un **argument de vente assumé** et la seule protection contre
l'établissement dont le téléphone ne s'arrête pas et qui mange la marge de tous les autres.

**Il ne coupe JAMAIS la ligne.** Décision produit du 30/08/2026 : un restaurant qui perd
ses réservations un samedi soir résilie le lundi, et la minute facturée au-delà du
forfait rapporte bien plus que l'appel refusé n'économise (0,20 à 0,45 € encaissés contre
2,5 c€ de coût, relevé du 28/09/2026). Le forfait compte, prévient et facture — il ne
bloque pas. Ce module n'expose donc aucune fonction capable de refuser un appel : la
tentation ne doit même pas exister dans l'API.

**Il se compte en minutes, à la seconde** (grille du 02/10/2026) : la somme des durées
des appels, pas un nombre d'appels arrondis chacun à la minute entamée. Un « vous êtes
ouverts ce soir ? » de quarante secondes coûte quarante secondes.

Le mois est le mois **calendaire**, parce que c'est la maille de facturation. Un mois
glissant donnerait un plafond que le client ne saurait pas lire sur sa facture. Et c'est
le mois du RESTAURANT (horloge.debut_du_mois) : à 00 h 30 le 1er, heure de Paris, SQLite
en UTC croyait encore au mois précédent, et l'appel changeait de facture.
"""
from dataclasses import dataclass

from . import db, horloge, plans

OK = "ok"
ALERTE = "alerte"
DEPASSEMENT = "depassement"

# Ce qui se décompte : les appels du mois que l'assistante a servis. Un appel qu'elle a
# rendu parce qu'elle était en panne (`secours_motif`, ASSISTANTE-118) dure le temps que
# le restaurant passe à son propre téléphone : le lui facturer ferait payer notre panne.
_DECOMPTES = "started_at >= ? AND secours_motif IS NULL"


@dataclass(frozen=True)
class Consommation:
    """État de consommation d'un établissement pour le mois en cours."""
    formule: plans.Formule
    appels: int  # les appels décomptés, pour information : on facture des minutes
    minutes: float  # à la seconde ; jamais arrondie ici, sinon la facture dérive
    inclus: int  # minutes du forfait ; 0 = formule sans forfait
    hors_forfait: float  # minutes au-delà du forfait — toutes, sans forfait
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

    @property
    def minutes_affichables(self) -> int:
        return round(self.minutes)

    @property
    def hors_forfait_affichable(self) -> int:
        """Vingt secondes au-delà du forfait ne s'affichent pas « 0 minute » à côté d'un
        montant : dès qu'il y a quelque chose à facturer, on écrit au moins 1."""
        return max(1, round(self.hors_forfait)) if self.hors_forfait > 0 else 0


def _du_mois(tenant_id: int) -> tuple[int, float]:
    """Appels et secondes décomptés ce mois-ci pour cet établissement.

    On compte les appels ENTRÉS (une ligne dans `calls`), pas ceux qui ont abouti : un
    appel où l'assistante a répondu consomme la même voix et la même transcription même
    si le client raccroche sans réserver. Un appel encore en cours n'a pas de durée : il
    entrera au compteur à sa clôture.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) FROM calls "
            f"WHERE tenant_id = ? AND {_DECOMPTES}",
            (tenant_id, horloge.debut_du_mois()),
        ).fetchone()
    return row[0], float(row[1])


def _consommation(formule: plans.Formule, appels: int, secondes: float) -> Consommation:
    minutes = max(0.0, secondes) / 60.0
    hors_forfait = max(0.0, minutes - formule.minutes_incluses)
    part = 0.0 if formule.sans_forfait else minutes / formule.minutes_incluses
    if formule.sans_forfait:
        # Rien d'inclus, donc rien à dépasser : chaque minute est facturée, et l'annoncer
        # comme un « plafond dépassé » ferait peur à un client qui paie ce qu'il a choisi.
        niveau = OK
    elif hors_forfait:
        niveau = DEPASSEMENT
    elif part >= plans.SEUIL_ALERTE:
        niveau = ALERTE
    else:
        niveau = OK
    return Consommation(
        formule=formule,
        appels=appels,
        minutes=minutes,
        inclus=formule.minutes_incluses,
        hors_forfait=hors_forfait,
        montant_eur=plans.cout_depassement_eur(formule, hors_forfait),
        part=part,
        niveau=niveau,
        groupement_manquant=formule.etablissements_inclus > 1,
    )


def etat(tenant) -> Consommation:
    """Consommation du mois en cours pour cet établissement."""
    return _consommation(plans.resolve(tenant), *_du_mois(tenant.id))


def etat_par_tenant(tenants_liste) -> dict[int, Consommation]:
    """Consommation de tous les établissements, en UNE requête.

    La vue du parc affiche N établissements : une requête par établissement ferait
    N requêtes pour une information que SQLite sait agréger d'un coup.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tenant_id, COUNT(*) AS n, COALESCE(SUM(duration_seconds), 0) AS secondes "
            f"FROM calls WHERE {_DECOMPTES} GROUP BY tenant_id",
            (horloge.debut_du_mois(),),
        ).fetchall()
    par_id = {row["tenant_id"]: (row["n"], float(row["secondes"])) for row in rows}
    return {
        t.id: _consommation(plans.resolve(t), *par_id.get(t.id, (0, 0.0)))
        for t in tenants_liste
    }


def alertes(tenants_liste) -> list[dict]:
    """Alertes de forfait, au format attendu par la vue du parc.

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
                "title": f"{tenant.name} · forfait dépassé de {c.hors_forfait_affichable} minute(s)",
                "detail": f"Formule {c.formule.label} : {c.minutes_affichables} minutes ce "
                          f"mois-ci pour {c.inclus} incluses. À facturer : "
                          f"{c.montant_eur:.2f} € ({c.formule.minute_supp_eur:.2f} € la "
                          "minute). La ligne n'a pas été coupée — c'est délibéré.",
            })
        elif c.niveau == ALERTE:
            resultat.append({
                "level": "info",
                "title": f"{tenant.name} · {c.part_affichable} % du forfait consommé",
                "detail": f"{c.minutes_affichables} minutes sur {c.inclus} incluses en "
                          f"formule {c.formule.label}. Au-delà, chaque minute sera "
                          f"facturée {c.formule.minute_supp_eur:.2f} €.",
            })
    return resultat


# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ LIMITE CONNUE — les formules multi-établissements ne sont pas applicables
#
# « Maison » vend 1 500 minutes pour CINQ établissements. Or rien, dans le modèle de
# données, ne regroupe cinq établissements sous un même client : `tenants` est une liste
# plate, et un compte client n'existe pas. Le compteur ci-dessus est donc PAR
# établissement — ce qui, sur une formule Maison, accorderait 1 500 minutes à chacun des
# cinq, soit cinq fois le forfait vendu.
#
# On ne bricole pas une règle de répartition ici (diviser par cinq serait inventé, et
# personne ne l'a vendue). `Consommation.groupement_manquant` porte le fait, l'admin
# l'affiche, et la formule Maison ne doit pas être vendue avant qu'un vrai regroupement
# existe. Mieux vaut une formule invendable qu'un plafond faux appliqué en silence.
# ─────────────────────────────────────────────────────────────────────────────
