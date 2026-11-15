"""La fiche Google Maps de la démonstration (ASSISTANTE-189).

Aucun réseau : le transport httpx est simulé (`gosom_faux`). On vérifie ce que le module
REFUSE de garder — les avis, leurs auteurs, le propriétaire, les e-mails — autant que ce qu'il
lit, et que la recherche est effacée chez gosom quoi qu'il arrive."""
import asyncio
import json

import pytest
from loguru import logger

from app import disponibilite, fiche_maps
from gosom_faux import CSV, INTERDITS, faux_gosom


@pytest.fixture()
def gosom(monkeypatch):
    monkeypatch.setenv("GOSOM_URL", "http://gosom:8080")
    monkeypatch.setattr(fiche_maps, "PAS_SECONDES", 0)

    def brancher(**options):
        transport, appels = faux_gosom(**options)
        monkeypatch.setattr(fiche_maps, "_TRANSPORT", transport)
        return appels
    return brancher


def chercher(nom="Cuba Compagnie", ville="Paris"):
    return asyncio.run(fiche_maps.chercher(nom, ville))


class TestLaFiche:
    def test_la_fiche_du_vrai_releve_donne_adresse_telephone_site_categorie(self, gosom):
        appels = gosom()
        [fiche] = chercher()
        assert fiche["titre"] == "Cuba Compagnie Café"
        assert fiche["adresse"] == "48 Bd Beaumarchais, 75011 Paris"
        assert fiche["telephone"] == "06 68 19 61 61"
        assert fiche["site"] == "https://www.cubacompagnie.com/"
        assert fiche["categorie"] == "Restaurant cubain"
        assert appels.liste[0] == "POST /api/v1/jobs"

    def test_les_horaires_apres_minuit_sont_acceptes_par_disponibilite(self, gosom):
        gosom()
        [fiche] = chercher()
        charges = disponibilite.charger(fiche["horaires"])
        assert charges is not None and not disponibilite.toujours_ferme(charges)
        for jour in disponibilite.JOURS:
            assert [(d.strftime("%H:%M"), f.strftime("%H:%M")) for d, f in charges["semaine"][jour]] \
                == [("07:00", "02:00")]

    def test_l_accessibilite_ne_garde_que_les_options_actives(self, gosom):
        gosom()
        [fiche] = chercher()
        assert "Entrée accessible en fauteuil roulant" in fiche["acces"]
        assert "Parking accessible" not in fiche["acces"]

    def test_aucune_donnee_de_personne_n_est_gardee(self, gosom):
        """La liste blanche : avis, auteur, propriétaire, e-mail, images ne sortent jamais."""
        gosom()
        [fiche] = chercher()
        assert not any(interdit in json.dumps(fiche, ensure_ascii=False) for interdit in INTERDITS)
        assert set(fiche_maps._lignes(CSV.read_text(encoding="utf-8"))[0]) == {
            "title", "category", "address", "complete_address", "phone", "website",
            "open_hours", "descriptions", "about", "link"}

    def test_rien_n_est_journalise_des_donnees_de_personnes(self, gosom):
        gosom(telechargement=500)   # un échec, pour que le module journalise quelque chose
        journal: list[str] = []
        sink = logger.add(lambda m: journal.append(str(m)), level="DEBUG")
        try:
            with pytest.raises(fiche_maps.MapsIndisponible):
                chercher()
            gosom()
            chercher()
        finally:
            logger.remove(sink)
        assert not any(interdit in ligne for ligne in journal for interdit in INTERDITS)

    def test_inactif_sans_gosom_url(self, monkeypatch):
        monkeypatch.setenv("GOSOM_URL", "")
        assert fiche_maps.actif() is False and chercher() == []


