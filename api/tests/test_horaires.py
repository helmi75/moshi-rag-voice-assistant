"""Horaires d'ouverture (app/disponibilite.py) : ce que l'assistante refuse, et pourquoi.

Avant, check_availability répondait toujours « disponible » : une table un lundi de
fermeture à trois heures du matin passait. Ici : le module pur, les trois outils qui
refusent, le prompt qui cite les horaires, l'écran admin et l'alerte du parc."""
import asyncio
import json
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, disponibilite, llm, reservations, tenants, users
from app.admin import routes_dashboard
from app.main import app

APPELANT = "+33612345678"

HORAIRES = {
    "semaine": {
        "lundi": [],
        "mardi": [["12:00", "14:30"], ["19:00", "22:30"]],
        "mercredi": [["12:00", "14:30"], ["19:00", "22:30"]],
        "jeudi": [["12:00", "14:30"], ["19:00", "22:30"]],
        "vendredi": [["12:00", "14:30"], ["19:00", "22:30"]],
        "samedi": [["12:00", "14:30"], ["19:00", "22:30"]],
        "dimanche": [["12:00", "15:00"]],
    },
    "fermetures": ["2026-12-25", "2027-08-03/2027-08-17"],
}


def _prochain(jour_semaine: int) -> date:
    """Le prochain jour de la semaine STRICTEMENT à venir (0 = lundi)."""
    aujourd_hui = date.today()
    ecart = (jour_semaine - aujourd_hui.weekday()) % 7 or 7
    return aujourd_hui + timedelta(days=ecart)


def _a(jour: date, heure: str) -> datetime:
    return datetime.combine(jour, time.fromisoformat(heure), tzinfo=llm.FUSEAU)


class TestDisponibilitePure:
    H = disponibilite.charger(json.dumps(HORAIRES))

    def test_un_jour_ferme_est_refuse_avec_les_jours_ouverts(self):
        motif = disponibilite.motif_de_fermeture(self.H, _a(_prochain(0), "20:00"))
        assert "fermé le lundi" in motif.lower()
        assert "mardi" in motif  # de quoi proposer autre chose

    def test_entre_deux_services_le_motif_cite_les_plages(self):
        motif = disponibilite.motif_de_fermeture(self.H, _a(_prochain(1), "16:00"))
        assert motif and "12h00-14h30" in motif and "19h00-22h30" in motif

    def test_les_bornes_sont_incluses(self):
        mardi = _prochain(1)
        assert disponibilite.motif_de_fermeture(self.H, _a(mardi, "12:00")) is None
        assert disponibilite.motif_de_fermeture(self.H, _a(mardi, "14:30")) is None
        assert disponibilite.motif_de_fermeture(self.H, _a(mardi, "14:31")) is not None
        assert disponibilite.motif_de_fermeture(self.H, _a(mardi, "22:30")) is None

    def test_un_service_de_nuit_deborde_sur_le_lendemain(self):
        nuit = disponibilite.charger(json.dumps({"semaine": {"vendredi": [["19:00", "01:00"]]}}))
        vendredi, samedi = _prochain(4), _prochain(4) + timedelta(days=1)
        assert disponibilite.motif_de_fermeture(nuit, _a(vendredi, "23:30")) is None
        assert disponibilite.motif_de_fermeture(nuit, _a(samedi, "00:30")) is None
        assert disponibilite.motif_de_fermeture(nuit, _a(samedi, "02:00")) is not None
        assert disponibilite.motif_de_fermeture(nuit, _a(vendredi, "18:00")) is not None

    def test_une_fermeture_exceptionnelle_prime(self):
        assert "exceptionnelle" in disponibilite.motif_de_fermeture(
            self.H, datetime(2026, 12, 25, 20, 0, tzinfo=llm.FUSEAU))  # un vendredi, ouvert sinon
        assert "exceptionnelle" in disponibilite.motif_de_fermeture(
            self.H, datetime(2027, 8, 10, 12, 30, tzinfo=llm.FUSEAU))  # dans l'intervalle
        assert disponibilite.motif_de_fermeture(
            self.H, datetime(2027, 8, 18, 12, 30, tzinfo=llm.FUSEAU)) is None  # mercredi, après

    def test_non_renseigne_ne_refuse_rien(self):
        assert disponibilite.charger(None) is None
        assert disponibilite.charger("   ") is None
        assert disponibilite.motif_de_fermeture(None, _a(_prochain(0), "03:00")) is None
        assert not disponibilite.est_configure(None)

    def test_un_json_illisible_vaut_non_renseigne(self):
        """Fail-open délibéré : refuser toutes les tables sur une donnée corrompue serait
        pire. L'alerte du parc, elle, dira « non renseigné »."""
        assert disponibilite.charger("{pas du json") is None
        assert disponibilite.charger(json.dumps({"semaine": {"lundi": [["25:00", "26:00"]]}})) is None

    def test_en_toutes_lettres_groupe_les_jours_identiques(self):
        texte = disponibilite.en_toutes_lettres(self.H, aujourd_hui=date(2026, 9, 1))
        assert "Lundi : fermé." in texte
        assert "Du mardi au samedi : 12h00-14h30 et 19h00-22h30." in texte
        assert "Dimanche : 12h00-15h00." in texte
        assert "25 décembre 2026" in texte and "du mardi 3 août 2027 au mardi 17 août 2027" in texte

    def test_les_fermetures_passees_ne_sont_pas_citees(self):
        texte = disponibilite.en_toutes_lettres(self.H, aujourd_hui=date(2028, 1, 1))
        assert "Fermetures" not in texte

    def test_le_formulaire_devient_le_json(self):
        form = {"lundi_ferme": "on",
                "mardi_1_debut": "12:00", "mardi_1_fin": "14:30",
                "mardi_2_debut": "19:00", "mardi_2_fin": "22:30",
                "dimanche_1_debut": "12:00", "dimanche_1_fin": "15:00",
                "fermetures": "2026-12-25\n2027-08-17/2027-08-03\n"}
        horaires, erreurs = disponibilite.depuis_formulaire(form)
        assert erreurs == []
        assert horaires["semaine"]["mardi"] == [["12:00", "14:30"], ["19:00", "22:30"]]
        assert horaires["semaine"]["lundi"] == [] and horaires["semaine"]["mercredi"] == []
        assert horaires["fermetures"] == ["2026-12-25", "2027-08-03/2027-08-17"]

    def test_le_formulaire_refuse_une_fin_avant_le_debut(self):
        _, erreurs = disponibilite.depuis_formulaire({"mardi_1_debut": "14:00", "mardi_1_fin": "12:00"})
        assert erreurs and "précède" in erreurs[0]
        _, erreurs = disponibilite.depuis_formulaire({"mardi_1_debut": "19:00", "mardi_1_fin": "01:00"})
        assert erreurs == []  # service de nuit : légitime
        _, erreurs = disponibilite.depuis_formulaire({"mardi_1_debut": "12:00"})
        assert erreurs and "deux heures" in erreurs[0]
        _, erreurs = disponibilite.depuis_formulaire({"fermetures": "Noël"})
        assert erreurs and "Noël" in erreurs[0]


