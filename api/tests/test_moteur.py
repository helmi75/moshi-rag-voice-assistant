"""Le moteur de l'appel se choisit par établissement, dans l'admin (05/10/2026).

Demande de Helmi après dix vrais appels sur GPT-Live : « dans le super admin je choisis
GPT-Live ou l'ancienne version pour chaque restaurateur ; en général GPT-Live par défaut
et l'ancienne version en secours ». Jusque-là, seul le .env du serveur
(`GPT_LIVE_ETABLISSEMENTS`) disait qui passait par GPT-Live : il fallait redéployer.

Ce fichier tient deux choses :

1. **le choix est réservé au super-admin** — GPT-Live coûte le double à la minute et envoie
   la voix des clients chez OpenAI : un restaurateur ne se l'attribue pas, et ne le
   retire pas non plus ;
2. **l'admin dit ce que chaque moteur coûte**, mesuré sur les appels qu'il a servis, et la
   voix de GPT-Live n'est plus rangée sous « Voix Mistral ».

L'aiguillage lui-même et le repli sur la chaîne classique sont dans `test_live.py`.
"""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import calls, db, tenants, users
from app.main import app
from app.voice import live


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cle(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-jamais-utilisee")
    monkeypatch.delenv("GPT_LIVE_ETABLISSEMENTS", raising=False)
    monkeypatch.delenv("GPT_LIVE_MODELE", raising=False)


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Chez Moteur", "+33199000771")
    yield tenant
    tenants.delete_tenant(tenant.id)


@pytest.fixture()
def voisin():
    tenant = tenants.create_tenant("Chez Voisin", "+33199000772")
    yield tenant
    tenants.delete_tenant(tenant.id)


def _csrf(client) -> str:
    charge = client.cookies.get("session").split(".")[0]
    charge += "=" * (-len(charge) % 4)
    return json.loads(base64.urlsafe_b64decode(charge))["csrf"]


def _admin(client) -> None:
    assert client.post("/admin/login", data={"email": "admin@test.local",
                                             "password": "test-admin-pass"},
                       follow_redirects=False).status_code == 303


def _restaurateur(client, tenant) -> None:
    user = users.create_user(f"gerant-{tenant.id}@test.fr", "resto-pass-moteur",
                             users.ROLE_RESTAURATEUR, tenant.id)
    assert client.post("/admin/login", data={"email": user.email,
                                             "password": "resto-pass-moteur"},
                       follow_redirects=False).status_code == 303


def _fiche(tenant, **champs) -> dict:
    return {"name": tenant.name, "business_type": "restaurant", "language": "fr-FR", **champs}


def _page(reponse) -> str:
    assert reponse.status_code == 200
    return reponse.text.replace("&#39;", "'")


def _selectionne(page: str) -> str:
    """La valeur présélectionnée du menu « Qui répond au téléphone »."""
    menu = page[page.index('name="moteur_voix"'):]
    menu = menu[:menu.index("</select>")]
    choisies = [option for option in menu.split("<option")[1:] if "selected" in option.split(">")[0]]
    assert len(choisies) == 1, menu
    return choisies[0].split('value="')[1].split('"')[0]


def _appel(tenant_id: int, sid: str, voix: str, secondes: float, *, telephonie: float,
           transcription: float, comprehension: float, cout_voix: float) -> None:
    total = telephonie + transcription + comprehension + cout_voix
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, started_at, ended_at, duration_seconds,
                                  status, estimated_cost, cout_telephonie, cout_transcription,
                                  cout_comprehension, cout_voix, voix_fournisseur)
               VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                       strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, 'completed', ?, ?, ?, ?, ?, ?)""",
            (sid, tenant_id, secondes, total, telephonie, transcription, comprehension,
             cout_voix, voix))


class TestLaColonne:
    def test_un_etablissement_nait_sans_choix_et_reste_sur_la_chaine_classique(self, resto):
        assert resto.moteur_voix is None
        assert live.moteur(resto) == live.CLASSIQUE and live.actif(resto) is False

    def test_le_choix_se_range_et_se_relit(self, resto):
        assert tenants.update_tenant(resto.id, moteur_voix="gpt_live").moteur_voix == "gpt_live"
        assert live.actif(tenants.get_by_id(resto.id)) is True
        assert tenants.update_tenant(resto.id, moteur_voix="classique").moteur_voix == "classique"
        assert live.actif(tenants.get_by_phone(resto.phone_number)) is False


class TestLeChoixEstReserveAuSuperAdmin:
    """Le champ voyage dans le même formulaire que ceux qu'un restaurateur peut modifier :
    cacher le menu dans le gabarit ne suffit pas, la route doit refuser le champ."""

    def test_un_restaurateur_ne_peut_pas_s_attribuer_gpt_live(self, client, resto):
        _restaurateur(client, resto)
        reponse = client.post(f"/admin/tenants/{resto.id}", data=_fiche(resto, moteur_voix="gpt_live"),
                              headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert reponse.status_code == 303      # la fiche s'enregistre…
        assert tenants.get_by_id(resto.id).moteur_voix is None      # …sans le champ réservé

    def test_un_restaurateur_ne_peut_pas_non_plus_le_retirer(self, client, resto):
        tenants.update_tenant(resto.id, moteur_voix="gpt_live")
        _restaurateur(client, resto)
        client.post(f"/admin/tenants/{resto.id}", data=_fiche(resto, moteur_voix="classique"),
                    headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert tenants.get_by_id(resto.id).moteur_voix == "gpt_live"

    def test_le_super_admin_le_peut(self, client, resto):
        """Contre-épreuve : sans elle, les deux tests ci-dessus passeraient même si la route
        ignorait le champ pour tout le monde."""
        _admin(client)
        for moteur in ("gpt_live", "classique"):
            reponse = client.post(
                f"/admin/tenants/{resto.id}",
                data=_fiche(resto, phone_number=resto.phone_number, moteur_voix=moteur),
                headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
            assert reponse.status_code == 303
            assert tenants.get_by_id(resto.id).moteur_voix == moteur

    def test_un_moteur_invente_n_est_jamais_ecrit(self, client, resto):
        _admin(client)
        client.post(f"/admin/tenants/{resto.id}",
                    data=_fiche(resto, phone_number=resto.phone_number, moteur_voix="le-mien"),
                    headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert tenants.get_by_id(resto.id).moteur_voix is None

    def test_un_formulaire_qui_n_envoie_pas_le_champ_ne_l_efface_pas(self, client, resto):
        tenants.update_tenant(resto.id, moteur_voix="gpt_live")
        _admin(client)
        client.post(f"/admin/tenants/{resto.id}", data=_fiche(resto, phone_number=resto.phone_number),
                    headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert tenants.get_by_id(resto.id).moteur_voix == "gpt_live"

    def test_la_fiche_d_un_autre_etablissement_est_fermee(self, client, resto, voisin):
        _restaurateur(client, resto)
        reponse = client.post(f"/admin/tenants/{voisin.id}",
                              data=_fiche(voisin, moteur_voix="gpt_live"),
                              headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert reponse.status_code == 403
        assert tenants.get_by_id(voisin.id).moteur_voix is None

    def test_a_la_creation_le_moteur_choisi_est_range(self, client):
        _admin(client)
        reponse = client.post(
            "/admin/tenants",
            data={"name": "Chez Neuf", "phone_number": "+33199000773", "moteur_voix": "gpt_live"},
            headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)
        assert reponse.status_code == 303
        neuf = tenants.get_by_phone("+33199000773")
        try:
            assert neuf.moteur_voix == "gpt_live"
        finally:
            tenants.delete_tenant(neuf.id)


class TestLaFiche:
    def test_le_super_admin_voit_le_menu_et_le_moteur_qui_vaut_aujourd_hui(self, client, resto, monkeypatch):
        _admin(client)
        page = _page(client.get(f"/admin/tenants/{resto.id}/edit"))
        assert "Moteur de l'appel" in page and "GPT-Live · secours classique" in page
        assert _selectionne(page) == "classique"
        # L'ancien réglage du .env se voit dans la fiche tant qu'aucun choix n'est rangé.
        monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", str(resto.id))
        assert _selectionne(_page(client.get(f"/admin/tenants/{resto.id}/edit"))) == "gpt_live"
        tenants.update_tenant(resto.id, moteur_voix="classique")
        assert _selectionne(_page(client.get(f"/admin/tenants/{resto.id}/edit"))) == "classique"

    def test_le_restaurateur_ne_voit_pas_le_menu(self, client, resto):
        _restaurateur(client, resto)
        page = _page(client.get(f"/admin/tenants/{resto.id}/edit"))
        assert 'name="moteur_voix"' not in page and "GPT-Live" not in page

    def test_gpt_live_sans_cle_le_dit_au_lieu_de_laisser_croire(self, client, resto, monkeypatch):
        tenants.update_tenant(resto.id, moteur_voix="gpt_live")
        _admin(client)
        assert "Clé OpenAI absente" not in _page(client.get(f"/admin/tenants/{resto.id}/edit"))
        monkeypatch.delenv("OPENAI_API_KEY")
        page = _page(client.get(f"/admin/tenants/{resto.id}/edit"))
        assert "Clé OpenAI absente" in page and "sk-test" not in page
        # Et le parc le signale, sans attendre qu'on ouvre la fiche.
        assert "Chez Moteur · GPT-Live choisi, clé OpenAI absente" in _page(client.get("/admin/"))

    def test_un_nouvel_etablissement_est_propose_sur_gpt_live_si_la_cle_existe(self, client, monkeypatch):
        """« En général je mets GPT-Live par défaut » : le menu le propose, rien n'est
        écrit tant que le formulaire n'est pas enregistré."""
        _admin(client)
        assert _selectionne(_page(client.get("/admin/tenants/new"))) == "gpt_live"
        monkeypatch.delenv("OPENAI_API_KEY")
        assert _selectionne(_page(client.get("/admin/tenants/new"))) == "classique"

    def test_la_liste_des_etablissements_dit_le_moteur_de_chacun(self, client, resto, voisin):
        tenants.update_tenant(resto.id, moteur_voix="gpt_live")
        _admin(client)
        page = _page(client.get("/admin/tenants"))
        ligne = lambda nom: page[page.index(nom):page.index("Voir comme le gérant", page.index(nom))]  # noqa: E731
        assert "Moteur : GPT-Live" in ligne("Chez Moteur")
        assert "Moteur : chaîne classique" in ligne("Chez Voisin")