class TestLesHoraires:
    @pytest.mark.parametrize("brut", [
        '{"lundi":["07:00–02:00"],"mardi":["07:00–02:00"],"mercredi":["07:00–02:00"],'
        '"jeudi":["07:00–02:00"],"vendredi":["07:00–02:00"],"samedi":["07:00–02:00"],"dimanche":["07:00-02:00"]}',
    ])
    def test_tiret_long_ou_court(self, brut):
        assert disponibilite.charger(fiche_maps.horaires_de(brut)) is not None

    def test_plusieurs_plages_et_un_jour_ferme(self):
        semaine = {j: ["12:00–14:30", "19:00–22:30"] for j in disponibilite.JOURS}
        semaine["lundi"] = ["Fermé"]
        sortie = json.loads(fiche_maps.horaires_de(json.dumps(semaine)))["semaine"]
        assert sortie["lundi"] == [] and sortie["mardi"] == [["12:00", "14:30"], ["19:00", "22:30"]]

    def test_ouvert_24h_sur_24_laisse_les_horaires_vides(self):
        semaine = {j: ["12:00–14:30"] for j in disponibilite.JOURS} | {"mardi": ["Ouvert 24h/24"]}
        assert fiche_maps.horaires_de(json.dumps(semaine)) == ""

    def test_une_fin_apres_6_h_avant_le_debut_est_une_faute_pas_un_service_de_nuit(self):
        semaine = {j: ["14:00–12:00"] for j in disponibilite.JOURS}
        assert fiche_maps.horaires_de(json.dumps(semaine)) == ""

    def test_un_jour_absent_ou_un_texte_illisible_laisse_vide(self):
        assert fiche_maps.horaires_de('{"lundi":["12:00–14:00"]}') == ""
        assert fiche_maps.horaires_de("pas du json") == ""
        assert fiche_maps.horaires_de("") == ""


class TestLeService:
    def test_la_recherche_est_effacee_apres_la_lecture(self, gosom):
        appels = gosom(etats=("pending", "working", "ok"))
        chercher()
        assert appels.liste[-1] == "DELETE /api/v1/jobs/job-1"

    def test_la_recherche_est_effacee_meme_quand_le_telechargement_echoue(self, gosom):
        appels = gosom(telechargement=500)
        with pytest.raises(fiche_maps.MapsIndisponible):
            chercher()
        assert "DELETE /api/v1/jobs/job-1" in appels.liste

    def test_la_recherche_est_effacee_meme_quand_elle_echoue_chez_gosom(self, gosom):
        appels = gosom(etats=("working", "failed"))
        with pytest.raises(fiche_maps.MapsIndisponible, match="échoué"):
            chercher()
        assert appels.liste[-1] == "DELETE /api/v1/jobs/job-1"

    def test_un_delai_depasse_est_une_erreur_claire_et_la_recherche_est_effacee(self, gosom, monkeypatch):
        monkeypatch.setattr(fiche_maps, "DELAI_TOTAL_SECONDES", 0.05)
        monkeypatch.setattr(fiche_maps, "PAS_SECONDES", 0.01)
        appels = gosom(etats=("working",))
        with pytest.raises(fiche_maps.MapsIndisponible, match="à temps"):
            chercher()
        assert appels.liste[-1] == "DELETE /api/v1/jobs/job-1"

    def test_un_csv_trop_gros_est_refuse(self, gosom, monkeypatch):
        monkeypatch.setattr(fiche_maps, "TAILLE_MAX", 100)
        gosom()
        with pytest.raises(fiche_maps.MapsIndisponible, match="volumineuse"):
            chercher()

    def test_un_service_injoignable_est_une_erreur_claire(self, gosom, monkeypatch):
        import httpx

        def panne(requete):
            raise httpx.ConnectError("refusé")
        monkeypatch.setattr(fiche_maps, "_TRANSPORT", httpx.MockTransport(panne))
        with pytest.raises(fiche_maps.MapsIndisponible, match="injoignable"):
            chercher()

    def test_plusieurs_fiches_sont_rendues_dans_la_limite(self, gosom):
        texte = CSV.read_text(encoding="utf-8")
        entete, ligne = texte.split("\n", 1)
        gosom(csv_texte=entete + "\n" + ligne.rstrip("\n") + "\n" + ligne.rstrip("\n") + "\n")
        assert len(chercher()) == 2
