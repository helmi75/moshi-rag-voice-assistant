"""« Ce que l'IA sait » : les fiches se modifient sur place (SCRUM-111), les horaires y
vivent (SCRUM-110), et la fiche « Établissement » ne touche plus ni à la base ni à
l'accueil.

Ce qui compte : le texte envoyé au prompt reste le même format `## Titre`, une
modification concurrente n'écrase rien en silence, et un restaurateur ne modifie que son
établissement."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import tenants, users
from app.admin.routes_connaissances import _version
from app.main import app

BASE = "## Horaires\nOuvert tous les jours de 12h à 23h.\n\n## Groupes\nJusqu'à 20 couverts.\n"


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Chez Fiches", f"+3365{id(object()) % 10_000_000:07d}",
                                   knowledge_base=BASE)
    compte = users.create_user(f"fiches-{tenant.id}@test.fr", "resto-pass",
                               users.ROLE_RESTAURATEUR, tenant.id)
    yield tenant, compte
    tenants.delete_tenant(tenant.id)


def _client(email="admin@test.local", password="test-admin-pass"):
    client = TestClient(app)
    resp = client.post("/admin/login", data={"email": email, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303
    client.get("/admin/")
    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    client.headers["X-CSRF-Token"] = json.loads(base64.b64decode(raw))["csrf"]
    return client


def _base(tenant) -> str:
    return tenants.get_by_id(tenant.id).knowledge_base


class TestAssemblage:
    def test_aller_retour_sans_perte(self):
        fiches = tenants.parse_knowledge_sections(BASE)
        assert tenants.assembler_fiches(fiches) == BASE

    def test_le_preambule_devient_une_fiche_general(self):
        texte = tenants.assembler_fiches(tenants.parse_knowledge_sections(
            "Restaurant familial.\n\n## Carte\nPizzas."))
        assert texte == "## Général\nRestaurant familial.\n\n## Carte\nPizzas.\n"

    def test_une_base_vide_reste_vide(self):
        assert tenants.assembler_fiches([]) == ""

    def test_un_titre_sur_plusieurs_lignes_est_remis_sur_une(self):
        assert tenants.assembler_fiches([{"title": "Accès\n et parking", "body": "x"}]) == (
            "## Accès et parking\nx\n")


class TestModifierUneFiche:
    def test_la_page_propose_de_modifier_chaque_fiche(self, resto):
        tenant, compte = resto
        page = _client(compte.email, "resto-pass").get(f"/admin/tenants/{tenant.id}/knowledge")
        assert page.status_code == 200
        for n in (0, 1):
            assert f"/admin/tenants/{tenant.id}/knowledge/fiches/{n}/modifier" in page.text
        assert "Ajouter une fiche" in page.text

    def test_le_formulaire_reprend_la_fiche(self, resto):
        tenant, compte = resto
        form = _client(compte.email, "resto-pass").get(
            f"/admin/tenants/{tenant.id}/knowledge/fiches/1/modifier")
        assert 'value="Groupes"' in form.text and "Jusqu&#39;à 20 couverts." in form.text
        assert f'value="{_version(BASE)}"' in form.text

    def test_modifier_ne_touche_qu_a_cette_fiche(self, resto):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches/1",
            data={"titre": "Groupes", "corps": "Jusqu'à 30 couverts, menu unique.",
                  "version": _version(BASE)})
        assert resp.status_code == 200 and "Fiche « Groupes » enregistrée." in resp.text
        assert _base(tenant) == ("## Horaires\nOuvert tous les jours de 12h à 23h.\n\n"
                                 "## Groupes\nJusqu'à 30 couverts, menu unique.\n")

    def test_ajouter_une_fiche(self, resto):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches",
            data={"titre": "Accès", "corps": "Métro Pigalle.", "version": _version(BASE)})
        assert resp.status_code == 200
        assert _base(tenant) == BASE + "\n## Accès\nMétro Pigalle.\n"
        assert [s["title"] for s in tenants.parse_knowledge_sections(_base(tenant))] == [
            "Horaires", "Groupes", "Accès"]

    def test_supprimer_une_fiche(self, resto):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches/0/supprimer",
            data={"version": _version(BASE)})
        assert resp.status_code == 200 and "supprimée" in resp.text
        assert _base(tenant) == "## Groupes\nJusqu'à 20 couverts.\n"


class TestRienNEstEcraseEnSilence:
    def test_une_base_modifiee_entre_temps_est_refusee(self, resto):
        """Deux onglets ouverts : le second enregistrement écraserait le premier."""
        tenant, compte = resto
        client = _client(compte.email, "resto-pass")
        ancienne = _version(BASE)
        assert client.post(f"/admin/tenants/{tenant.id}/knowledge/fiches/0",
                           data={"titre": "Horaires", "corps": "Midi seulement.",
                                 "version": ancienne}).status_code == 200
        apres_premier = _base(tenant)
        resp = client.post(f"/admin/tenants/{tenant.id}/knowledge/fiches/1",
                           data={"titre": "Groupes", "corps": "Pas de groupes.",
                                 "version": ancienne})
        assert resp.status_code == 409 and "modifiée entre-temps" in resp.text
        assert _base(tenant) == apres_premier

    def test_sans_version_rien_n_est_ecrit(self, resto):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches/0/supprimer", data={})
        assert resp.status_code == 409 and _base(tenant) == BASE

    def test_une_fiche_disparue_n_est_pas_recreee(self, resto):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches/7",
            data={"titre": "Fantôme", "corps": "x", "version": _version(BASE)})
        assert resp.status_code == 409 and _base(tenant) == BASE


class TestValidation:
    @pytest.mark.parametrize("titre,corps,attendu", [
        ("", "Du texte.", "titre de la fiche est vide"),
        ("x" * 81, "Du texte.", "une ligne de 80 caractères"),
        ("Carte", "Entrées.\n## Desserts\nTarte.", "commence par « ## »"),
    ])
    def test_une_saisie_qui_casserait_le_decoupage_est_refusee(self, resto, titre, corps,
                                                              attendu):
        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches",
            data={"titre": titre, "corps": corps, "version": _version(BASE)})
        assert resp.status_code == 422 and attendu in resp.text.replace("&#39;", "'")
        assert _base(tenant) == BASE

    def test_la_base_reste_plafonnee(self, resto):
        from app.admin.routes_tenants import KB_MAX

        tenant, compte = resto
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}/knowledge/fiches",
            data={"titre": "Carte", "corps": "x" * KB_MAX, "version": _version(BASE)})
        assert resp.status_code == 422 and _base(tenant) == BASE


class TestUnRestaurateurChezLui:
    def test_ni_lire_ni_modifier_les_fiches_d_un_autre(self, resto):
        tenant, _ = resto
        autre = tenants.create_tenant("Voisin Fiches", f"+3366{id(object()) % 10_000_000:07d}")
        try:
            compte = users.create_user(f"voisin-{autre.id}@test.fr", "resto-pass",
                                       users.ROLE_RESTAURATEUR, autre.id)
            client = _client(compte.email, "resto-pass")
            base = f"/admin/tenants/{tenant.id}/knowledge"
            assert client.get(base).status_code == 403
            assert client.get(f"{base}/fiches/0/modifier").status_code == 403
            for chemin, data in [
                (f"{base}/fiches", {"titre": "Pirate", "corps": "x"}),
                (f"{base}/fiches/0", {"titre": "Pirate", "corps": "x"}),
                (f"{base}/fiches/0/supprimer", {}),
            ]:
                resp = client.post(chemin, data={**data, "version": _version(BASE)})
                assert resp.status_code == 403, chemin
            assert _base(tenant) == BASE
        finally:
            tenants.delete_tenant(autre.id)

    def test_sans_jeton_csrf_rien_n_est_ecrit(self, resto):
        tenant, compte = resto
        client = _client(compte.email, "resto-pass")
        del client.headers["X-CSRF-Token"]
        resp = client.post(f"/admin/tenants/{tenant.id}/knowledge/fiches/0/supprimer",
                           data={"version": _version(BASE)})
        assert resp.status_code == 403 and _base(tenant) == BASE


class TestLaFicheEtablissement:
    """SCRUM-111 : la fiche « Établissement » ne montre plus le grand champ de texte.
    Un formulaire qui n'envoie plus la base ne doit surtout pas l'effacer."""

    def test_plus_de_base_ni_d_accueil_a_la_modification(self, resto):
        tenant, compte = resto
        page = _client(compte.email, "resto-pass").get(f"/admin/tenants/{tenant.id}/edit")
        assert 'name="knowledge_base"' not in page.text and 'name="greeting"' not in page.text
        assert f'href="/admin/tenants/{tenant.id}/knowledge"' in page.text

    def test_enregistrer_l_identite_garde_la_base_et_l_accueil(self, resto):
        tenant, compte = resto
        tenants.update_tenant(tenant.id, greeting="Bonjour, Chez Fiches.")
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}", data={"name": "Chez Fiches bis"},
            follow_redirects=False)
        assert resp.status_code == 303
        apres = tenants.get_by_id(tenant.id)
        assert apres.name == "Chez Fiches bis"
        assert apres.knowledge_base == BASE and apres.greeting == "Bonjour, Chez Fiches."

    def test_un_ancien_formulaire_ne_remplace_pas_la_base(self, resto):
        tenant, compte = resto
        _client(compte.email, "resto-pass").post(
            f"/admin/tenants/{tenant.id}",
            data={"name": "Chez Fiches", "knowledge_base": "", "greeting": ""})
        assert _base(tenant) == BASE

    def test_la_creation_garde_ses_champs(self):
        page = _client().get("/admin/tenants/new")
        assert 'name="knowledge_base"' in page.text and 'name="greeting"' in page.text