class TestCeQueChaqueMoteurCoute:
    def test_la_voix_de_gpt_live_a_sa_ligne_et_ne_gonfle_plus_la_voix_mistral(self, resto):
        """Jusqu'au 05/10/2026, les minutes de GPT-Live s'affichaient sous « Voix Mistral
        (Voxtral) » : un fournisseur payé pour le travail d'un autre."""
        _appel(resto.id, "CA-moteur-live", "gpt-live", 120, telephonie=0.02, transcription=0.0,
               comprehension=0.004, cout_voix=0.10)
        _appel(resto.id, "CA-moteur-classique", "voxtral", 120, telephonie=0.02, transcription=0.0184,
               comprehension=0.006, cout_voix=0.012)
        lignes = {l["label"]: l["amount"] for l in calls.cost_breakdown(resto.id, days=30)}
        assert lignes["Voix Mistral (Voxtral)"] == pytest.approx(0.012)
        assert lignes["Voix GPT-Live (OpenAI)"] == pytest.approx(0.10)
        # La répartition retombe toujours sur le coût total.
        assert sum(lignes.values()) == pytest.approx(calls.totals(resto.id, days=30)["total_cost"])

    def test_sans_appel_gpt_live_la_ligne_n_existe_pas(self, resto):
        _appel(resto.id, "CA-moteur-seul", "voxtral", 60, telephonie=0.01, transcription=0.0092,
               comprehension=0.003, cout_voix=0.006)
        assert not [l for l in calls.cost_breakdown(resto.id, days=30) if "GPT-Live" in l["label"]]

    def test_le_cout_a_la_minute_de_chaque_moteur_est_mesure(self, resto):
        _appel(resto.id, "CA-m-live-1", "gpt-live", 60, telephonie=0.01, transcription=0.0,
               comprehension=0.002, cout_voix=0.05)
        _appel(resto.id, "CA-m-live-2", "gpt-live", 120, telephonie=0.02, transcription=0.0,
               comprehension=0.004, cout_voix=0.10)
        _appel(resto.id, "CA-m-classique", "voxtral", 120, telephonie=0.02, transcription=0.0184,
               comprehension=0.006, cout_voix=0.012)
        # Ni l'un ni l'autre : la voix d'avant la bascule, le banc d'essai, un appel en cours.
        _appel(resto.id, "CA-m-moshi", "moshi", 600, telephonie=0.1, transcription=0.09,
               comprehension=0.01, cout_voix=0.2)
        _appel(resto.id, f"{calls.PREFIXE_BANC}-m", "gpt-live", 30_000, telephonie=0.0,
               transcription=0.0, comprehension=0.01, cout_voix=0.05)
        with db.get_conn() as conn:
            conn.execute("INSERT INTO calls (call_sid, tenant_id, voix_fournisseur) "
                         "VALUES ('CA-m-en-cours', ?, 'gpt-live')", (resto.id,))
        mesures = calls.par_moteur(resto.id, days=30)
        assert set(mesures) == {"gpt_live", "classique"}
        gpt = mesures["gpt_live"]
        assert gpt["n_calls"] == 2 and gpt["minutes"] == pytest.approx(3.0)
        assert gpt["total"] == pytest.approx(0.186)
        assert gpt["par_minute"] == pytest.approx(0.062)
        assert gpt["moteur_par_minute"] == pytest.approx(0.052)       # sans Twilio
        classique = mesures["classique"]
        assert classique["n_calls"] == 1 and classique["par_minute"] == pytest.approx(0.0282)
        assert classique["moteur_par_minute"] == pytest.approx(0.0182)

    def test_un_moteur_sans_appel_n_a_pas_de_chiffre(self, resto, voisin):
        _appel(resto.id, "CA-m-seul", "voxtral", 60, telephonie=0.01, transcription=0.0092,
               comprehension=0.003, cout_voix=0.006)
        assert set(calls.par_moteur(resto.id)) == {"classique"}
        assert calls.par_moteur(voisin.id) == {}

    def test_les_tarifs_affiches_sont_ceux_du_chiffrage(self):
        tarifs = calls.tarifs_gpt_live()
        une_minute = calls.couts_appel(60, {"voix": {"fournisseur": "gpt-live"},
                                            "consommation": {"secondes_voix": 60.0,
                                                             "jetons_entree": 1_000_000}})
        assert une_minute["voix"] == pytest.approx(tarifs["voix_par_minute"])
        assert une_minute["telephonie"] == pytest.approx(tarifs["telephonie_par_minute"])
        assert une_minute["comprehension"] == pytest.approx(tarifs["entree_par_million"])
        # Le cerveau confié à OpenAI se chiffre à ses propres tarifs.
        openai = calls.couts_appel(60, {"voix": {"fournisseur": "gpt-live", "cerveau": "openai"},
                                        "consommation": {"jetons_entree": 1_000_000}})
        assert openai["comprehension"] == pytest.approx(
            calls.tarifs_gpt_live(cerveau_openai=True)["entree_par_million"])

    def test_sante_et_couts_montre_les_deux_moteurs_et_explique_le_calcul(self, client, resto):
        _appel(resto.id, "CA-s-live", "gpt-live", 120, telephonie=0.02, transcription=0.0,
               comprehension=0.004, cout_voix=0.10)
        _appel(resto.id, "CA-s-classique", "voxtral", 120, telephonie=0.02, transcription=0.0184,
               comprehension=0.006, cout_voix=0.012)
        tenants.update_tenant(resto.id, moteur_voix="gpt_live")
        _admin(client)
        page = _page(client.get("/admin/health"))
        carte = page[page.index('id="moteurs"'):]
        carte = carte[:carte.index("</section>")]
        assert "GPT-Live" in carte and "Chaîne classique" in carte and "$/min" in carte
        mesures = calls.par_moteur(None, days=30)
        for moteur in ("gpt_live", "classique"):
            assert f"{mesures[moteur]['par_minute']:.3f}".replace(".", ",") + " $/min" in carte
        assert "Comment un appel GPT-Live est chiffré" in carte
        assert "0,05 $ la minute de session" in carte and "relevés le 04/10/2026" in carte
        assert "Voix GPT-Live (OpenAI)" in page
        assert "Voix-à-voix (GPT-Live)" in page and "prêt" in page
        assert "Dernier échec de GPT-Live" not in page

    def test_sante_et_couts_dit_quand_gpt_live_est_a_l_ecart(self, client, monkeypatch):
        live.mettre_a_l_ecart("session refusée (credit_balance_exhausted)")
        _admin(client)
        page = _page(client.get("/admin/health"))
        assert "Dernier échec de GPT-Live" in page and "credit_balance_exhausted" in page
        assert "à l'écart" in page
        monkeypatch.delenv("OPENAI_API_KEY")
        assert "clé absente" in _page(client.get("/admin/health"))

    def test_la_fiche_d_un_appel_dit_le_moteur_qui_l_a_servi_a_l_exploitant_seulement(self, client, resto):
        _appel(resto.id, "CA-fiche-live", "gpt-live", 90, telephonie=0.02, transcription=0.0,
               comprehension=0.003, cout_voix=0.075)
        with db.get_conn() as conn:
            identifiant = conn.execute("SELECT id FROM calls WHERE call_sid = 'CA-fiche-live'").fetchone()[0]
        _admin(client)
        assert "· GPT-Live" in _page(client.get(f"/admin/calls/{identifiant}"))
        gerant = TestClient(app)
        _restaurateur(gerant, resto)
        assert "GPT-Live" not in _page(gerant.get(f"/admin/calls/{identifiant}"))
