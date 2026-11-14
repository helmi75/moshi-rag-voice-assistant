"""Démonstration personnalisée (ASSISTANTE-139).

Aucun réseau : la résolution de noms (`socket.getaddrinfo`) et le transport HTTP sont
remplacés, le modèle est une doublure. On vérifie surtout ce que la fonction REFUSE : une
adresse du réseau interne (SSRF), une information que le site ne dit pas, une validation sans
relecture, et tout ce qui pourrait faire parvenir un essai à un vrai restaurateur."""
import asyncio
import json
import os
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connecteurs, db, demos, notifications, reservations, rgpd, taches, tenants, users
from app.main import app

PAGE = """<html><head><title>Chez Marcel</title><style>.x{color:red}</style></head><body>
<script>var secret = "ne pas lire";</script>
<h1>Chez Marcel</h1><p>Bistrot lyonnais, 12 rue des Halles, Lyon.</p>
<p>Ouvert du mardi au samedi, de 12h à 14h30 et de 19h à 22h30.</p>
<p>Réservations : 04 78 00 00 00</p></body></html>"""


def _resolution(table: dict):
    def getaddrinfo(hote, port, *a, **k):
        adresses = table.get(hote)
        if adresses is None:
            raise socket.gaierror("inconnu")
        return [(socket.AF_INET6 if ":" in x else socket.AF_INET, socket.SOCK_STREAM, 6, "", (x, port))
                for x in adresses]
    return getaddrinfo


@pytest.fixture()
def reseau(monkeypatch):
    """Un faux Internet : `site.example` est public, les autres noms pointent où on le dit.
    Rend la liste des URL réellement demandées au transport."""
    demandees: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _resolution({
        "site.example": ["93.184.216.34"], "interne.example": ["10.0.0.5"],
        "double.example": ["93.184.216.34", "192.168.1.10"], "v6.example": ["2606:2800:220:1::1"],
        "rebond.example": ["169.254.169.254"], "127.0.0.1": ["127.0.0.1"], "10.1.2.3": ["10.1.2.3"],
        "169.254.169.254": ["169.254.169.254"], "::1": ["::1"], "mappee.example": ["::ffff:127.0.0.1"],
    }))
    reponses: dict = {}

    def handler(requete: httpx.Request) -> httpx.Response:
        demandees.append(str(requete.url))
        return reponses.get(requete.headers.get("host"), httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=PAGE.encode()))

    monkeypatch.setattr(demos, "_TRANSPORT", httpx.MockTransport(handler))
    return SimpleNamespace(demandees=demandees, reponses=reponses)


def lire(url):
    return asyncio.run(demos.lire_le_site(url))


