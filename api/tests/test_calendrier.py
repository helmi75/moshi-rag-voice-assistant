"""Le calendrier des réservations (SCRUM-112) : mois, semaine, jour, dans les deux
interfaces.

Ce qui compte : les compteurs ne comptent que ce qui viendra (une annulée gonflerait la
journée), un jour fermé se voit, une réservation resOS apparaît avec son statut (« à
valider »), un resOS injoignable est SIGNALÉ au lieu de montrer une journée vide, et un
restaurateur ne voit que son carnet."""
import base64
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import connecteurs, disponibilite, reservations, tenants, users
from app.admin import calendrier
from app.connecteurs import bac_a_sable, sante
from app.main import app

# Mars 2030 : le 1er est un vendredi, le 11 un lundi, le 13 un mercredi.
MERCREDI = "2030-03-13"
LUNDI = "2030-03-11"
HORAIRES = {"semaine": {"lundi": [], "mardi": [["12:00", "14:30"], ["19:00", "22:30"]],
                        "mercredi": [["12:00", "14:30"], ["19:00", "22:30"]],
                        "jeudi": [["19:00", "22:30"]], "vendredi": [["19:00", "23:00"]],
                        "samedi": [["19:00", "23:00"]], "dimanche": [["12:00", "15:00"]]},
            "fermetures": ["2030-03-20"]}


def _client(email="admin@test.local", password="test-admin-pass"):
    client = TestClient(app)
    assert client.post("/admin/login", data={"email": email, "password": password},
                       follow_redirects=False).status_code == 303
    client.get("/admin/")
    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    client.headers["X-CSRF-Token"] = json.loads(base64.b64decode(raw))["csrf"]
    return client


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Chez Agenda", f"+3367{id(object()) % 10_000_000:07d}")
    tenants.update_tenant(tenant.id, opening_hours=json.dumps(HORAIRES))
    compte = users.create_user(f"agenda-{tenant.id}@test.fr", "resto-pass",
                               users.ROLE_RESTAURATEUR, tenant.id)
    yield tenants.get_by_id(tenant.id), compte
    tenants.delete_tenant(tenant.id)


def _resa(tenant, nom, jour=MERCREDI, heure="20:00", couverts=2):
    return reservations.create_reservation(tenant.id, nom, jour, heure, couverts,
                                           customer_phone="+33611223344")


class TestPur:
    def test_le_mois_s_affiche_en_semaines_entieres(self):
        debut, fin = calendrier.bornes("mois", date(2030, 3, 13))
        assert (debut, fin) == (date(2030, 2, 25), date(2030, 3, 31))
        assert len(calendrier.jours(debut, fin)) % 7 == 0

    def test_la_semaine_va_du_lundi_au_dimanche(self):
        assert calendrier.bornes("semaine", date(2030, 3, 13)) == (date(2030, 3, 11),
                                                                   date(2030, 3, 17))

    def test_les_fleches_changent_d_annee(self):
        assert calendrier.voisins("mois", date(2030, 1, 31)) == (date(2029, 12, 1),
                                                                date(2030, 2, 1))
        assert calendrier.voisins("jour", date(2030, 3, 1))[0] == date(2030, 2, 28)

    def test_les_titres(self):
        assert calendrier.titre("mois", date(2030, 3, 13)) == "Mars 2030"
        assert calendrier.titre("jour", date(2030, 3, 13)) == "Mercredi 13 mars 2030"
        assert calendrier.titre("semaine", date(2030, 3, 13)) == "Du 11 au 17 mars 2030"
        assert calendrier.titre("semaine", date(2030, 4, 1)) == "Du 1 au 7 avril 2030"
        assert calendrier.titre("semaine", date(2030, 12, 31)) == (
            "Du 30 décembre 2030 au 5 janvier 2031")

    def test_une_annulee_ne_compte_pas(self):
        resas = [calendrier.interne({"party_size": 4}, 1),
                 calendrier.interne({"party_size": 6, "cancelled_at": "2030-03-12"}, 1),
                 calendrier.resos({"party_size": 3, "statut": "request"}, 1),
                 calendrier.resos({"party_size": 5, "statut": "declined"}, 1)]
        assert calendrier.resume(resas) == {"reservations": 2, "couverts": 7,
                                            "a_valider": 1, "annulees": 2}

    def test_les_statuts_resos(self):
        assert calendrier.resos({"statut": "request"}, 1)["libelle"] == "À valider"
        assert calendrier.resos({"statut": "approved"}, 1)["etat"] == "confirmee"
        assert calendrier.resos({"statut": "no_show"}, 1)["etat"] == "annulee"
        assert calendrier.resos({"statut": "approved"}, 1)["modifiable"] is False

    def test_aucune_reservation_ne_sort_de_la_grille(self):
        heures = calendrier.heures([{"time": "08:30"}, {"time": "00:15"}])
        assert heures[0] == 0 and heures[-1] == calendrier.HEURE_MAX
        assert calendrier.heures([]) == list(range(calendrier.HEURE_MIN, calendrier.HEURE_MAX + 1))

    def test_jour_ferme(self):
        horaires = disponibilite.charger(json.dumps(HORAIRES))
        assert disponibilite.ferme_le(horaires, date(2030, 3, 11))       # lundi
        assert disponibilite.ferme_le(horaires, date(2030, 3, 20))       # exceptionnel
        assert not disponibilite.ferme_le(horaires, date(2030, 3, 13))
        assert not disponibilite.ferme_le(None, date(2030, 3, 11))       # non renseigné


