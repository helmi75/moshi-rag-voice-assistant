"""Grille tarifaire (#29) : ce qui est vendu doit être décidé à un seul endroit.

Ces tests ne vérifient pas « les prix sont ceux-ci » — un prix change, et un test qui
recopie la grille ne fait que la répéter. Ils vérifient les propriétés qui, si elles
cassaient, feraient vendre quelque chose que personne n'a arrêté : catalogue fermé,
repli sûr, cohérence interne de la grille.
"""
import pytest

from app import plans


class TestCatalogue:
    def test_le_catalogue_n_est_pas_vide(self):
        """Filet de sécurité : un catalogue vidé par mégarde ferait passer tous les
        autres tests au vert sans rien vérifier."""
        assert len(plans.catalogue()) >= 3

    def test_les_identifiants_sont_uniques(self):
        ids = [f.id for f in plans.catalogue()]
        assert len(ids) == len(set(ids))

    def test_chaque_formule_est_vendable(self):
        for f in plans.catalogue():
            assert f.prix_mensuel_eur > 0, f.id
            assert f.appels_inclus > 0, f.id
            assert f.etablissements_inclus >= 1, f.id
            assert f.label and f.argument, f.id

    def test_aucune_formule_illimitee(self):
        """« Illimité » est la seule façon sûre de perdre de l'argent sur un client :
        le coût GPU est linéaire, pas le prix. Décision produit, pas frilosité."""
        for f in plans.catalogue():
            assert f.appels_inclus < 10_000, f"{f.id} ressemble à de l'illimité"


class TestCoherenceDeLaGrille:
    """Une grille peut être arithmétiquement valide et commercialement absurde."""

    def test_monter_de_formule_donne_plus_d_appels(self):
        formules = sorted(plans.catalogue(), key=lambda f: f.prix_mensuel_eur)
        for precedente, suivante in zip(formules, formules[1:]):
            assert suivante.appels_inclus > precedente.appels_inclus, (
                f"{suivante.id} coûte plus cher que {precedente.id} sans donner "
                "plus d'appels")

    def test_le_tarif_implicite_baisse_a_nombre_d_etablissements_egal(self):
        """Payer plus cher doit faire baisser le prix à l'appel — sinon la formule
        supérieure n'a aucun sens pour le client.

        La comparaison n'a de sens qu'à nombre d'établissements ÉGAL : Maison affiche
        un tarif implicite plus élevé que Service (0,47 € contre 0,37 €) parce qu'elle
        inclut cinq numéros et cinq établissements. Comparer les deux reviendrait à
        reprocher à un abonnement familial d'être plus cher par personne qu'un forfait
        solo à gros volume. On compare donc par groupe."""
        par_taille: dict[int, list] = {}
        for f in plans.catalogue():
            par_taille.setdefault(f.etablissements_inclus, []).append(f)
        compares = 0
        for formules in par_taille.values():
            formules.sort(key=lambda f: f.prix_mensuel_eur)
            for precedente, suivante in zip(formules, formules[1:]):
                assert suivante.tarif_implicite_eur < precedente.tarif_implicite_eur, (
                    f"{suivante.id} coûte plus cher à l'appel que {precedente.id}")
                compares += 1
        assert compares >= 1, "aucune paire comparable : le test ne vérifie rien"

    def test_le_depassement_est_rentable(self):
        """Le coût mesuré d'un appel est de 12,1 c€ à 3,5 min (docs/TARIFS.md).
        Facturer en dessous reviendrait à payer pour servir."""
        cout_mesure_eur = 0.121
        assert plans.DEPASSEMENT_EUR > cout_mesure_eur * 2, (
            "moins de 50 % de marge sur le dépassement : une pointe d'activité "
            "coûterait plus cher qu'elle ne rapporte")

    def test_l_alerte_precede_le_plafond(self):
        """Prévenir à 100 % ne prévient de rien : le dépassement est déjà là."""
        assert 0.5 <= plans.SEUIL_ALERTE < 1.0


class TestResolution:
    """Même règle que le catalogue de voix : une valeur hors catalogue ne doit jamais
    sortir d'ici, sinon un client se voit appliquer un plafond que personne n'a vendu."""

    class _Tenant:
        def __init__(self, plan):
            self.plan = plan

    def test_une_formule_du_catalogue_est_rendue(self):
        assert plans.resolve(self._Tenant("service")).id == "service"

    @pytest.mark.parametrize("valeur", [None, "", "premium", "ESSENTIEL", "essentiel "])
    def test_hors_catalogue_replie_sur_le_defaut(self, valeur):
        assert plans.resolve(self._Tenant(valeur)).id == plans.DEFAUT

    def test_sans_etablissement_du_tout(self):
        """Le chemin d'appel peut résoudre une formule avant d'avoir un tenant."""
        assert plans.resolve(None).id == plans.DEFAUT

    def test_le_defaut_figure_bien_au_catalogue(self):
        assert plans.get(plans.DEFAUT) is not None


class TestDepassement:
    def test_facture_proportionnelle(self):
        assert plans.cout_depassement_eur(10) == round(10 * plans.DEPASSEMENT_EUR, 2)

    @pytest.mark.parametrize("appels", [0, -5])
    def test_sous_le_plafond_aucun_avoir(self, appels):
        """Un établissement qui consomme moins que son forfait ne génère pas de
        crédit : sinon un mois creux ferait une facture négative."""
        assert plans.cout_depassement_eur(appels) == 0.0