class TestLireLeSite:
    def test_un_site_public_est_lu_sans_script_ni_style(self, reseau):
        texte = lire("https://site.example/")
        assert "Bistrot lyonnais" in texte and "04 78 00 00 00" in texte
        assert "secret" not in texte and "color:red" not in texte
        # on se connecte à l'adresse vérifiée, pas au nom
        assert reseau.demandees == ["https://93.184.216.34/"]

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/", "https://interne.example/", "https://double.example/",
        "https://rebond.example/", "https://mappee.example/",
    ])
    def test_une_adresse_non_publique_est_refusee_avant_toute_requete(self, reseau, url):
        with pytest.raises(demos.SiteRefuse, match="pas publique"):
            lire(url)
        assert reseau.demandees == []

    @pytest.mark.parametrize("url", ["ftp://site.example/", "file:///etc/passwd",
                                     "https://user:mdp@site.example/"])
    def test_un_schema_ou_un_identifiant_refuse(self, reseau, url):
        with pytest.raises(demos.SiteRefuse):
            lire(url)
        assert reseau.demandees == []

    @pytest.mark.parametrize("url", ["http://site.example:22/", "https://site.example:8080/"])
    def test_un_autre_port_que_80_et_443_est_refuse(self, reseau, url):
        with pytest.raises(demos.SiteRefuse, match="ports"):
            lire(url)

    def test_un_nom_introuvable_est_refuse_proprement(self, reseau):
        with pytest.raises(demos.SiteRefuse, match="introuvable"):
            lire("https://inconnu.example/")

    def test_une_redirection_vers_une_adresse_privee_est_refusee(self, reseau):
        reseau.reponses["site.example"] = httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        with pytest.raises(demos.SiteRefuse, match="pas publique"):
            lire("https://site.example/")
        assert reseau.demandees == ["https://93.184.216.34/"]

    def test_une_redirection_publique_est_suivie_mais_pas_a_l_infini(self, reseau):
        reseau.reponses["site.example"] = httpx.Response(301, headers={"location": "/suite"})
        with pytest.raises(demos.SiteRefuse, match="redirections"):
            lire("https://site.example/")
        assert len(reseau.demandees) == demos.REDIRECTIONS_MAX + 1

    def test_une_reponse_trop_grosse_est_refusee(self, reseau, monkeypatch):
        monkeypatch.setattr(demos, "TAILLE_MAX", 100)
        reseau.reponses["site.example"] = httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<p>" + b"x" * 500 + b"</p>")
        with pytest.raises(demos.SiteRefuse, match="volumineuse"):
            lire("https://site.example/")

    def test_un_type_autre_que_html_est_refuse(self, reseau):
        reseau.reponses["site.example"] = httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"\x89PNG")
        with pytest.raises(demos.SiteRefuse, match="HTML"):
            lire("https://site.example/")

    def test_le_texte_est_borne(self, reseau, monkeypatch):
        monkeypatch.setattr(demos, "TEXTE_MAX", 50)
        assert len(lire("https://site.example/")) <= 50


class FauxModele:
    def __init__(self, contenu: str, usage=None):
        self.contenu = contenu
        self.usage = usage or SimpleNamespace(total_tokens=321, cost=0.0042)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.contenu))],
                               usage=self.usage)


def modele(monkeypatch, objet) -> None:
    monkeypatch.setattr(demos.llm, "get_client", lambda: FauxModele(json.dumps(objet)))


TEXTE = demos.html_en_texte(PAGE)


class TestExtraction:
    def test_une_information_citee_est_gardee_avec_sa_source(self, monkeypatch):
        modele(monkeypatch, {"adresse": {"valeur": "12 rue des Halles, Lyon",
                                         "citation": "12 RUE des Halles,  Lyon"}})
        r = asyncio.run(demos.extraire(TEXTE))
        assert r["champs"]["adresse"] == {"valeur": "12 rue des Halles, Lyon",
                                          "citation": "12 RUE des Halles, Lyon", "trouve": True}
        assert r["jetons"] == 321 and r["cout_usd"] == 0.0042

    def test_une_citation_absente_du_site_vide_le_champ(self, monkeypatch):
        modele(monkeypatch, {"cuisine": {"valeur": "Étoilée Michelin", "citation": "Une étoile au Michelin"},
                             "paiement": {"valeur": "Cartes acceptées", "citation": ""}})
        champs = asyncio.run(demos.extraire(TEXTE))["champs"]
        assert champs["cuisine"] == {"valeur": "", "citation": "", "trouve": False}
        assert champs["paiement"]["trouve"] is False

    def test_un_chiffre_que_la_citation_ne_porte_pas_est_refuse(self, monkeypatch):
        modele(monkeypatch, {"telephone": {"valeur": "04 78 99 99 99", "citation": "Réservations : 04 78 00 00 00"}})
        assert asyncio.run(demos.extraire(TEXTE))["champs"]["telephone"]["trouve"] is False

    def test_les_horaires_structures_exigent_un_format_valide_et_une_citation(self, monkeypatch):
        semaine = {"mardi": [["12:00", "14:30"], ["19:00", "22:30"]]}
        citation = "Ouvert du mardi au samedi, de 12h à 14h30"
        modele(monkeypatch, {"horaires": {"valeur": {"semaine": semaine}, "citation": citation}})
        h = asyncio.run(demos.extraire(TEXTE))["champs"]["horaires"]
        assert h["trouve"] and json.loads(h["valeur"])["semaine"]["mardi"][0] == ["12:00", "14:30"]
        modele(monkeypatch, {"horaires": {"valeur": {"semaine": {"mardi": [["midi", "14h"]]}}, "citation": citation}})
        assert asyncio.run(demos.extraire(TEXTE))["champs"]["horaires"]["trouve"] is False
        modele(monkeypatch, {"horaires": {"valeur": {"semaine": semaine}, "citation": "Ouvert tous les jours"}})
        assert asyncio.run(demos.extraire(TEXTE))["champs"]["horaires"]["trouve"] is False
        modele(monkeypatch, {"horaires": {"valeur": {"semaine": {}}, "citation": citation}})
        assert asyncio.run(demos.extraire(TEXTE))["champs"]["horaires"]["trouve"] is False

    def test_le_cout_n_est_jamais_estime(self, monkeypatch):
        monkeypatch.setattr(demos.llm, "get_client", lambda: FauxModele("{}", SimpleNamespace(total_tokens=10)))
        r = asyncio.run(demos.extraire(TEXTE))
        assert r["jetons"] == 10 and r["cout_usd"] is None


