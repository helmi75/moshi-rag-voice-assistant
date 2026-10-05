"""Le bac à sable resOS, les horaires lus dans resOS, et la panne de resOS (SCRUM-86, 87, 93).

Trois promesses :
- en bac à sable, on appelle le vrai numéro et la réservation arrive dans un carnet qui
  se comporte comme resOS — et qui survit à un redéploiement ;
- pour un carnet resOS, les horaires viennent de resOS, sans jamais faire attendre un
  appelant ;
- une panne de resOS se VOIT : supervision, e-mail au restaurateur, et pas quatre
  blancs de 4 s dans le même appel.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest

from app import connecteurs, db, horloge, llm, supervision, tenants
from app.connecteurs import bac_a_sable, resos, sante
from app.connecteurs.bac_a_sable import CLE_DE_TEST, Etat, creer_app

APPELANT = "+33612345678"


def _jour(iso_semaine: int) -> str:
    """La prochaine date (au moins demain) qui tombe ce jour de la semaine (1 = lundi)."""
    jour = horloge.aujourd_hui() + timedelta(days=1)
    while jour.isoweekday() != iso_semaine:
        jour += timedelta(days=1)
    return jour.isoformat()


def _date_lisible(iso: str) -> str:
    from app.admin.routes_resos import _date_courte

    return _date_courte(iso)


def _remise_a_zero():
    sante.reinitialiser()
    connecteurs.oublier_horaires()
    bac_a_sable.oublier_tout()


@pytest.fixture()
def base(tmp_path):
    _remise_a_zero()
    with patch.object(db, "DB_PATH", str(tmp_path / "carnet.db")):
        db.init_db()
        yield tmp_path
    _remise_a_zero()


def _resto(mode, numero="+33199000901"):
    resto = tenants.create_tenant(f"Chez {mode}", numero)
    return tenants.update_tenant(resto.id, booking_provider=mode)


@pytest.fixture()
def demo(base):
    return _resto(connecteurs.RESOS_DEMO)


@pytest.fixture()
def reel(base, monkeypatch):
    """Un « vrai » resOS : le connecteur réseau, branché sur un faux par le transport."""
    etat = Etat()
    monkeypatch.setattr(resos, "_transport", httpx.ASGITransport(app=creer_app(etat)))
    monkeypatch.setenv("RESOS_API_URL", "http://resos-reel/v1")
    resto = _resto(connecteurs.RESOS, "+33199000902")
    monkeypatch.setenv("RESOS_API_KEYS", f"{resto.id}={CLE_DE_TEST}")
    resto.faux = etat
    return resto


def _outil(tenant, nom, args, numero=APPELANT):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, numero)))


def _creer(tenant, jour=None, heure="20:00"):
    return _outil(tenant, "create_reservation", {
        "customer_name": "Dupont", "date": jour or _jour(3), "time": heure, "party_size": 2})


class TestLeBacASable:
    def test_la_reservation_arrive_dans_le_carnet_fictif(self, demo):
        reponse = _creer(demo)
        assert reponse["status"] == "pending_restaurant_approval"
        (booking,) = bac_a_sable.etat_pour(demo.id).reservations.values()
        assert booking["status"] == "request" and booking["source"] == "phone"

    def test_sans_cle_ni_reseau(self, demo, monkeypatch):
        monkeypatch.delenv("RESOS_API_KEYS", raising=False)
        monkeypatch.setenv("RESOS_API_URL", "http://127.0.0.1:9/v1")  # jamais utilisé
        assert _creer(demo)["status"] == "pending_restaurant_approval"

    def test_le_carnet_survit_a_un_redeploiement(self, demo):
        reference = _creer(demo)["reservation_id"]
        bac_a_sable.oublier_tout()  # le processus redémarre
        assert reference in bac_a_sable.etat_pour(demo.id).reservations

    def test_une_panne_simulee_ne_survit_pas(self, demo):
        etat = bac_a_sable.etat_pour(demo.id)
        etat.en_panne = True
        etat.sauver()
        bac_a_sable.oublier_tout()
        assert bac_a_sable.etat_pour(demo.id).en_panne is False

    def test_un_nom_corrige_part_en_note_au_restaurant(self, demo):
        """resOS fait foi et le nom ne s'y change pas par cette API : le restaurant le
        lit en note, et aucune seconde demande n'est créée (appel 240, 05/10/2026)."""
        reference = _creer(demo)["reservation_id"]
        assert "error" in _creer(demo)                      # même numéro, même créneau
        reponse = _outil(demo, "modify_reservation",
                         {"reservation_id": reference, "customer_name": "Dupond"})
        assert reponse["status"] == "modified"
        etat = bac_a_sable.etat_pour(demo.id)
        assert len(etat.reservations) == 1
        assert [texte for _, texte in etat.notes] == ["Nom corrigé par le client : Dupond"]

    def test_chaque_etablissement_a_son_carnet(self, demo):
        autre = _resto(connecteurs.RESOS_DEMO, "+33199000903")
        _creer(demo)
        assert bac_a_sable.etat_pour(autre.id).reservations == {}


