"""SMS de confirmation facturés au restaurateur (ASSISTANTE-198, décision du 23/11/2026).

0,20 € HT le SMS, le même prix pour toutes les formules, hors décompte des minutes. Le
restaurateur allume les SMS lui-même et fixe, s'il le veut, un plafond mensuel : atteint,
plus de SMS, mais l'appel continue et Marie confirme à l'oral. Ce fichier tient ce qui ne
doit jamais arriver : un SMS envoyé au-delà du plafond qu'il s'est fixé, un SMS envoyé sans
être compté, le compte d'un autre établissement ou d'un autre mois, le nom d'expéditeur
choisi par un restaurateur. Aucun test ne touche le réseau.
"""
import asyncio
import base64
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import calls, db, llm, plans, quotas, sms, tenants, users
from app.main import app

MOBILE = "+33612345678"


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Le Bouchon Doré", "+33199000891")
    tenants.update_tenant(tenant.id, sms_actif=1)
    yield tenants.get_by_id(tenant.id)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM calls WHERE tenant_id = ?", (tenant.id,))
    tenants.delete_tenant(tenant.id)


@pytest.fixture()
def planifies():
    partis: list = []
    with patch.object(llm.sms, "planifier",
                      side_effect=lambda tenant, reservation, call_id, numero, nom:
                      partis.append(numero)):
        yield partis


_compteur = iter(range(1, 10_000))


def _appel(tenant, sms_envoyes=1, sid=None, started_at=None) -> int:
    """Un appel du mois, avec `sms_envoyes` SMS acceptés par Twilio."""
    identifiant = calls.start_call(sid or f"CA-fact-{next(_compteur)}", tenant.id, MOBILE)
    for _ in range(sms_envoyes):
        sms._ajouter_cout(identifiant, 0.0798)
    if started_at:
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = ? WHERE id = ?", (started_at, identifiant))
    return identifiant


def _creer(tenant, call_id) -> dict:
    jour = (date.today() + timedelta(days=1)).isoformat()
    return json.loads(asyncio.run(llm.run_tool(
        tenant, "create_reservation",
        {"customer_name": "Martin", "date": jour, "time": "20:00", "party_size": 2},
        MOBILE, call_id)))


class TestLePrix:
    def test_vingt_centimes_hors_taxes_le_sms(self):
        assert plans.SMS_PRIX_EUR == 0.20
        assert plans.cout_sms_eur(3) == 0.60
        assert plans.cout_sms_eur(0) == 0.0


class TestLeCompte:
    def test_chaque_sms_accepte_est_compte_une_fois(self, resto):
        identifiant = _appel(resto, sms_envoyes=2)
        with db.get_conn() as conn:
            row = conn.execute("SELECT sms_envoyes, cout_sms FROM calls WHERE id = ?",
                               (identifiant,)).fetchone()
        assert row["sms_envoyes"] == 2 and row["cout_sms"] == pytest.approx(2 * 0.0798)

    def test_le_mois_du_restaurant_seulement(self, resto):
        _appel(resto, sms_envoyes=1)
        _appel(resto, sms_envoyes=1)
        _appel(resto, sms_envoyes=5, started_at="2000-01-15T12:00:00Z")
        etat = quotas.sms(resto)
        assert etat.nombre == 2 and etat.montant_eur == 0.40

    def test_un_autre_etablissement_ne_compte_pas(self, resto):
        autre = tenants.create_tenant("Chez L'Autre", "+33199000892")
        try:
            _appel(autre, sms_envoyes=4)
            assert quotas.sms(resto).nombre == 0
            assert quotas.sms(tenants.get_by_id(autre.id)).nombre == 4
        finally:
            with db.get_conn() as conn:
                conn.execute("DELETE FROM calls WHERE tenant_id = ?", (autre.id,))
            tenants.delete_tenant(autre.id)

    def test_le_banc_d_essai_et_l_etablissement_d_essai_ne_comptent_pas(self, resto, monkeypatch):
        _appel(resto, sms_envoyes=1, sid=f"{calls.PREFIXE_BANC}fact")
        assert quotas.sms(resto).nombre == 0
        _appel(resto, sms_envoyes=1)
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(resto.id))
        assert quotas.sms(resto).nombre == 0

    def test_le_parc_compte_en_une_fois_la_meme_chose(self, resto):
        _appel(resto, sms_envoyes=3)
        assert quotas.sms_par_tenant([resto])[resto.id].nombre == 3