@pytest.fixture()
def actives(monkeypatch):
    monkeypatch.setenv("DEMOS_ACTIVES", "1")
    yield
    with db.get_conn() as conn:
        for (t,) in conn.execute("SELECT tenant_id FROM demonstrations WHERE tenant_id IS NOT NULL").fetchall():
            tenants.delete_tenant(t)
        conn.execute("DELETE FROM demonstrations")


def _demo_lue(monkeypatch, reseau) -> int:
    modele(monkeypatch, {"adresse": {"valeur": "12 rue des Halles, Lyon", "citation": "12 rue des Halles, Lyon"},
                         "cuisine": {"valeur": "Bistrot lyonnais", "citation": "Bistrot lyonnais"}})
    identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
    asyncio.run(demos.preparer(identifiant))
    return identifiant


class TestLeCycle:
    def test_preparer_lit_extrait_et_laisse_a_relire(self, actives, monkeypatch, reseau):
        d = demos.obtenir(_demo_lue(monkeypatch, reseau))
        assert d["etat"] == "a_relire" and d["relu"] is False and d["lecture"] == "terminee"
        assert d["champs"]["adresse"]["trouve"] and d["jetons"] == 321 and d["site_url"] == "https://site.example"

    def test_un_echec_de_lecture_laisse_des_champs_vides_et_un_message(self, actives, monkeypatch, reseau):
        identifiant = demos.creer("Chez Marcel", "Lyon", "http://127.0.0.1/")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert d["etat"] == "a_relire" and "pas publique" in d["message"]
        assert not any(c["trouve"] for c in d["champs"].values())

    def test_un_modele_en_panne_n_est_pas_une_erreur(self, actives, monkeypatch, reseau):
        def panne():
            raise RuntimeError("clé invalide sk-secret")
        monkeypatch.setattr(demos.llm, "get_client", panne)
        identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert "à la main" in d["message"] and "sk-secret" not in d["message"]

    def test_valider_sans_relecture_est_refuse_cote_serveur(self, actives, monkeypatch, reseau):
        identifiant = _demo_lue(monkeypatch, reseau)
        with pytest.raises(demos.PasRelue):
            demos.valider(identifiant)
        assert demos.obtenir(identifiant)["tenant_id"] is None

    def test_valider_cree_un_etablissement_sans_numero_ni_compte(self, actives, monkeypatch, reseau):
        identifiant = _demo_lue(monkeypatch, reseau)
        demos.enregistrer_relecture(identifiant, {"adresse": "12 rue des Halles, Lyon", "cuisine": "Bistrot"})
        t = demos.valider(identifiant)
        assert t.name == "Chez Marcel" and t.greeting == "Bonjour, Chez Marcel."
        assert t.phone_number == f"demo-{identifiant}" and tenants.est_demo(t)
        assert "12 rue des Halles, Lyon" in t.knowledge_base and "Bistrot" in t.knowledge_base
        assert users.list_users(t.id) == []
        d = demos.obtenir(identifiant)
        assert d["etat"] == "prete" and d["tenant_id"] == t.id and d["validee_at"]
        with pytest.raises(demos.Refus):
            demos.valider(identifiant)

    def test_effacer_supprime_l_etablissement_et_vide_les_champs(self, actives, monkeypatch, reseau):
        identifiant = _demo_lue(monkeypatch, reseau)
        demos.enregistrer_relecture(identifiant, {})
        t = demos.valider(identifiant)
        reservations.create_reservation(t.id, "Essai", "2030-03-13", "20:00", 2)
        assert demos.effacer(identifiant) is True
        assert tenants.get_by_id(t.id) is None
        d = demos.obtenir(identifiant)
        assert d["etat"] == "effacee" and d["tenant_id"] is None
        assert not any(c["trouve"] for c in d["champs"].values())
        assert demos.effacer(999999) is False

    def test_la_purge_efface_les_vieilles_demonstrations(self, actives, monkeypatch, reseau):
        vieille = _demo_lue(monkeypatch, reseau)
        demos.enregistrer_relecture(vieille, {})
        t = demos.valider(vieille)
        recente = demos.creer("Récent", "Lyon", "site.example")
        with db.get_conn() as conn:
            conn.execute("UPDATE demonstrations SET created_at = ? WHERE id = ?",
                         (rgpd.horloge.il_y_a(31), vieille))
        assert rgpd.jours_demo() == 30
        rgpd.purger()
        # l'établissement part, et la ligne avec lui : plus rien du restaurant démarché
        assert demos.obtenir(vieille) is None and tenants.get_by_id(t.id) is None
        assert demos.obtenir(recente)["etat"] == "a_relire"

    def test_les_saisies_sont_bornees(self, actives):
        for nom, ville, url in [("", "Lyon", "site.example"), ("X" * 101, "Lyon", "site.example"),
                                ("A", "Lyon", "ftp://site.example"), ("A", "Lyon", "a b")]:
            with pytest.raises(demos.Refus):
                demos.creer(nom, ville, url)