class TestLesHorairesViennentDeResos:
    """SCRUM-86 : c'est resOS qui fait foi, pas une saisie chez nous qui divergerait."""

    def test_conversion_de_l_exemple_de_la_doc(self):
        horaires = resos.horaires_depuis_resos([
            {"day": 1, "open": 1200, "close": 1645, "special": False},
            {"day": 2, "open": 1000, "close": 2200, "special": False},
            {"day": 2, "open": 1000, "close": 2200, "special": False},  # doublon
            {"day": 5, "open": 1900, "close": 2300, "special": True},   # ignorée
            {"day": 9, "open": 1200, "close": 1400},                    # illisible
        ])
        assert horaires["semaine"]["lundi"] == [["12:00", "16:45"]]
        assert horaires["semaine"]["mardi"] == [["10:00", "22:00"]]
        assert horaires["semaine"]["vendredi"] == []

    def test_jamais_d_attente_au_decroche(self, demo, monkeypatch):
        """Première lecture : l'établissement tel quel, et un rafraîchissement lancé en
        fond — pas une requête pendant que l'appelant attend « Bonjour »."""
        lances = []
        monkeypatch.setattr("app.taches.lancer",
                            lambda coro, nom: (coro.close(), lances.append(nom)))
        assert connecteurs.avec_horaires_du_carnet(demo) is demo
        assert lances and "horaires" in lances[0]

    def test_le_prompt_et_les_refus_suivent_resos(self, demo):
        etat = bac_a_sable.etat_pour(demo.id)
        etat.jours_fermes = {1}  # lundi fermé dans resOS
        asyncio.run(connecteurs.rafraichir_horaires(demo))
        vu = connecteurs.avec_horaires_du_carnet(demo)
        prompt = llm.build_system_prompt(vu)
        assert "# Horaires de réservation" in prompt and "19h00" in prompt
        reponse = _outil(vu, "create_reservation", {
            "customer_name": "Dupont", "date": _jour(1), "time": "20:00", "party_size": 2})
        assert "error" in reponse
        assert etat.reservations == {}

    def test_un_changement_dans_resos_est_suivi_apres_expiration(self, demo, monkeypatch):
        asyncio.run(connecteurs.rafraichir_horaires(demo))
        bac_a_sable.etat_pour(demo.id).services = [["18:00", "21:00"]]
        monkeypatch.setattr(connecteurs, "HORAIRES_CACHE_SECONDES", 0.0)
        monkeypatch.setattr("app.taches.lancer", lambda coro, nom: asyncio.run(coro))
        connecteurs.avec_horaires_du_carnet(demo)  # expiré : relu
        assert "18:00" in connecteurs.horaires_en_cache(demo)

    def test_notre_carnet_garde_ses_horaires(self, base):
        interne = tenants.create_tenant("Chez Nous", "+33199000904")
        assert connecteurs.avec_horaires_du_carnet(interne) is interne

    def test_une_panne_garde_l_ancienne_copie(self, demo):
        premiere = asyncio.run(connecteurs.rafraichir_horaires(demo))
        bac_a_sable.etat_pour(demo.id).en_panne = True
        sante.rouvrir(demo.id)
        assert asyncio.run(connecteurs.rafraichir_horaires(demo)) == premiere


