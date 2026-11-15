"""La démonstration avec la fiche Google Maps (ASSISTANTE-189) et la réponse inexploitable du
modèle (ASSISTANTE-188). Aucun réseau : gosom, le site et le modèle sont simulés."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import demos, disponibilite, fiche_maps, taches, tenants, users
from app.main import app
from gosom_faux import CSV, INTERDITS, faux_gosom
# Les doublures et fixtures de test_demos.py : le faux Internet, le faux modèle, la session.
from test_demos import (FauxModele, TEXTE, _connecter, _demo_lue, _resolution, actives,  # noqa: F401
                        client, modele, reseau)  # noqa: F401
from app import db


class TestReponseInexploitable:
    """ASSISTANTE-188 : un modèle qui raisonne dépensait tous ses jetons et rendait du vide ;
    la démonstration disait « n'a pas pu lire ce site », sans la cause ni le coût."""

    def test_une_reponse_coupee_dit_sa_cause_et_garde_jetons_et_cout(self, actives, monkeypatch, reseau):
        usage = SimpleNamespace(total_tokens=2600, cost=0.0123, reasoning_tokens=2000)
        monkeypatch.setattr(demos.llm, "get_client", lambda: FauxModele("", usage, finish_reason="length"))
        identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert "coupée par la limite de jetons" in d["message"]
        assert "2000 jetons de raisonnement" in d["message"]
        assert d["jetons"] == 2600 and d["cout_usd"] == 0.0123
        assert not any(c["trouve"] for c in d["champs"].values())

    def test_une_reponse_sans_json_dit_sa_cause(self, actives, monkeypatch, reseau):
        monkeypatch.setattr(demos.llm, "get_client", lambda: FauxModele("Désolé, je ne peux pas."))
        identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert "sans JSON" in d["message"] and d["jetons"] == 321 and d["cout_usd"] == 0.0042

    def test_le_raisonnement_est_borne(self, monkeypatch):
        faux = FauxModele("{}")
        monkeypatch.setattr(demos.llm, "get_client", lambda: faux)
        asyncio.run(demos.extraire(TEXTE))
        assert faux.kwargs["extra_body"]["reasoning"] == {"max_tokens": demos.RAISONNEMENT_MAX_JETONS}
        assert faux.kwargs["max_tokens"] == demos.MAX_JETONS > demos.RAISONNEMENT_MAX_JETONS


@pytest.fixture()
def maps(monkeypatch, actives):  # noqa: F811
    """gosom actif et simulé ; rend la liste des appels qu'il a reçus."""
    monkeypatch.setenv("GOSOM_URL", "http://gosom:8080")
    monkeypatch.setattr(fiche_maps, "PAS_SECONDES", 0)

    def brancher(**options):
        transport, appels = faux_gosom(**options)
        monkeypatch.setattr(fiche_maps, "_TRANSPORT", transport)
        return appels
    return brancher


def _tout_ce_qui_est_en_base() -> str:
    with db.get_conn() as conn:
        return json.dumps([dict(r) for r in conn.execute("SELECT * FROM demonstrations")],
                          ensure_ascii=False)


def _deux_fiches() -> str:
    entete, ligne = CSV.read_text(encoding="utf-8").split("\n", 1)
    autre = ligne.replace("Cuba Compagnie Café", "Cuba Compagnie Bastille").replace(
        "48 Bd Beaumarchais, 75011 Paris", "1 rue de la Roquette, 75011 Paris").replace(
        "https://www.cubacompagnie.com/", "")
    return entete + "\n" + ligne.rstrip("\n") + "\n" + autre.rstrip("\n") + "\n"


def _site_de_la_fiche_lisible(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolution({"www.cubacompagnie.com": ["93.184.216.34"]}))