class TestAucunEssaiNePrevientUnVraiRestaurateur:
    def test_les_destinataires_d_une_demo_sont_vides(self):
        t = tenants.create_tenant("Chez Demo", "demo-424242", knowledge_base="")
        try:
            tenants.update_tenant(t.id, notify_email="vrai@restaurant.fr")
            users.create_user("vrai-compte@restaurant.fr", "mot-de-passe-de-test-1", users.ROLE_RESTAURATEUR, t.id)
            assert notifications.destinataires(tenants.get_by_id(t.id)) == []
        finally:
            tenants.delete_tenant(t.id)

    def test_un_etablissement_ordinaire_garde_ses_destinataires(self):
        t = tenants.create_tenant("Chez Vrai", "+33100000424")
        try:
            tenants.update_tenant(t.id, notify_email="vrai@restaurant.fr")
            assert notifications.destinataires(tenants.get_by_id(t.id)) == ["vrai@restaurant.fr"]
        finally:
            tenants.delete_tenant(t.id)

    def test_le_carnet_d_une_demo_est_toujours_interne(self):
        t = tenants.create_tenant("Chez Demo", "demo-424243")
        try:
            tenants.update_tenant(t.id, booking_provider="resos")
            tenant = tenants.get_by_id(t.id)
            assert connecteurs.fournisseur(tenant) == connecteurs.INTERNE
            assert not connecteurs.est_resos(tenant)
            assert type(connecteurs.pour(tenant)).__name__ == "ConnecteurInterne"
        finally:
            tenants.delete_tenant(t.id)


@pytest.fixture()
def client():
    return TestClient(app)


