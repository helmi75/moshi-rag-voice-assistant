"""La fiche d'une réservation (ASSISTANTE-116) : un clic sur une carte de la vue du jour
ouvre la réservation, son client et la conversation qui l'a prise.

Ce qui compte : on y retrouve bien la conversation (transcription) de l'appel qui a pris
la réservation, l'historique du client, une réservation sans appel ne donne pas une page
vide, resOS marche aussi — et un restaurateur ne lit JAMAIS la réservation d'un autre,
même en mettant son propre établissement dans l'adresse."""
import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import calls, connecteurs, reservations, tenants, users
from app.connecteurs import bac_a_sable, sante
from app.main import app

JOUR = "2030-03-13"
CLIENT = "+33611223344"


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
    tenant = tenants.create_tenant("Chez Fiche", f"+3368{id(object()) % 10_000_000:07d}")
    compte = users.create_user(f"fiche-{tenant.id}@test.fr", "resto-pass",
                               users.ROLE_RESTAURATEUR, tenant.id)
    yield tenant, compte
    tenants.delete_tenant(tenant.id)


def _appel(tenant, reservation_id, *, numero=CLIENT, phrase="Une table pour deux, mercredi."):
    sid = f"CA{uuid.uuid4().hex[:30]}"
    calls.start_call(sid, tenant.id, numero)
    return calls.finish_call(sid, "completed", [
        {"role": "user", "content": phrase},
        {"role": "assistant", "content": "C'est noté pour mercredi à 20 heures."}],
        reservation_id=reservation_id)


def _fiche(tenant, ref) -> str:
    return f"/admin/tenants/{tenant.id}/reservations/{ref}"


class TestLaCarteMeneALaFiche:
    def test_la_carte_du_jour_porte_le_lien(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2,
                                               customer_phone=CLIENT)
        jour = _client(compte.email, "resto-pass").get(
            f"/admin/reservations?vue=jour&jour={JOUR}").text
        assert f'class="cal-lien" href="{_fiche(tenant, resa["id"])}"' in jour


class TestLaFiche:
    def test_la_conversation_et_le_client(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2,
                                               customer_phone=CLIENT, notes="Terrasse")
        call_id = _appel(tenant, resa["id"])
        ancienne = reservations.create_reservation(tenant.id, "Durand", "2030-01-08",
                                                   "12:30", 4, customer_phone=CLIENT)
        page = _client(compte.email, "resto-pass").get(_fiche(tenant, resa["id"]))
        assert page.status_code == 200
        texte = page.text.replace("&#39;", "'")
        # La conversation : la transcription de l'appel qui l'a prise.
        assert "Une table pour deux, mercredi." in texte
        assert "Prise au téléphone par l'assistante" in texte
        # Le client : son numéro cliquable, ses autres réservations, ses appels.
        assert f'href="tel:{CLIENT}"' in texte
        assert _fiche(tenant, ancienne["id"]) in texte and "08/01/2030" in texte
        assert f'href="/admin/calls/{call_id}" aria-current="true"' in texte
        assert "Mercredi 13 mars 2030" in texte and "Terrasse" in texte
        # La réservation n'est pas répétée dans l'encadré de l'appel.
        assert "Créé pendant cet appel" not in texte

    def test_sans_appel_la_fiche_le_dit(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Martin", JOUR, "19:30", 3)
        texte = _client(compte.email, "resto-pass").get(
            _fiche(tenant, resa["id"])).text.replace("&#39;", "'")
        assert "Aucun appel rattaché" in texte and "saisie à la main" in texte
        assert "Numéro non communiqué" in texte
        assert "Sans numéro, ses autres réservations ne se retrouvent pas." in texte

    def test_une_premiere_reservation(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Petit", JOUR, "19:30", 3,
                                               customer_phone="+33699887766")
        texte = _client(compte.email, "resto-pass").get(
            _fiche(tenant, resa["id"])).text.replace("&#39;", "'")
        assert "c'est sa première réservation chez vous" in texte

    def test_une_reservation_inconnue(self, resto):
        tenant, compte = resto
        client = _client(compte.email, "resto-pass")
        assert client.get(_fiche(tenant, 99_999_999)).status_code == 404
        assert client.get(_fiche(tenant, "pas-un-nombre")).status_code == 404


class TestUnRestaurateurChezLui:
    def test_ni_par_l_etablissement_d_un_autre(self, resto):
        tenant, _ = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2)
        autre = tenants.create_tenant("Voisin Fiche", f"+3369{id(object()) % 10_000_000:07d}")
        try:
            compte = users.create_user(f"voisin-fiche-{autre.id}@test.fr", "resto-pass",
                                       users.ROLE_RESTAURATEUR, autre.id)
            client = _client(compte.email, "resto-pass")
            assert client.get(_fiche(tenant, resa["id"])).status_code == 403
            # Son propre établissement devant l'identifiant d'une réservation d'autrui.
            page = client.get(_fiche(autre, resa["id"]))
            assert page.status_code == 404 and "Durand" not in page.text
        finally:
            tenants.delete_tenant(autre.id)

    def test_le_super_admin_voit_tout(self, resto):
        tenant, _ = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2)
        page = _client().get(_fiche(tenant, resa["id"]))
        assert page.status_code == 200 and "Chez Fiche" in page.text


class TestResos:
    @pytest.fixture()
    def demo(self):
        sante.reinitialiser()
        bac_a_sable.oublier_tout()
        tenant = tenants.create_tenant("Fiche resOS", f"+3362{id(object()) % 10_000_000:07d}")
        tenants.update_tenant(tenant.id, booking_provider=connecteurs.RESOS_DEMO)
        etat = bac_a_sable.etat_pour(tenant.id)
        etat.reservations.clear()
        yield tenants.get_by_id(tenant.id), etat
        tenants.delete_tenant(tenant.id)
        (bac_a_sable.dossier() / f"etablissement{tenant.id}.json").unlink(missing_ok=True)
        bac_a_sable.oublier_tout()
        sante.reinitialiser()

    def test_la_fiche_lit_resos_et_retrouve_l_appel(self, demo):
        tenant, etat = demo
        ref = etat.reserver(date=JOUR, time="20:00", people=4, nom="Lefèvre",
                            phone=CLIENT, status="request")
        _appel(tenant, ref, phrase="Pour quatre personnes, au nom de Lefèvre.")
        client = _client()
        jour = client.get(f"/admin/reservations?vue=jour&jour={JOUR}&tenant_id={tenant.id}")
        assert _fiche(tenant, ref) in jour.text
        texte = client.get(_fiche(tenant, ref)).text.replace("&#39;", "'")
        assert "Lefèvre" in texte and "À valider" in texte
        assert "Pour quatre personnes, au nom de Lefèvre." in texte
        assert "Son historique est dans resOS." in texte
        assert "Demande de réservation envoyée à resOS" not in texte

    def test_resos_injoignable(self, demo):
        tenant, etat = demo
        ref = etat.reserver(date=JOUR, time="20:00", nom="Lefèvre")
        etat.simuler("panne")
        page = _client().get(_fiche(tenant, ref))
        assert page.status_code == 502 and "Carnet resOS injoignable" in page.text
