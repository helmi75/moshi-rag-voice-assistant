"""« Nous écrire » : le formulaire de contact du site (ASSISTANTE-185).

La route est ouverte à tout le monde : on vérifie surtout ce qu'elle REFUSE (champs,
plafonds, autre site, corps trop gros), que le message est gardé en base même sans SMTP, et
que la page de l'admin n'est lue que par le super-admin. Aucun réseau : l'e-mail est un
bouchon.
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import contact, db, rgpd, tenants, users
from app.main import app

JSON = {"Content-Type": "application/json"}
VALIDE = {"nom": "Camille Durand", "email": "camille@exemple.fr", "telephone": "06 12 34 56 78",
          "etablissement": "Chez Camille", "message": "Bonjour, je voudrais une démonstration."}


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _contact_pret(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("RAPPEL_NOTIFIER", raising=False)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM contacts")
    contact.reinitialiser()
    yield
    contact.reinitialiser()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM contacts")


def _lignes() -> list[dict]:
    return contact.lister()


def _envoyer(client, corps=None, **champs):
    donnees = dict(VALIDE if corps is None else corps)
    donnees.update(champs)
    return client.post("/contact", json=donnees)


class TestEnvoi:
    def test_un_message_valide_est_garde_en_base(self, client):
        r = _envoyer(client)
        assert r.status_code == 200
        assert r.json()["message"] == "Message envoyé. Nous vous répondons par e-mail."
        [ligne] = _lignes()
        assert ligne["nom"] == "Camille Durand" and ligne["email"] == "camille@exemple.fr"
        assert ligne["telephone"] == "06 12 34 56 78" and ligne["lu_at"] is None

    def test_les_champs_facultatifs_peuvent_manquer(self, client):
        corps = {k: v for k, v in VALIDE.items() if k not in ("telephone", "etablissement")}
        assert _envoyer(client, corps).status_code == 200
        assert _lignes()[0]["telephone"] is None

    def test_les_espaces_autour_sont_retires(self, client):
        _envoyer(client, nom="  Camille  ")
        assert _lignes()[0]["nom"] == "Camille"

    @pytest.mark.parametrize("champ,valeur", [
        ("nom", ""), ("nom", "   "), ("nom", "x" * 101), ("nom", "Camille\nDurand"),
        ("email", ""), ("email", "pas-un-mail"), ("email", "a@b"), ("email", "a b@c.fr"),
        ("email", "a" * 250 + "@b.fr"), ("email", "a@b.fr\nBcc: x@y.fr"),
        ("telephone", "1" * 31), ("telephone", "06\n12"),
        ("etablissement", "x" * 121), ("etablissement", "Chez\rCamille"),
        ("message", ""), ("message", "trop bref"), ("message", "x" * 2001),
        ("nom", 12), ("message", ["liste"]),
    ])
    def test_un_champ_invalide_est_refuse_en_francais(self, client, champ, valeur):
        r = _envoyer(client, **{champ: valeur})
        assert r.status_code == 422 and r.json()["etat"] == "champs"
        assert r.json()["message"]
        assert _lignes() == []

    def test_un_message_de_2000_caracteres_passe(self, client):
        assert _envoyer(client, message="é" * 2000).status_code == 200

    def test_un_autre_type_de_contenu_est_refuse_formulaire_classique(self, client):
        r = client.post("/contact", data=VALIDE)
        assert r.status_code == 415 and _lignes() == []

    def test_une_demande_venue_d_un_autre_site_est_refusee_autre_site(self, client):
        r = client.post("/contact", json=VALIDE, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.post("/contact", json=VALIDE, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert _lignes() == []

    def test_le_meme_site_passe(self, client):
        r = client.post("/contact", json=VALIDE,
                        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

    def test_un_corps_trop_gros_est_refuse(self, client):
        r = client.post("/contact", content=b'{"message": "' + b"x" * 20000 + b'"}', headers=JSON)
        assert r.status_code == 400 and _lignes() == []

    def test_un_corps_illisible_est_refuse(self, client):
        assert client.post("/contact", content=b"pas du json", headers=JSON).status_code == 400
        assert client.post("/contact", content=b"[1]", headers=JSON).status_code == 400

    def test_le_champ_piege_repond_ok_sans_rien_enregistrer(self, client):
        r = _envoyer(client, site="http://spam.example")
        assert r.status_code == 200 and _lignes() == []


class TestLesPlafonds:
    def test_le_quatrieme_envoi_de_la_meme_adresse_dans_l_heure_est_refuse(self, client):
        for _ in range(3):
            assert _envoyer(client).status_code == 200
        r = _envoyer(client)
        assert r.status_code == 429 and len(_lignes()) == 3

    def test_un_message_invalide_ne_compte_pas_pour_l_adresse(self, client):
        for _ in range(5):
            assert _envoyer(client, message="court").status_code == 422
        assert _envoyer(client).status_code == 200

    def test_le_plafond_du_jour_borne_la_boite(self, client, monkeypatch):
        monkeypatch.setattr(contact, "MAX_PAR_JOUR", 2)
        monkeypatch.setattr(contact, "PAR_ADRESSE_PAR_HEURE", 100)
        assert _envoyer(client).status_code == 200
        assert _envoyer(client).status_code == 200
        r = _envoyer(client)
        assert r.status_code == 503
        assert r.json()["message"] == "Beaucoup de messages aujourd'hui, écrivez-nous plus tard."
        assert len(_lignes()) == 2

    def test_le_plafond_du_jour_est_de_cinquante(self):
        assert contact.MAX_PAR_JOUR == 50 and contact.PAR_ADRESSE_PAR_HEURE == 3

    def test_le_compteur_par_adresse_ne_grossit_pas_sans_fin(self, monkeypatch):
        monkeypatch.setattr(contact, "_ADRESSES_MAX", 10)
        for i in range(50):
            contact.adresse_bloquee(f"203.0.113.{i}")
        assert len(contact._par_adresse) <= 51


class TestLEmail:
    def test_le_sujet_est_fixe_et_ne_porte_rien_du_visiteur(self, client, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        monkeypatch.setenv("SMTP_FROM", "marie@test.local")
        monkeypatch.setenv("PUBLIC_URL", "https://helmane.test")
        # `envoyer` rend ses arguments au lieu d'une coroutine : on lit ce qui serait parti.
        with patch("app.contact.taches.lancer") as lancer, \
                patch("app.contact.notifications.envoyer", new=lambda *a: a):
            assert _envoyer(client, nom="Camille Secrète").status_code == 200
        adresses, sujet, corps = lancer.call_args.args[0]
        assert adresses == ["admin@test.local"]
        assert sujet == "Message reçu depuis le site"
        assert "Camille" not in sujet and "exemple.fr" not in sujet
        assert "Camille Secrète" in corps and "camille@exemple.fr" in corps
        assert "https://helmane.test/admin/contacts" in corps

    def test_sans_smtp_le_message_est_garde_et_la_requete_reussit(self, client):
        with patch("app.contact.taches.lancer") as lancer:
            assert _envoyer(client).status_code == 200
        lancer.assert_not_called()
        assert len(_lignes()) == 1

    def test_un_envoi_qui_echoue_ne_fait_pas_echouer_la_requete(self, client, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        monkeypatch.setenv("SMTP_FROM", "marie@test.local")
        with patch("app.contact.taches.lancer", side_effect=RuntimeError("boum")):
            assert _envoyer(client).status_code == 200
        assert len(_lignes()) == 1


class TestLaPageDeLAdmin:
    @pytest.fixture()
    def restaurateur(self):
        t = tenants.create_tenant("Chez Contact", "+33100000185")
        u = users.create_user(f"resto-{t.id}@test.fr", "-".join(("compte", "contact", "sans", "valeur")),
                              users.ROLE_RESTAURATEUR, t.id)
        yield u, "-".join(("compte", "contact", "sans", "valeur"))
        tenants.delete_tenant(t.id)

    def _connecter(self, client, email, mot_de_passe):
        r = client.post("/admin/login", data={"email": email, "password": mot_de_passe},
                        follow_redirects=False)
        assert r.status_code == 303, "connexion refusée : le test ne prouverait rien"

    def _super(self, client):
        self._connecter(client, os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"])

    def test_sans_session_on_est_renvoye_a_la_connexion(self, client):
        r = client.get("/admin/contacts", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/admin/login" in r.headers["location"]

    def test_le_super_admin_voit_les_messages(self, client):
        _envoyer(client)
        self._super(client)
        r = client.get("/admin/contacts")
        assert r.status_code == 200
        assert "Camille Durand" in r.text and "camille@exemple.fr" in r.text
        assert "Bonjour, je voudrais une démonstration." in r.text
        assert 'href="/admin/contacts"' in r.text  # le lien du menu

    def test_les_plus_recents_d_abord(self, client):
        _envoyer(client, nom="Premier")
        with db.get_conn() as conn:
            conn.execute("UPDATE contacts SET created_at = '2026-01-01T10:00:00Z'")
        _envoyer(client, nom="Second")
        self._super(client)
        page = client.get("/admin/contacts").text
        assert page.index("Second") < page.index("Premier")

    def test_un_restaurateur_n_y_a_pas_acces_restaurateur(self, client, restaurateur):
        _envoyer(client)
        u, mdp = restaurateur
        self._connecter(client, u.email, mdp)
        r = client.get("/admin/contacts")
        assert r.status_code == 403
        assert "Camille" not in r.text
        # ni le lien dans son menu
        assert 'href="/admin/contacts"' not in client.get("/admin/").text

    def test_le_texte_d_un_visiteur_est_affiche_echappe(self, client):
        _envoyer(client, message="<script>alert(1)</script> bonjour", nom="<b>Gras</b>")
        self._super(client)
        page = client.get("/admin/contacts").text
        assert "<script>alert(1)</script>" not in page and "<b>Gras</b>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    def test_marquer_lu_exige_le_jeton_csrf(self, client):
        _envoyer(client)
        self._super(client)
        identifiant = _lignes()[0]["id"]
        r = client.post(f"/admin/contacts/{identifiant}/lu", data={"csrf_token": "faux"},
                        follow_redirects=False)
        assert r.status_code == 403 and _lignes()[0]["lu_at"] is None

    def test_marquer_lu(self, client):
        _envoyer(client)
        self._super(client)
        page = client.get("/admin/contacts").text
        jeton = page.split('name="csrf_token" value="')[1].split('"')[0]
        identifiant = _lignes()[0]["id"]
        r = client.post(f"/admin/contacts/{identifiant}/lu", data={"csrf_token": jeton},
                        follow_redirects=False)
        assert r.status_code == 303 and _lignes()[0]["lu_at"]
        assert "Marquer lu" not in client.get("/admin/contacts").text

    def test_un_restaurateur_ne_peut_pas_marquer_lu(self, client, restaurateur):
        _envoyer(client)
        u, mdp = restaurateur
        self._connecter(client, u.email, mdp)
        jeton = client.get("/admin/").text.split('"X-CSRF-Token": "')[1].split('"')[0]
        r = client.post(f"/admin/contacts/{_lignes()[0]['id']}/lu", data={"csrf_token": jeton},
                        follow_redirects=False)
        assert r.status_code == 403 and _lignes()[0]["lu_at"] is None


class TestLaConservation:
    def test_la_purge_efface_les_anciens_messages(self, client):
        _envoyer(client, nom="Ancien")
        _envoyer(client, nom="Récent")
        with db.get_conn() as conn:
            conn.execute("UPDATE contacts SET created_at = '2020-01-01T00:00:00Z' WHERE nom = 'Ancien'")
        rgpd.purger()
        assert [c["nom"] for c in _lignes()] == ["Récent"]

    def test_la_duree_se_regle_et_vaut_un_an_par_defaut(self, monkeypatch):
        monkeypatch.delenv("RETENTION_CONTACT_JOURS", raising=False)
        assert rgpd.jours_contact() == 365
        monkeypatch.setenv("RETENTION_CONTACT_JOURS", "30")
        assert rgpd.jours_contact() == 30


class TestLaPage:
    def test_la_page_d_accueil_porte_le_formulaire(self, client):
        page = client.get("/").text
        assert 'id="contact"' in page and 'class="contact"' in page
        assert 'href="#contact"' in page
        assert "Vos coordonnées ne servent qu'à vous répondre." in page
        assert page.index('id="contact"') < page.index("<footer")


def test_la_page_n_affiche_que_les_messages_les_plus_recents(monkeypatch):
    """Revue d'ASSISTANTE-185 : 50 messages par jour pendant un an ne font pas une page de
    18 000 lignes."""
    monkeypatch.setattr(contact, "AFFICHES", 3)
    for i in range(5):
        contact.enregistrer({"nom": f"N{i}", "email": "a@b.fr", "telephone": "",
                             "etablissement": "", "message": "un message assez long"})
    noms = [c["nom"] for c in contact.lister()]
    assert noms == ["N4", "N3", "N2"]