@pytest.fixture()
def resto(tmp_path, monkeypatch):
    """Base neuve, horloge à 15 h aujourd'hui (les créneaux du soir sont à venir), et un
    établissement dont les horaires sont ceux de HORAIRES."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "horaires.db"))
    db.init_db()
    quinze_heures = datetime.combine(date.today(), time(15, 0), tzinfo=llm.FUSEAU)
    monkeypatch.setattr(llm, "maintenant", lambda: quinze_heures)
    tenant = tenants.create_tenant("Chez Horaires", "+33199000555")
    return tenants.update_tenant(tenant.id, opening_hours=json.dumps(HORAIRES))


def _outil(tenant, nom, args):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, APPELANT)))


class TestLesOutilsRefusentUnCreneauFerme:
    def test_check_availability_un_lundi(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": _prochain(0).isoformat(), "time": "20:00", "party_size": 2})
        assert "fermé" in reponse["error"].lower()

    def test_create_reservation_un_lundi_n_ecrit_rien(self, resto):
        reponse = _outil(resto, "create_reservation",
                         {"customer_name": "Durand", "date": _prochain(0).isoformat(),
                          "time": "20:00", "party_size": 2})
        assert "fermé" in reponse["error"].lower()
        assert reservations.list_reservations(resto.id) == []

    def test_modify_vers_un_jour_ferme_ne_change_rien(self, resto):
        mardi = _prochain(1).isoformat()
        mienne = reservations.create_reservation(
            tenant_id=resto.id, customer_name="Dupont", date=mardi, time="20:00",
            party_size=2, customer_phone=APPELANT)
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": mienne["id"], "date": _prochain(0).isoformat()})
        assert "fermé" in reponse["error"].lower()
        assert reservations.get_reservation(mienne["id"])["date"] == mardi

    def test_entre_deux_services_egalement(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": _prochain(1).isoformat(), "time": "16:00", "party_size": 2})
        assert "19h00-22h30" in reponse["error"]

    def test_un_mardi_soir_passe(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": _prochain(1).isoformat(), "time": "20:00", "party_size": 2})
        assert reponse == {"available": True, "covers_already_booked": 0}

    def test_sans_horaires_rien_n_est_refuse(self, resto):
        libre = tenants.create_tenant("Sans Horaires", "+33199000556")
        reponse = _outil(libre, "check_availability",
                         {"date": _prochain(0).isoformat(), "time": "03:00", "party_size": 2})
        assert reponse["available"] is True


class TestPromptHoraires:
    def test_le_prompt_cite_les_horaires_quand_ils_existent(self, resto):
        prompt = llm.build_system_prompt(resto)
        assert "# Horaires de réservation" in prompt
        assert "Du mardi au samedi : 12h00-14h30 et 19h00-22h30." in prompt
        assert "l'outil le refuserait" in prompt
        # Placée avant la procédure de réservation, qui s'y réfère.
        assert prompt.index("# Horaires de réservation") < prompt.index("# Réservation — dans l'ordre")

    def test_rien_sans_horaires(self, resto):
        libre = tenants.create_tenant("Sans Horaires", "+33199000557")
        assert "# Horaires de réservation" not in llm.build_system_prompt(libre)


def _login(client, email="admin@test.local", password="test-admin-pass"):
    resp = client.post("/admin/login", data={"email": email, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def _csrf(client) -> str:
    client.get("/admin/")
    import base64

    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.b64decode(raw))["csrf"]


@pytest.fixture()
def etablissement():
    tenant = tenants.create_tenant("Horaires Admin", f"+3363{id(object()) % 10_000_000:07d}")
    yield tenant
    tenants.delete_tenant(tenant.id)


class TestAdminHoraires:
    FORM = {"lundi_ferme": "on",
            "mardi_1_debut": "12:00", "mardi_1_fin": "14:30",
            "mardi_2_debut": "19:00", "mardi_2_fin": "22:30",
            "dimanche_1_debut": "12:00", "dimanche_1_fin": "15:00",
            "fermetures": "2026-12-25"}

    def test_la_page_s_affiche_et_la_navigation_la_propose(self, etablissement):
        client = _login(TestClient(app))
        page = client.get(f"/admin/tenants/{etablissement.id}/horaires")
        assert page.status_code == 200
        assert "Horaires d'ouverture" in page.text
        assert f"/admin/tenants/{etablissement.id}/horaires" in page.text  # item de navigation
        assert "Rien de renseigné" in page.text

    def test_enregistrer_puis_relire(self, etablissement):
        client = _login(TestClient(app))
        token = _csrf(client)
        resp = client.post(f"/admin/tenants/{etablissement.id}/horaires",
                           data={**self.FORM, "csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        stocke = json.loads(tenants.get_by_id(etablissement.id).opening_hours)
        assert stocke["semaine"]["mardi"] == [["12:00", "14:30"], ["19:00", "22:30"]]
        assert stocke["semaine"]["lundi"] == []
        assert stocke["fermetures"] == ["2026-12-25"]
        page = client.get(f"/admin/tenants/{etablissement.id}/horaires").text
        assert "Mardi" in page and 'value="22:30"' in page and "Rien de renseigné" not in page

    def test_une_saisie_fausse_est_renvoyee_sans_perdre_le_reste(self, etablissement):
        client = _login(TestClient(app))
        token = _csrf(client)
        resp = client.post(f"/admin/tenants/{etablissement.id}/horaires",
                           data={**self.FORM, "jeudi_1_debut": "14:00", "jeudi_1_fin": "12:00",
                                 "csrf_token": token})
        assert resp.status_code == 422
        assert "précède" in resp.text
        assert 'value="22:30"' in resp.text  # le mardi saisi est toujours là
        assert tenants.get_by_id(etablissement.id).opening_hours is None

    def test_un_restaurateur_ne_touche_pas_aux_horaires_d_un_autre(self, etablissement):
        autre = tenants.create_tenant("Autre Resto", f"+3364{id(object()) % 10_000_000:07d}")
        try:
            compte = users.create_user(f"resto{autre.id}@test.fr", "resto-pass",
                                       users.ROLE_RESTAURATEUR, autre.id)
            client = _login(TestClient(app), compte.email, "resto-pass")
            assert client.get(f"/admin/tenants/{etablissement.id}/horaires").status_code == 403
            token = _csrf(client)
            assert client.post(f"/admin/tenants/{etablissement.id}/horaires",
                               data={**self.FORM, "csrf_token": token}).status_code == 403
            assert client.get(f"/admin/tenants/{autre.id}/horaires").status_code == 200
        finally:
            tenants.delete_tenant(autre.id)


class TestAlerteParc:
    def test_sans_horaires_le_parc_alerte(self, etablissement):
        alertes = routes_dashboard._alerts(routes_dashboard._venue_rows())
        assert any("horaires d'ouverture non renseignés" in a["title"]
                   and etablissement.name in a["title"] for a in alertes)

    def test_avec_horaires_plus_d_alerte(self, etablissement):
        tenants.update_tenant(etablissement.id, opening_hours=json.dumps(HORAIRES))
        alertes = routes_dashboard._alerts(routes_dashboard._venue_rows())
        assert not any("horaires" in a["title"] and etablissement.name in a["title"]
                       for a in alertes)
