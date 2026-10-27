"""Page « Appels en cours » (ASSISTANTE-164) : le registre, la page, le cloisonnement."""
import pytest
from fastapi.testclient import TestClient

from app import en_cours, tenants, users
from app.main import app

MDP = "Mot-de-passe-test-164"


@pytest.fixture(autouse=True)
def _registre_vide():
    en_cours.reinitialiser()
    yield
    en_cours.reinitialiser()


@pytest.fixture()
def deux_etablissements():
    suffixe = id(object()) % 10_000_000
    a = tenants.create_tenant("Chez Alpha 164", f"+3361{suffixe:07d}")
    b = tenants.create_tenant("Chez Beta 164", f"+3362{suffixe:07d}")
    resto_a = users.create_user(f"a164-{a.id}@test.fr", MDP, users.ROLE_RESTAURATEUR, a.id)
    yield a, b, resto_a
    tenants.delete_tenant(a.id)
    tenants.delete_tenant(b.id)


def _connecte(email="admin@test.local", mdp="test-admin-pass"):
    client = TestClient(app)
    r = client.post("/admin/login", data={"email": email, "password": mdp},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


class TestRegistre:
    def test_un_appel_ouvert_est_liste_en_classique(self):
        en_cours.ouvrir("CA1", 7)
        [e] = en_cours.lister()
        assert (e["tenant_id"], e["moteur"], e["incident"]) == (7, "classique", None)
        assert "caller" not in e and "from_number" not in e

    def test_moteur_et_incident_se_renseignent_puis_l_appel_se_ferme(self):
        en_cours.ouvrir("CA1", 7)
        en_cours.moteur("CA1", "temps_reel")
        en_cours.incident("CA1", "RuntimeError")
        [e] = en_cours.lister()
        assert (e["moteur"], e["incident"]) == ("temps_reel", "RuntimeError")
        en_cours.fermer("CA1")
        assert en_cours.lister() == []

    def test_un_call_sid_absent_ne_s_enregistre_pas(self):
        en_cours.ouvrir(None, 7)
        en_cours.moteur(None, "temps_reel")
        en_cours.incident(None, "x")
        assert en_cours.lister() == []

    def test_fermer_ou_renseigner_un_inconnu_ne_leve_pas(self):
        en_cours.fermer("inconnu")
        en_cours.fermer(None)
        en_cours.moteur("inconnu", "temps_reel")
        en_cours.incident("inconnu", "x")

    def test_la_liste_est_triee_par_debut_et_filtrable(self):
        en_cours.ouvrir("CA1", 1)
        en_cours.ouvrir("CA2", 2)
        en_cours.ouvrir("CA3", 1)
        assert [e["call_sid"] for e in en_cours.lister()] == ["CA1", "CA2", "CA3"]
        assert [e["call_sid"] for e in en_cours.lister(1)] == ["CA1", "CA3"]

    def test_lister_rend_des_copies(self):
        en_cours.ouvrir("CA1", 1)
        en_cours.lister()[0]["moteur"] = "falsifie"
        assert en_cours.lister()[0]["moteur"] == "classique"


class TestPage:
    def test_le_super_admin_voit_les_deux_etablissements(self, deux_etablissements):
        a, b, _ = deux_etablissements
        en_cours.ouvrir("CA-a", a.id)
        en_cours.ouvrir("CA-b", b.id)
        en_cours.moteur("CA-b", "temps_reel")
        en_cours.incident("CA-b", "RuntimeError")
        texte = _connecte().get("/admin/calls/live").text
        assert "Chez Alpha 164" in texte and "Chez Beta 164" in texte
        assert "temps réel" in texte and "classique" in texte
        assert "RuntimeError" in texte
        assert "min" in texte

    def test_le_fragment_se_rafraichit_tout_seul(self, deux_etablissements):
        a, _, _ = deux_etablissements
        en_cours.ouvrir("CA-a", a.id)
        texte = _connecte().get("/admin/calls/live/_liste").text
        assert 'hx-trigger="every 3s"' in texte and "Chez Alpha 164" in texte

    def test_etat_vide(self):
        texte = _connecte().get("/admin/calls/live").text
        assert "Aucun appel en cours" in texte

    def test_aucun_nom_de_fournisseur(self, deux_etablissements):
        a, _, _ = deux_etablissements
        en_cours.ouvrir("CA-a", a.id)
        en_cours.moteur("CA-a", "temps_reel")
        texte = _connecte().get("/admin/calls/live").text.lower()
        assert "gpt-live" not in texte and "openai" not in texte

    def test_sans_session_la_page_n_est_pas_servie(self):
        r = TestClient(app).get("/admin/calls/live", follow_redirects=False)
        assert r.status_code in (303, 307, 401)


class TestCloisonnement:
    def test_un_restaurateur_ne_voit_que_ses_appels(self, deux_etablissements):
        a, b, resto = deux_etablissements
        en_cours.ouvrir("CA-a", a.id)
        en_cours.ouvrir("CA-b", b.id)
        client = _connecte(resto.email, MDP)
        for adresse in ("/admin/calls/live", "/admin/calls/live/_liste"):
            texte = client.get(adresse).text
            assert "Chez Alpha 164" in texte
            assert "Chez Beta 164" not in texte

    def test_un_restaurateur_est_refuse_sur_l_etablissement_d_un_autre(self, deux_etablissements):
        a, b, resto = deux_etablissements
        en_cours.ouvrir("CA-b", b.id)
        client = _connecte(resto.email, MDP)
        for adresse in ("/admin/calls/live", "/admin/calls/live/_liste"):
            r = client.get(f"{adresse}?tenant_id={b.id}")
            assert r.status_code == 403
            assert "Chez Beta 164" not in r.text