class TestCarnetInterne:
    def test_entre_bornes_incluses_annulees_comprises(self, resto):
        tenant, _ = resto
        _resa(tenant, "Avant", jour="2030-03-10")
        dedans = _resa(tenant, "Dedans", jour="2030-03-11")
        annulee = _resa(tenant, "Annulée", jour="2030-03-17")
        reservations.cancel_reservation(annulee["id"])
        _resa(tenant, "Après", jour="2030-03-18")
        noms = [r["customer_name"] for r in
                reservations.entre("2030-03-11", "2030-03-17", [tenant.id])]
        assert noms == ["Dedans", "Annulée"] and dedans["id"]
        assert reservations.entre("2030-03-11", "2030-03-17", []) == []


class TestVues:
    def test_le_mois_compte_les_reservations_et_les_couverts(self, resto):
        tenant, compte = resto
        _resa(tenant, "Dupont", couverts=4)
        _resa(tenant, "Martin", heure="12:30", couverts=3)
        reservations.cancel_reservation(_resa(tenant, "Absent", couverts=8)["id"])
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=mois&jour={MERCREDI}").text
        assert "Mars 2030" in page
        assert "mercredi 13 mars 2030 : 2 réservation(s), 7 couvert(s)" in page
        assert f"vue=jour&amp;jour={MERCREDI}" in page   # un clic ouvre le jour

    def test_les_jours_fermes_sont_grises(self, resto):
        tenant, compte = resto
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=mois&jour={MERCREDI}").text
        assert "lundi 11 mars 2030, fermé" in page
        assert "mercredi 20 mars 2030, fermé" in page
        assert "mercredi 13 mars 2030, fermé" not in page

    def test_la_semaine_place_la_reservation_a_son_heure(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont", heure="20:15", couverts=4)
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=semaine&jour={MERCREDI}").text
        ligne = page.split("20 h</th>")[1].split("</tr>")[0]
        assert "Dupont" in ligne and f"#resa-{resa['id']}" in ligne
        assert "Dupont" not in page.split("20 h</th>")[0].split("<tbody>")[1]

    def test_le_jour_montre_la_carte_et_ses_actions(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont", couverts=4)
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=jour&jour={MERCREDI}").text
        assert f'id="resa-{resa["id"]}"' in page and "Confirmée" in page
        assert f"/admin/reservations/{resa['id']}/edit?rendu=carte" in page
        assert f"/admin/reservations/{resa['id']}/cancel" in page

    def test_un_jour_ferme_le_dit(self, resto):
        _, compte = resto
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=jour&jour={LUNDI}").text
        assert "Fermé ce jour-là" in page and "Aucune réservation ce jour-là" in page

    def test_sans_vue_choisie_le_telephone_peut_basculer_sur_le_jour(self, resto):
        _, compte = resto
        client = _client(compte.email, "resto-pass")
        assert "data-vue-implicite" in client.get("/admin/reservations").text
        assert "data-vue-implicite" not in client.get("/admin/reservations?vue=mois").text

    def test_les_anciens_liens_ouvrent_le_jour(self, resto):
        tenant, compte = resto
        _resa(tenant, "Dupont")
        page = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?date_from={MERCREDI}").text
        assert "Mercredi 13 mars 2030" in page and "Dupont" in page

    def test_la_liste_reste_disponible(self, resto):
        tenant, compte = resto
        _resa(tenant, "Dupont")
        page = _client(compte.email, "resto-pass").get("/admin/reservations?vue=liste").text
        assert "Dupont" in page and "<table>" in page


class TestDroits:
    def test_un_restaurateur_ne_voit_que_son_carnet(self, resto):
        tenant, compte = resto
        autre = tenants.create_tenant("Voisin Agenda", f"+3368{id(object()) % 10_000_000:07d}")
        try:
            _resa(tenant, "ChezMoi")
            _resa(autre, "ChezLeVoisin")
            client = _client(compte.email, "resto-pass")
            for vue in ("mois", "semaine", "jour"):
                page = client.get(f"/admin/reservations?vue={vue}&jour={MERCREDI}"
                                  f"&tenant_id={autre.id}").text
                assert "ChezLeVoisin" not in page and "Voisin Agenda" not in page
            assert "ChezMoi" in client.get(
                f"/admin/reservations?vue=jour&jour={MERCREDI}&tenant_id={autre.id}").text
        finally:
            tenants.delete_tenant(autre.id)

    def test_le_super_admin_filtre_par_etablissement(self, resto):
        tenant, _ = resto
        autre = tenants.create_tenant("Voisin Filtre", f"+3369{id(object()) % 10_000_000:07d}")
        try:
            _resa(tenant, "ChezAgenda")
            _resa(autre, "ChezFiltre")
            client = _client()
            tous = client.get(f"/admin/reservations?vue=jour&jour={MERCREDI}").text
            assert "ChezAgenda" in tous and "ChezFiltre" in tous
            assert "Voisin Filtre" in tous  # sur tout le parc, la carte dit où
            un = client.get(
                f"/admin/reservations?vue=jour&jour={MERCREDI}&tenant_id={autre.id}").text
            assert "ChezFiltre" in un and "ChezAgenda" not in un
        finally:
            tenants.delete_tenant(autre.id)

    def test_modifier_la_reservation_d_un_autre_est_interdit(self, resto):
        tenant, _ = resto
        autre = tenants.create_tenant("Voisin Droits", f"+3360{id(object()) % 10_000_000:07d}")
        try:
            compte = users.create_user(f"droits-{autre.id}@test.fr", "resto-pass",
                                       users.ROLE_RESTAURATEUR, autre.id)
            resa = _resa(tenant, "Intouchable")
            client = _client(compte.email, "resto-pass")
            assert client.get(f"/admin/reservations/{resa['id']}/edit?rendu=carte"
                              ).status_code == 403
            assert client.post(f"/admin/reservations/{resa['id']}/cancel",
                               data={"rendu": "carte"}).status_code == 403
            assert reservations.get_reservation(resa["id"])["cancelled_at"] is None
        finally:
            tenants.delete_tenant(autre.id)


class TestActionsDepuisLaCarte:
    def test_modifier_sans_deplacer_rend_la_carte(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont")
        client = _client(compte.email, "resto-pass")
        formulaire = client.get(f"/admin/reservations/{resa['id']}/edit?rendu=carte").text
        assert 'name="rendu" value="carte"' in formulaire and "<article" in formulaire
        resp = client.post(f"/admin/reservations/{resa['id']}", data={
            "customer_name": "Dupont", "date": MERCREDI, "time": "20:00", "party_size": "6",
            "rendu": "carte"})
        assert resp.status_code == 200 and "<article" in resp.text and "6 couverts" in resp.text
        assert "hx-refresh" not in resp.headers

    def test_deplacer_recharge_la_page(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont")
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/reservations/{resa['id']}", data={
                "customer_name": "Dupont", "date": "2030-03-14", "time": "20:00",
                "party_size": "2", "rendu": "carte"})
        assert resp.headers["hx-refresh"] == "true"
        assert reservations.get_reservation(resa["id"])["date"] == "2030-03-14"

    def test_une_saisie_fausse_revient_dans_la_carte(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont")
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/reservations/{resa['id']}", data={
                "customer_name": "Dupont", "date": MERCREDI, "time": "25:00",
                "party_size": "2", "rendu": "carte"})
        assert resp.status_code == 422 and "Rien n'a été enregistré" in resp.text
        assert reservations.get_reservation(resa["id"])["time"] == "20:00"

    def test_annuler_garde_la_trace(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont")
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/reservations/{resa['id']}/cancel", data={"rendu": "carte"})
        assert resp.status_code == 200 and "Annulée" in resp.text
        assert resp.headers["hx-refresh"] == "true"
        assert reservations.get_reservation(resa["id"])["cancelled_at"]

    def test_la_liste_garde_ses_lignes(self, resto):
        tenant, compte = resto
        resa = _resa(tenant, "Dupont")
        resp = _client(compte.email, "resto-pass").post(
            f"/admin/reservations/{resa['id']}/cancel")
        assert resp.text.lstrip().startswith("<tr") and "hx-refresh" not in resp.headers


class TestCarnetResos:
    @pytest.fixture()
    def demo(self):
        sante.reinitialiser()
        connecteurs.oublier_horaires()
        bac_a_sable.oublier_tout()
        tenant = tenants.create_tenant("Chez resOS", f"+3361{id(object()) % 10_000_000:07d}")
        tenants.update_tenant(tenant.id, booking_provider=connecteurs.RESOS_DEMO)
        etat = bac_a_sable.etat_pour(tenant.id)
        etat.reservations.clear()
        yield tenants.get_by_id(tenant.id), etat
        tenants.delete_tenant(tenant.id)
        # Le carnet fictif est sauvé sur disque : un établissement créé plus tard avec
        # le même identifiant le relirait, réservations comprises.
        (bac_a_sable.dossier() / f"etablissement{tenant.id}.json").unlink(missing_ok=True)
        bac_a_sable.oublier_tout()
        sante.reinitialiser()

    def test_les_reservations_resos_apparaissent_avec_leur_statut(self, demo):
        tenant, etat = demo
        etat.reserver(date=MERCREDI, time="20:00", people=4, nom="Demande", status="request")
        etat.reserver(date=MERCREDI, time="12:30", people=2, nom="Validée", status="approved")
        etat.reserver(date=MERCREDI, time="21:00", people=6, nom="Refusée", status="declined")
        client = _client()
        jour = client.get(f"/admin/reservations?vue=jour&jour={MERCREDI}"
                          f"&tenant_id={tenant.id}").text
        assert "Demande" in jour and "À valider" in jour and "Refusée" in jour
        assert "se valident, se modifient" in jour
        assert "/edit?rendu=carte" not in jour  # resOS fait foi : pas d'action ici
        mois = client.get(f"/admin/reservations?vue=mois&jour={MERCREDI}"
                          f"&tenant_id={tenant.id}").text
        assert "mercredi 13 mars 2030 : 2 réservation(s), 6 couvert(s)" in mois
        assert "1 à valider" in mois

    def test_un_mois_charge_se_lit_en_entier(self, demo):
        """Au-delà de 100 réservations, resOS pagine : une seule page montrerait des
        jours vides qui ne le sont pas."""
        tenant, etat = demo
        for n in range(130):
            etat.reservations[f"r{n:03d}"] = bac_a_sable._booking(f"r{n:03d}", {
                "date": f"2030-03-{n % 28 + 1:02d}", "time": "20:00", "people": 2,
                "status": "approved", "guest": {"name": f"Client {n}"}})
        import asyncio

        lues = asyncio.run(connecteurs.pour(tenant).entre("2030-03-01", "2030-03-31"))
        assert len(lues) == 130

    def test_resos_injoignable_est_signale(self, demo):
        tenant, etat = demo
        etat.simuler("panne")
        page = _client().get(f"/admin/reservations?vue=jour&jour={MERCREDI}"
                             f"&tenant_id={tenant.id}")
        assert page.status_code == 200
        assert "Carnet incomplet" in page.text and "Chez resOS" in page.text