class TestLePlafond:
    def test_sans_plafond_le_sms_part_toujours(self, resto, planifies):
        for _ in range(3):
            _appel(resto)
        assert _creer(resto, _appel(resto, sms_envoyes=0))["sms_confirmation"] is True
        assert len(planifies) == 1

    def test_sous_le_plafond_le_sms_part(self, resto, planifies):
        tenants.update_tenant(resto.id, sms_plafond=3)
        _appel(resto, sms_envoyes=2)
        resultat = _creer(tenants.get_by_id(resto.id), _appel(resto, sms_envoyes=0))
        assert resultat["sms_confirmation"] is True and len(planifies) == 1

    def test_plafond_atteint_plus_de_sms_mais_la_reservation_est_prise(self, resto, planifies):
        tenants.update_tenant(resto.id, sms_plafond=3)
        _appel(resto, sms_envoyes=3)
        resultat = _creer(tenants.get_by_id(resto.id), _appel(resto, sms_envoyes=0))
        assert resultat["status"] == "confirmed" and resultat["reservation_id"]
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_le_mois_suivant_le_plafond_repart_de_zero(self, resto, planifies):
        tenants.update_tenant(resto.id, sms_plafond=1)
        _appel(resto, sms_envoyes=1, started_at="2000-01-15T12:00:00Z")
        resultat = _creer(tenants.get_by_id(resto.id), _appel(resto, sms_envoyes=0))
        assert resultat["sms_confirmation"] is True

    def test_niveaux(self, resto):
        tenants.update_tenant(resto.id, sms_plafond=10)
        t = tenants.get_by_id(resto.id)
        assert quotas.sms(t).niveau == quotas.OK
        _appel(resto, sms_envoyes=8)
        assert quotas.sms(t).niveau == quotas.ALERTE
        _appel(resto, sms_envoyes=2)
        assert quotas.sms(t).niveau == quotas.PLAFOND_ATTEINT
        assert quotas.sms_plafond_atteint(t) is True

    def test_alertes_du_parc_a_80_pourcent_et_au_plafond(self, resto):
        tenants.update_tenant(resto.id, sms_plafond=10)
        _appel(resto, sms_envoyes=8)
        alertes = quotas.alertes([tenants.get_by_id(resto.id)])
        assert any("SMS" in a["title"] and "80 %" in a["title"] for a in alertes)
        _appel(resto, sms_envoyes=2)
        alertes = quotas.alertes([tenants.get_by_id(resto.id)])
        assert any("SMS" in a["title"] and a["level"] == "warn" for a in alertes)

    def test_sans_plafond_aucune_alerte_sms(self, resto):
        _appel(resto, sms_envoyes=50)
        assert not any("SMS" in a["title"] for a in quotas.alertes([resto]))


# ---- Admin ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(app)


def _csrf(client) -> str:
    charge = client.cookies.get("session").split(".")[0]
    charge += "=" * (-len(charge) % 4)
    return json.loads(base64.urlsafe_b64decode(charge))["csrf"]


def _restaurateur(client, tenant) -> None:
    user = users.create_user(f"gerant-fact-{tenant.id}@test.fr", "resto-pass-fact",
                             users.ROLE_RESTAURATEUR, tenant.id)
    assert client.post("/admin/login", data={"email": user.email, "password": "resto-pass-fact"},
                       follow_redirects=False).status_code == 303


def _poster(client, tenant, **champs):
    data = {"name": tenant.name, "business_type": "restaurant", "language": "fr-FR",
            "phone_number": tenant.phone_number, **champs}
    return client.post(f"/admin/tenants/{tenant.id}", data=data,
                       headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)


class TestLeRestaurateurDecide:
    def test_il_allume_et_fixe_son_plafond(self, client, resto):
        tenants.update_tenant(resto.id, sms_actif=0)
        _restaurateur(client, resto)
        assert _poster(client, resto, sms_present="1", sms_actif="1",
                       sms_plafond="40").status_code == 303
        apres = tenants.get_by_id(resto.id)
        assert apres.sms_actif is True and apres.sms_plafond == 40

    def test_il_eteint_et_retire_son_plafond(self, client, resto):
        tenants.update_tenant(resto.id, sms_plafond=40)
        _restaurateur(client, resto)
        _poster(client, resto, sms_present="1", sms_plafond="")
        apres = tenants.get_by_id(resto.id)
        assert apres.sms_actif is False and apres.sms_plafond is None

    def test_il_ne_choisit_pas_le_nom_d_expediteur(self, client, resto):
        _restaurateur(client, resto)
        _poster(client, resto, sms_present="1", sms_actif="1", sms_expediteur="MaBanque")
        assert tenants.get_by_id(resto.id).sms_expediteur is None

    @pytest.mark.parametrize("saisie", ["0", "-3", "dix", "1.5", "²", "1000000"])
    def test_un_plafond_invalide_est_refuse_par_le_serveur(self, client, resto, saisie):
        _restaurateur(client, resto)
        reponse = _poster(client, resto, sms_present="1", sms_actif="1", sms_plafond=saisie)
        assert reponse.status_code == 422
        assert tenants.get_by_id(resto.id).sms_plafond is None

    def test_cloisonnement_403_sur_le_plafond_d_un_autre(self, client, resto):
        autre = tenants.create_tenant("Chez L'Autre", "+33199000893")
        try:
            _restaurateur(client, autre)
            assert _poster(client, resto, sms_present="1", sms_actif="1",
                           sms_plafond="1").status_code == 403
            assert tenants.get_by_id(resto.id).sms_plafond is None
        finally:
            tenants.delete_tenant(autre.id)

    def test_sa_fiche_montre_la_case_le_prix_et_le_plafond(self, client, resto):
        tenants.update_tenant(resto.id, sms_plafond=40)
        _restaurateur(client, resto)
        page = client.get(f"/admin/tenants/{resto.id}/edit").text
        assert 'name="sms_actif"' in page and 'name="sms_plafond"' in page
        assert "0,20 €" in page and 'value="40"' in page
        assert 'name="sms_expediteur"' not in page

    def test_son_tableau_de_bord_montre_les_sms_du_mois(self, client, resto):
        tenants.update_tenant(resto.id, sms_plafond=10)
        _appel(resto, sms_envoyes=3)
        _restaurateur(client, resto)
        page = client.get("/admin/").text.replace("&#39;", "'")
        assert "3 SMS" in page and "0,60 €" in page and "10" in page