class TestLaPanneDeResos:
    """SCRUM-87 : on ne ment jamais, et ça se voit."""

    def test_coupe_circuit_pas_de_second_blanc_dans_l_appel(self, reel):
        reel.faux.en_panne = True
        assert "error" in _creer(reel)
        requetes = len(reel.faux.requetes)
        assert "error" in _outil(reel, "find_reservation", {})
        assert len(reel.faux.requetes) == requetes, "un second essai = un second blanc"
        reel.faux.en_panne = False
        sante.rouvrir(reel.id)
        assert _creer(reel)["status"] == "pending_restaurant_approval"

    def test_la_supervision_passe_en_panne_puis_revient(self, reel):
        reel.faux.en_panne = True
        _creer(reel)
        controle = _controle()
        assert controle["niveau"] == supervision.PANNE
        assert "Chez resos" in controle["detail"]
        reel.faux.en_panne = False
        sante.rouvrir(reel.id)
        _creer(reel)
        assert _controle()["niveau"] == supervision.OK

    def test_l_etat_survit_au_redemarrage(self, reel):
        reel.faux.en_panne = True
        _creer(reel)
        sante.reinitialiser()  # le processus redémarre : la mémoire est vide
        assert _controle()["niveau"] == supervision.PANNE

    def test_une_cle_absente_est_une_panne(self, reel, monkeypatch):
        monkeypatch.setenv("RESOS_API_KEYS", "")
        controle = _controle()
        assert controle["niveau"] == supervision.PANNE
        assert "clé API absente" in controle["detail"]

    def test_une_panne_simulee_n_alerte_pas_la_production(self, demo):
        bac_a_sable.etat_pour(demo.id).en_panne = True
        _creer(demo)
        assert _controle()["niveau"] == supervision.ATTENTION

    def test_sans_resos_rien_a_surveiller(self, base):
        tenants.create_tenant("Chez Nous", "+33199000905")
        controle = _controle()
        assert controle["niveau"] == supervision.OK and "Sans objet" in controle["resume"]

    def test_le_restaurateur_est_prevenu_une_fois(self, reel):
        reel.faux.en_panne = True
        with patch("app.notifications.planifier") as planifier:
            _creer(reel)
            _outil(reel, "find_reservation", {})
            _creer(reel)
        evenements = [appel.args[1] for appel in planifier.call_args_list]
        assert evenements == ["carnet_injoignable"], evenements

    def test_le_texte_de_l_alerte(self, base):
        from app import notifications

        resto = tenants.create_tenant("Chez Alerte", "+33199000906")
        sujet, corps = notifications.sujet_et_corps(
            "carnet_injoignable", resto, {"erreur": "HTTP 503"})
        assert "resOS ne répond pas" in sujet
        assert "AUCUNE réservation" in corps and "HTTP 503" in corps


def _controle():
    supervision.vider_cache()
    for controle in supervision.etat(force=True)["controles"]:
        if controle["cle"] == "carnets":
            return controle
    raise AssertionError("contrôle « carnets » absent")