def _connecter(client, email=None, mdp=None):
    r = client.post("/admin/login", data={"email": email or os.environ["ADMIN_EMAIL"],
                                          "password": mdp or os.environ["ADMIN_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 303
    jeton = client.get("/admin/").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return {"X-CSRF-Token": jeton}


class TestLesPages:
    def test_sans_l_interrupteur_tout_est_404_et_le_menu_est_absent(self, client, monkeypatch):
        monkeypatch.delenv("DEMOS_ACTIVES", raising=False)
        entete = _connecter(client)
        assert client.get("/admin/demos").status_code == 404
        assert client.get("/admin/demos/1").status_code == 404
        assert client.post("/admin/demos", headers=entete, data={"nom": "A"}).status_code == 404
        assert client.post("/admin/demos/1/valider", headers=entete).status_code == 404
        assert 'href="/admin/demos"' not in client.get("/admin/").text

    def test_le_super_admin_prepare_relit_valide_et_efface(self, client, actives, monkeypatch, reseau):
        modele(monkeypatch, {"cuisine": {"valeur": "Bistrot lyonnais <b>", "citation": "Bistrot lyonnais"}})
        monkeypatch.setattr(taches, "lancer", lambda coro, nom: coro.close())
        entete = _connecter(client)
        assert 'href="/admin/demos"' in client.get("/admin/").text
        r = client.post("/admin/demos", headers=entete, follow_redirects=False,
                        data={"nom": "Chez Marcel", "ville": "Lyon", "site_url": "site.example"})
        assert r.status_code == 303
        identifiant = demos.lister()[0]["id"]
        assert "Lecture du site en cours" in client.get(r.headers["location"]).text
        asyncio.run(demos.preparer(identifiant))   # la tâche de fond, jouée à la main
        page = client.get(r.headers["location"]).text
        assert "Chez Marcel" in page and "Bistrot lyonnais" in page and "Source" in page
        assert "<b>" not in page  # échappé
        # le serveur refuse la validation sans relecture, bouton ou pas
        assert client.post(f"/admin/demos/{identifiant}/valider", headers=entete).status_code == 409
        assert client.post(f"/admin/demos/{identifiant}/relecture", headers=entete, follow_redirects=False,
                           data={"valeur_cuisine": "Bistrot lyonnais"}).status_code == 303
        assert client.post(f"/admin/demos/{identifiant}/valider", headers=entete,
                           follow_redirects=False).status_code == 303
        assert demos.obtenir(identifiant)["etat"] == "prete"
        r = client.post(f"/admin/demos/{identifiant}/effacer", headers=entete, follow_redirects=False)
        assert r.status_code == 303 and demos.obtenir(identifiant)["etat"] == "effacee"

    def test_une_saisie_refusee_ne_cree_rien(self, client, actives):
        entete = _connecter(client)
        r = client.post("/admin/demos", headers=entete, data={"nom": "A", "ville": "L", "site_url": "ftp://x"})
        assert r.status_code == 422 and "http" in r.text and demos.lister() == []

    def test_un_post_sans_csrf_est_refuse(self, client, actives):
        _connecter(client)
        assert client.post("/admin/demos", data={"nom": "A", "ville": "B", "site_url": "c.fr"}).status_code == 403

    def test_un_restaurateur_n_y_a_pas_acces(self, client, actives):
        t = tenants.create_tenant("Chez Demos Resto", "+33100000139")
        mdp = "-".join(("compte", "demos", "sans", "valeur"))
        u = users.create_user(f"resto-{t.id}@test.fr", mdp, users.ROLE_RESTAURATEUR, t.id)
        try:
            identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
            entete = _connecter(client, u.email, mdp)
            assert client.get("/admin/demos").status_code == 403
            assert client.get(f"/admin/demos/{identifiant}").status_code == 403
            assert client.post(f"/admin/demos/{identifiant}/effacer", headers=entete).status_code == 403
            assert client.post(f"/admin/demos/{identifiant}/valider", headers=entete).status_code == 403
            assert demos.obtenir(identifiant)["etat"] == "a_relire"
            assert 'href="/admin/demos"' not in client.get("/admin/").text
        finally:
            tenants.delete_tenant(t.id)

    def test_une_demonstration_inconnue_est_404(self, client, actives):
        entete = _connecter(client)
        assert client.get("/admin/demos/99999").status_code == 404
        assert client.post("/admin/demos/99999/effacer", headers=entete).status_code == 404
        assert client.post("/admin/demos/0/effacer", headers=entete).status_code == 422