class TestAvecLaFicheGoogleMaps:
    def test_nom_et_ville_suffisent_et_la_fiche_garde_sa_source(self, maps, monkeypatch, reseau):
        maps()
        modele(monkeypatch, {})
        identifiant = demos.creer("Cuba Compagnie", "Paris")   # pas de site saisi
        assert demos.obtenir(identifiant)["site_url"] == ""
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        adresse = d["champs"]["adresse"]
        assert adresse["valeur"] == "48 Bd Beaumarchais, 75011 Paris" and adresse["source"] == "Google Maps"
        assert adresse["citation"] == adresse["valeur"] and adresse["trouve"]
        assert d["champs"]["telephone"]["valeur"] == "06 68 19 61 61"
        assert d["champs"]["cuisine"]["valeur"] == "Restaurant cubain"
        h = d["champs"]["horaires"]
        assert h["source"] == "Google Maps" and disponibilite.charger(h["valeur"]) is not None
        assert d["champs"]["horaires_texte"]["source"] == "Google Maps"
        assert d["etat"] == "a_relire" and d["relu"] is False

    def test_aucune_donnee_de_personne_n_arrive_en_base(self, maps, monkeypatch, reseau):
        maps()
        modele(monkeypatch, {})
        identifiant = demos.creer("Cuba Compagnie", "Paris")
        asyncio.run(demos.preparer(identifiant))
        base = _tout_ce_qui_est_en_base()
        assert "48 Bd Beaumarchais" in base
        assert not any(interdit in base for interdit in INTERDITS)

    def test_le_site_de_la_fiche_comble_le_reste_sans_ecraser_maps(self, maps, monkeypatch, reseau):
        _site_de_la_fiche_lisible(monkeypatch)
        maps()
        modele(monkeypatch, {
            "adresse": {"valeur": "12 rue des Halles, Lyon", "citation": "12 rue des Halles, Lyon"},
            "paiement": {"valeur": "Bistrot lyonnais", "citation": "Bistrot lyonnais"}})
        identifiant = demos.creer("Cuba Compagnie", "Paris")
        asyncio.run(demos.preparer(identifiant))
        champs = demos.obtenir(identifiant)["champs"]
        assert champs["adresse"]["valeur"] == "48 Bd Beaumarchais, 75011 Paris"   # Maps n'est pas écrasé
        assert champs["adresse"]["source"] == "Google Maps"
        assert champs["paiement"]["valeur"] == "Bistrot lyonnais"
        assert champs["paiement"]["source"] == demos.SOURCE_SITE

    def test_gosom_injoignable_se_replie_sur_le_site_saisi(self, maps, monkeypatch, reseau):
        maps(telechargement=500)
        modele(monkeypatch, {"cuisine": {"valeur": "Bistrot lyonnais", "citation": "Bistrot lyonnais"}})
        identifiant = demos.creer("Chez Marcel", "Lyon", "site.example")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert "Google Maps" in d["message"]
        assert d["champs"]["cuisine"]["trouve"] and d["champs"]["cuisine"]["source"] == demos.SOURCE_SITE

    def test_rien_trouve_et_pas_de_site_demande_l_adresse_du_site(self, maps, reseau):
        maps(csv_texte=CSV.read_text(encoding="utf-8").split("\n", 1)[0] + "\n")
        identifiant = demos.creer("Introuvable", "Lyon")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert d["etat"] == "a_relire" and "Aucune fiche Google Maps" in d["message"]
        assert "adresse du site" in d["message"] and not any(c["trouve"] for c in d["champs"].values())

    def test_sans_gosom_rien_ne_change(self, actives, monkeypatch, reseau):  # noqa: F811
        monkeypatch.setenv("GOSOM_URL", "")
        d = demos.obtenir(_demo_lue(monkeypatch, reseau))
        assert d["champs"]["adresse"]["trouve"] and "Google Maps" not in d["message"]

    def test_plusieurs_fiches_attendent_le_choix_de_helmi(self, maps, monkeypatch, reseau):
        maps(csv_texte=_deux_fiches())
        modele(monkeypatch, {})
        identifiant = demos.creer("Cuba Compagnie", "Paris")
        asyncio.run(demos.preparer(identifiant))
        d = demos.obtenir(identifiant)
        assert d["lecture"] == "choix" and len(d["choix"]) == 2 and not demos.lecture_en_cours(d)
        assert not any(c["trouve"] for c in d["champs"].values())
        with pytest.raises(demos.Refus):
            demos.choisir(identifiant, 7)
        demos.choisir(identifiant, 1)
        assert demos.lecture_en_cours(demos.obtenir(identifiant))
        with pytest.raises(demos.Refus):   # second clic
            demos.choisir(identifiant, 1)
        asyncio.run(demos.preparer_apres_choix(identifiant))
        d = demos.obtenir(identifiant)
        assert d["lecture"] == "terminee" and d["choix"] == []
        assert d["champs"]["adresse"]["valeur"] == "1 rue de la Roquette, 75011 Paris"
        assert "n'indique pas de site" in d["message"]   # la fiche choisie n'a pas de site

    def test_choisir_sans_choix_en_attente_est_refuse(self, actives):  # noqa: F811
        identifiant = demos.creer("A", "B", "site.example")
        with pytest.raises(demos.Refus):
            demos.choisir(identifiant, 0)
        with pytest.raises(demos.DemoIntrouvable):
            demos.choisir(99999, 0)