class TestLaPageCarnetResos:
    """SCRUM-93 : suivre les tests en direct, et jouer le restaurant en bac à sable."""

    @pytest.fixture(autouse=True)
    def _application(self):
        from app.main import app

        _remise_a_zero()
        yield app
        _remise_a_zero()

    def _connecte(self, email="admin@test.local", mot_de_passe="test-admin-pass"):
        import base64

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        assert client.post("/admin/login", data={"email": email, "password": mot_de_passe},
                           follow_redirects=False).status_code == 303
        client.get("/admin/")
        brut = client.cookies.get("session").split(".")[0]
        brut += "=" * (-len(brut) % 4)
        client.headers["X-CSRF-Token"] = json.loads(base64.b64decode(brut))["csrf"]
        return client

    @pytest.fixture()
    def resto_demo(self):
        # Le faux resOS garde son état sur disque : sans l'effacer, la réservation d'un
        # test attend le suivant, qui la « recrée » — ce que le serveur refuse désormais.
        resto = _resto(connecteurs.RESOS_DEMO, "+33199000907")
        fichier = bac_a_sable.dossier() / f"etablissement{resto.id}.json"
        fichier.unlink(missing_ok=True)
        bac_a_sable.oublier_tout()
        yield resto
        tenants.delete_tenant(resto.id)
        fichier.unlink(missing_ok=True)
        bac_a_sable.oublier_tout()

    def test_la_demande_apparait_et_se_valide(self, resto_demo):
        client = self._connecte()
        page = client.get(f"/admin/tenants/{resto_demo.id}/resos")
        assert page.status_code == 200 and "Bac à sable" in page.text
        _creer(resto_demo)
        direct = client.get(f"/admin/tenants/{resto_demo.id}/resos/direct").text
        assert "Dupont" in direct and "À valider" in direct
        (reference,) = bac_a_sable.etat_pour(resto_demo.id).reservations
        client.post(f"/admin/tenants/{resto_demo.id}/resos/demandes/{reference}/valider")
        assert bac_a_sable.etat_pour(resto_demo.id).reservations[reference]["status"] == "approved"

    def test_le_journal_montre_ce_qu_a_fait_l_assistante(self, resto_demo):
        client = self._connecte()
        _creer(resto_demo)
        direct = client.get(f"/admin/tenants/{resto_demo.id}/resos/direct").text
        assert "POST /bookings" in direct and "GET /bookingFlow/times" in direct

    def test_simuler_une_panne_puis_la_lever(self, resto_demo):
        client = self._connecte()
        client.post(f"/admin/tenants/{resto_demo.id}/resos/simulation",
                    data={"action": "panne"}, follow_redirects=False)
        reponse = _creer(resto_demo)
        assert "N'annonce RIEN" in reponse["error"]
        page = client.get(f"/admin/tenants/{resto_demo.id}/resos").text
        assert "Panne simulée en cours" in page
        client.post(f"/admin/tenants/{resto_demo.id}/resos/simulation",
                    data={"action": "arreter"}, follow_redirects=False)
        assert _creer(resto_demo)["status"] == "pending_restaurant_approval", (
            "arrêter la simulation doit rouvrir le circuit tout de suite")

    def test_chaque_clic_dit_ce_qui_a_ete_enregistre(self, resto_demo):
        """Demande de Helmi (27/09) : « quand je clique et que ça s'enregistre, mets-moi
        quelque part l'état du truc ». Relu dans l'état, pas recopié du formulaire."""
        client = self._connecte()
        url = f"/admin/tenants/{resto_demo.id}/resos"
        page = client.post(f"{url}/simulation", data={"action": "lent"}).text
        assert "État de resOS enregistré à" in page and "resOS lent" in page
        page = client.post(f"{url}/simulation", data={"action": "arreter"}).text
        assert "resOS fonctionne normalement." in page
        page = client.post(f"{url}/bac", data={
            "capacite": "2", "services": "19:00-22:00", "fermes": "", "jour_1": "on"}).text
        assert "Restaurant enregistré à" in page
        assert "2 table(s) par créneau · services 19:00-22:00 · fermé le lundi." in page
        assert "Restaurant enregistré à" not in client.get(url).text, (
            "la confirmation ne s'affiche qu'une fois")

    def test_les_reglages_ne_touchent_jamais_a_la_simulation(self, resto_demo):
        """27/09/2026 : la case « lent », mêlée aux réglages, était renvoyée cochée à
        chaque « Enregistrer » — deux jours de tests en échec sans que rien ne le montre."""
        client = self._connecte()
        bac = bac_a_sable.etat_pour(resto_demo.id)
        reglages = {"capacite": "3", "services": "12:00-14:00, 19:00-22:00", "fermes": ""}
        client.post(f"/admin/tenants/{resto_demo.id}/resos/bac",
                    data={**reglages, "lent": "on", "en_panne": "on"}, follow_redirects=False)
        assert bac.simulation() is None
        bac.simuler("panne")
        client.post(f"/admin/tenants/{resto_demo.id}/resos/bac", data=reglages,
                    follow_redirects=False)
        assert bac.simulation()["quoi"] == "panne"

    def test_une_simulation_s_arrete_d_elle_meme(self, resto_demo, monkeypatch):
        bac = bac_a_sable.etat_pour(resto_demo.id)
        bac.simuler("lent")
        maintenant = bac_a_sable.time.monotonic()
        monkeypatch.setattr(bac_a_sable.time, "monotonic",
                            lambda: maintenant + bac_a_sable.SIMULATION_MINUTES * 60 + 1)
        assert bac.simulation() is None
        assert bac.retard == 0 and bac.en_panne is False

    def test_la_page_reste_lisible_pendant_une_panne_simulee(self, resto_demo):
        client = self._connecte()
        bac = bac_a_sable.etat_pour(resto_demo.id)
        bac.reserver(date=_jour(4), time="20:00", nom="Pendant-la-panne", status="request")
        bac.simuler("lent")
        direct = client.get(f"/admin/tenants/{resto_demo.id}/resos/direct").text
        assert "Pendant-la-panne" in direct and "Carnet illisible" not in direct
        assert _date_lisible(_jour(4)) in direct
        assert "Panne simulée" in direct

    def test_l_etat_reel_des_cases_se_voit(self, resto_demo):
        client = self._connecte()
        bac_a_sable.etat_pour(resto_demo.id).jours_fermes = {1, 6}
        page = client.get(f"/admin/tenants/{resto_demo.id}/resos").text
        assert "fermé le <b>lundi, samedi</b>" in page

    def test_les_fichiers_statiques_portent_leur_empreinte(self, resto_demo):
        """27/09/2026 : la correction des cases était en ligne, la capture de Helmi
        montrait encore l'ancienne page — le navigateur gardait l'ancienne feuille de
        style. L'adresse change avec le contenu : un correctif atteint le navigateur."""
        from app.admin import deps

        page = self._connecte().get(f"/admin/tenants/{resto_demo.id}/resos").text
        for nom in ("pico.min.css", "admin.css", "htmx.min.js", "admin.js"):
            assert f'/admin/static/{nom}?v=' in page, nom
        attendu = deps._empreinte("admin.css")
        assert len(attendu) == 10 and f"admin.css?v={attendu}" in page

    def test_une_case_a_cocher_est_carree(self):
        """Le rayon du thème (pensé pour les boutons en pilule) rendait les cases rondes :
        on les prenait pour des boutons radio (27/09/2026, jours fermés)."""
        import pathlib
        import re

        css = (pathlib.Path(__file__).resolve().parents[1] / "app" / "admin" / "static"
               / "admin.css").read_text(encoding="utf-8")
        assert re.search(r'input\[type="checkbox"\]:not\(\[role="switch"\]\)\s*\{\s*border-radius:\s*\.3rem', css)

    def test_une_case_cochee_garde_sa_coche(self):
        """Le `background: … !important` imposé à tous les champs effaçait la coche des
        cases (le fond porte l'icône) : un état invisible, sur Carnet resOS et Horaires."""
        import pathlib
        import re

        css = (pathlib.Path(__file__).resolve().parents[1] / "app" / "admin" / "static"
               / "admin.css").read_text(encoding="utf-8")
        # Sans les commentaires : placé juste avant la règle, le commentaire devenait le
        # début du « sélecteur » lu ici, et ce test ne voyait plus rien (le garde-fou de
        # mutation l'a prouvé).
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        vues = 0
        for regle in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            selecteurs, corps = regle.group(1), regle.group(2)
            if "background" in corps and "!important" in corps:
                for selecteur in selecteurs.split(","):
                    selecteur = selecteur.strip()
                    if selecteur.startswith("input") and ":focus" not in selecteur:
                        vues += 1
                        assert 'not([type="checkbox"])' in selecteur, selecteur
        assert vues, "aucune règle de champ trouvée : ce test ne vérifie plus rien"

    def test_des_reglages_illisibles_sont_refuses(self, resto_demo):
        client = self._connecte()
        reponse = client.post(f"/admin/tenants/{resto_demo.id}/resos/bac",
                              data={"capacite": "3", "services": "midi", "fermes": ""})
        assert reponse.status_code == 422

    def test_le_restaurateur_ne_joue_pas_le_restaurant(self, resto_demo):
        from app import users

        users.create_user(f"resto{resto_demo.id}@test.fr", "resto-pass-carnet",
                          users.ROLE_RESTAURATEUR, resto_demo.id)
        client = self._connecte(f"resto{resto_demo.id}@test.fr", "resto-pass-carnet")
        assert client.get(f"/admin/tenants/{resto_demo.id}/resos").status_code == 200
        reponse = client.post(f"/admin/tenants/{resto_demo.id}/resos/bac",
                              data={"capacite": "0", "services": "12:00-14:00"})
        assert reponse.status_code == 403
        assert bac_a_sable.etat_pour(resto_demo.id).capacite == 3

    def test_un_autre_restaurateur_ne_voit_rien(self, resto_demo):
        from app import users

        autre = tenants.create_tenant("Chez Voisin", "+33199000908")
        try:
            users.create_user(f"voisin{autre.id}@test.fr", "voisin-pass-carnet",
                              users.ROLE_RESTAURATEUR, autre.id)
            client = self._connecte(f"voisin{autre.id}@test.fr", "voisin-pass-carnet")
            assert client.get(f"/admin/tenants/{resto_demo.id}/resos").status_code == 403
        finally:
            tenants.delete_tenant(autre.id)

    def test_les_horaires_resos_ne_se_saisissent_pas_ici(self, resto_demo):
        client = self._connecte()
        page = client.get(f"/admin/tenants/{resto_demo.id}/horaires").text
        assert "Horaires lus dans resOS" in page
        reponse = client.post(f"/admin/tenants/{resto_demo.id}/horaires",
                              data={"lundi_ferme": "on"})
        assert reponse.status_code == 409
        assert tenants.get_by_id(resto_demo.id).opening_hours is None
