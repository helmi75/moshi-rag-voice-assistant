"""Grille tarifaire (#29, ASSISTANTE-120) : ce qui est vendu doit être décidé à un seul endroit.

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
            assert f.minute_supp_eur > 0, f.id
            assert f.etablissements_inclus >= 1, f.id
            assert f.label and f.argument, f.id
            if f.sans_forfait:
                # Sans forfait, pas d'abonnement non plus : sinon on vendrait du vide.
                assert f.prix_mensuel_eur == 0, f.id
            else:
                assert f.prix_mensuel_eur > 0 and f.minutes_incluses > 0, f.id

    def test_aucune_formule_illimitee(self):
        """« Illimité » est la seule façon sûre de perdre de l'argent sur un client :
        le coût d'une minute est linéaire, pas le prix. Décision produit, pas frilosité."""
        for f in plans.catalogue():
            assert f.minutes_incluses < 10_000, f"{f.id} ressemble à de l'illimité"


class TestCoherenceDeLaGrille:
    """Une grille peut être arithmétiquement valide et commercialement absurde."""

    def test_monter_de_formule_donne_plus_de_minutes(self):
        formules = sorted(plans.catalogue(), key=lambda f: f.prix_mensuel_eur)
        for precedente, suivante in zip(formules, formules[1:]):
            assert suivante.minutes_incluses > precedente.minutes_incluses, (
                f"{suivante.id} coûte plus cher que {precedente.id} sans donner "
                "plus de minutes")

    def test_le_tarif_implicite_baisse_a_nombre_d_etablissements_egal(self):
        """Payer plus cher par mois doit faire baisser le prix de la minute — sinon la
        formule supérieure n'a aucun sens pour le client. Sans forfait, la minute est
        celle qu'on facture : c'est la plus chère de toutes.

        La comparaison n'a de sens qu'à nombre d'établissements ÉGAL : Maison inclut
        cinq numéros et cinq établissements. La comparer à Service reviendrait à
        reprocher à un abonnement familial son prix par personne. On compare donc par
        groupe."""
        par_taille: dict[int, list] = {}
        for f in plans.catalogue():
            par_taille.setdefault(f.etablissements_inclus, []).append(f)
        compares = 0
        for formules in par_taille.values():
            formules.sort(key=lambda f: f.prix_mensuel_eur)
            for precedente, suivante in zip(formules, formules[1:]):
                assert suivante.tarif_implicite_eur < precedente.tarif_implicite_eur, (
                    f"{suivante.id} coûte plus cher à la minute que {precedente.id}")
                compares += 1
        assert compares >= 2, "trop peu de paires comparables : le test ne vérifie rien"

    def test_la_minute_en_plus_baisse_quand_on_monte(self):
        formules = sorted(plans.catalogue(), key=lambda f: f.prix_mensuel_eur)
        for precedente, suivante in zip(formules, formules[1:]):
            assert suivante.minute_supp_eur < precedente.minute_supp_eur, (
                f"la minute en plus de {suivante.id} n'est pas moins chère que celle "
                f"de {precedente.id}")

    def test_l_abonnement_bat_la_minute_seule_quand_on_use_son_forfait(self):
        """Un client qui consomme tout son forfait ne doit pas payer moins sans
        abonnement : sinon personne n'a de raison de s'abonner."""
        sans = [f for f in plans.catalogue() if f.sans_forfait]
        avec = [f for f in plans.catalogue() if not f.sans_forfait]
        assert sans and avec
        for libre in sans:
            for f in avec:
                assert f.minutes_incluses * libre.minute_supp_eur > f.prix_mensuel_eur, (
                    f"{f.minutes_incluses} minutes coûtent moins cher en {libre.id} "
                    f"qu'en {f.id}")

    def test_chaque_minute_facturee_est_rentable(self):
        """Une minute coûte environ 2,5 c€ (docs/TARIFS.md, relevé du 28/09/2026). On
        garde une borne haute de 5 c€ : une minute vendue moins du double ferait d'une
        pointe d'activité une perte."""
        cout_borne_haute_eur = 0.05
        for f in plans.catalogue():
            assert f.minute_supp_eur > cout_borne_haute_eur * 2, f.id
            assert f.tarif_implicite_eur > cout_borne_haute_eur * 2, f.id

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
    def test_facture_proportionnelle_au_tarif_de_la_formule(self):
        for f in plans.catalogue():
            assert plans.cout_depassement_eur(f, 10) == round(10 * f.minute_supp_eur, 2)

    def test_les_secondes_comptent(self):
        service = plans.get("service")
        assert plans.cout_depassement_eur(service, 0.5) == round(service.minute_supp_eur / 2, 2)

    @pytest.mark.parametrize("minutes", [0, -5, -0.5])
    def test_sous_le_plafond_aucun_avoir(self, minutes):
        """Un établissement qui consomme moins que son forfait ne génère pas de
        crédit : sinon un mois creux ferait une facture négative."""
        for f in plans.catalogue():
            assert plans.cout_depassement_eur(f, minutes) == 0.0


class TestEquivalentEnAppels:
    """Un ordre de grandeur pour le restaurateur, pas une mesure : arrondi à la dizaine."""

    def test_la_grille_en_appels(self):
        assert plans.DUREE_APPEL_MIN == 3.5  # docs/TARIFS.md : 24 appels d'essai
        assert [plans.appels_equivalents(f.minutes_incluses)
                for f in plans.catalogue() if not f.sans_forfait] == [70, 170, 430]

    @pytest.mark.parametrize("minutes", [100, 250, 333, 600, 1500])
    def test_toujours_une_dizaine_ronde(self, minutes):
        assert plans.appels_equivalents(minutes) % 10 == 0

    def test_sans_minute_aucun_appel(self):
        assert plans.appels_equivalents(0) == 0


class TestSansForfait:
    def test_la_formule_sans_abonnement_existe_et_n_inclut_rien(self):
        liberte = plans.get("liberte")
        assert liberte is not None and liberte.sans_forfait
        assert liberte.prix_mensuel_eur == 0 and liberte.mise_en_service_eur > 0

    def test_elle_n_est_pas_la_formule_par_defaut(self):
        """Un établissement sans formule attribuée reste sur un forfait : lui appliquer
        la minute seule le facturerait sans que personne le lui ait vendu."""
        assert not plans.defaut().sans_forfait