class TestLeChoixDeFicheParLesPages:
    def test_l_ecran_de_choix_puis_la_reprise(self, client, maps, monkeypatch, reseau):  # noqa: F811
        maps(csv_texte=_deux_fiches())
        modele(monkeypatch, {})
        monkeypatch.setattr(taches, "lancer", lambda coro, nom: coro.close())
        entete = _connecter(client)
        r = client.post("/admin/demos", headers=entete, follow_redirects=False,
                        data={"nom": "Cuba Compagnie", "ville": "Paris", "site_url": ""})
        assert r.status_code == 303
        identifiant = demos.lister()[0]["id"]
        asyncio.run(demos.preparer(identifiant))
        page = client.get(r.headers["location"]).text
        assert "Quelle fiche Google Maps" in page and "Cuba Compagnie Bastille" in page
        assert "1 rue de la Roquette" in page or "48 Bd Beaumarchais, 75011 Paris" in page
        assert "Enregistrer la relecture" not in page
        assert client.post(f"/admin/demos/{identifiant}/choisir", headers=entete,
                           data={"fiche": "abc"}).status_code == 422
        assert client.post(f"/admin/demos/{identifiant}/choisir", headers=entete,
                           data={"fiche": "9"}).status_code == 409
        assert client.post(f"/admin/demos/{identifiant}/choisir", headers=entete, follow_redirects=False,
                           data={"fiche": "0"}).status_code == 303
        assert demos.lecture_en_cours(demos.obtenir(identifiant))

    def test_le_choix_exige_le_jeton_csrf_et_le_super_admin(self, client, maps, monkeypatch, reseau):  # noqa: F811
        maps(csv_texte=_deux_fiches())
        modele(monkeypatch, {})
        identifiant = demos.creer("Cuba Compagnie", "Paris")
        asyncio.run(demos.preparer(identifiant))
        _connecter(client)
        assert client.post(f"/admin/demos/{identifiant}/choisir", data={"fiche": "0"}).status_code == 403
        t = tenants.create_tenant("Chez Choix", "+33100000189")
        mdp = "-".join(("compte", "choix", "sans", "valeur"))
        u = users.create_user(f"resto-{t.id}@test.fr", mdp, users.ROLE_RESTAURATEUR, t.id)
        try:
            autre = TestClient(app)
            entete = _connecter(autre, u.email, mdp)
            assert autre.post(f"/admin/demos/{identifiant}/choisir", headers=entete,
                              data={"fiche": "0"}).status_code == 403
            assert demos.obtenir(identifiant)["lecture"] == "choix"
        finally:
            tenants.delete_tenant(t.id)
