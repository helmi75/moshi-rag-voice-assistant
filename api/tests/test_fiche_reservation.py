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
        assert "Aucun appel rattaché" in texte
        assert "Aucun appel n'est rattaché à cette réservation" in texte
        assert "Numéro non communiqué" in texte
        assert "Sans numéro, ses autres réservations ne se retrouvent pas." in texte

    def test_sans_appel_l_origine_n_est_pas_inventee(self, resto):
        """L'admin ne crée pas de réservation : sans appel rattaché, elle vient d'un SMS,
        d'un appel encore en cours ou jamais clôturé, ou c'est la seconde d'un même
        appel. La fiche ne peut pas dire « saisie à la main »."""
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Martin", JOUR, "19:30", 3,
                                               customer_phone=CLIENT)
        texte = _client(compte.email, "resto-pass").get(_fiche(tenant, resa["id"])).text
        assert "saisie à la main" not in texte.lower() and "Saisie à la main" not in texte
        assert "hors de l" not in texte

    def test_aucune_autre_reservation_sans_rien_affirmer(self, resto):
        """Le numéro est comparé tel qu'il est écrit : ne rien trouver ne prouve pas que
        c'est une première réservation."""
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Petit", JOUR, "19:30", 3,
                                               customer_phone="+33699887766")
        texte = _client(compte.email, "resto-pass").get(
            _fiche(tenant, resa["id"])).text.replace("&#39;", "'")
        assert "Aucune autre réservation trouvée avec ce numéro." in texte
        assert "première réservation" not in texte

    def test_un_numero_saisi_a_la_francaise_n_est_pas_dit_inconnu(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Petit", JOUR, "19:30", 3,
                                               customer_phone="06 11 22 33 44")
        texte = _client(compte.email, "resto-pass").get(
            _fiche(tenant, resa["id"])).text.replace("&#39;", "'")
        assert 'href="tel:06 11 22 33 44"' in texte
        assert "n'est pas écrit au format international" in texte
        assert "Numéro inconnu" not in texte

    @pytest.mark.parametrize("ref", [99_999_999, "pas-un-nombre", "²", "٣",
                                     "9" * 25, "-1", "1.0"])
    def test_une_reservation_inconnue(self, resto, ref):
        """« ² » passe str.isdigit() et fait échouer int() ; vingt-cinq chiffres débordent
        l'INTEGER de SQLite : un 404, pas une erreur 500."""
        tenant, compte = resto
        assert _client(compte.email, "resto-pass").get(_fiche(tenant, ref)).status_code == 404

    def test_l_appel_est_date_a_l_heure_du_restaurant(self, resto):
        """Un appel de 00 h 30 à Paris est stocké la veille à 23 h 30 UTC : la fiche ne
        doit pas dire que la réservation a été prise la veille."""
        from app import db

        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2,
                                               customer_phone=CLIENT)
        call_id = _appel(tenant, resa["id"])
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = '2030-01-31T23:30:00Z' WHERE id = ?",
                         (call_id,))
        texte = _client(compte.email, "resto-pass").get(
            _fiche(tenant, resa["id"])).text.replace("&#39;", "'")
        assert "Prise au téléphone par l'assistante, le 01/02 à 00:30" in texte
        assert "01/02/2030" in texte and "2030-02-01 00:30" in texte
        assert "23:30" not in texte and "31/01" not in texte

    def test_marquer_traite_revient_a_la_fiche(self, resto):
        from app import messages

        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2,
                                               customer_phone=CLIENT)
        call_id = _appel(tenant, resa["id"])
        message = messages.create_message(tenant.id, "Rappeler pour le menu", call_id=call_id,
                                          caller_number=CLIENT)
        client = _client(compte.email, "resto-pass")
        fiche = _fiche(tenant, resa["id"])

        def retour_du_message(page: str) -> str:
            formulaire = page.split(f'action="/admin/messages/{message}/traite"')[1]
            return formulaire.split('name="retour" value="')[1].split('"')[0]

        assert retour_du_message(client.get(fiche).text) == fiche
        # La fiche de l'appel, elle, garde son propre retour.
        appel = f"/admin/calls/{call_id}"
        assert retour_du_message(client.get(appel).text) == appel
        resp = client.post(f"/admin/messages/{message}/traite", data={"retour": fiche},
                           follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == fiche


class TestLeRetour:
    def test_le_restaurateur_revient_au_jour(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2)
        page = _client(compte.email, "resto-pass").get(_fiche(tenant, resa["id"])).text
        assert f'href="/admin/reservations?vue=jour&amp;jour={JOUR}#resa-{resa["id"]}"' in page

    def test_le_super_admin_revient_au_jour_de_cet_etablissement(self, resto):
        """Sans `tenant_id`, il retombait sur tout le parc."""
        tenant, _ = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2)
        page = _client().get(_fiche(tenant, resa["id"])).text
        assert (f'href="/admin/reservations?vue=jour&amp;jour={JOUR}&amp;tenant_id={tenant.id}'
                f'#resa-{resa["id"]}"') in page


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

    def test_sans_appel_la_fiche_resos_ne_dit_pas_hors_de_l_assistante(self, demo):
        tenant, etat = demo
        ref = etat.reserver(date=JOUR, time="20:00", nom="Lefèvre")
        texte = _client().get(_fiche(tenant, ref)).text.replace("&#39;", "'")
        assert "Aucun appel rattaché" in texte
        assert "hors de l'assistante" not in texte and "directement dans resOS" not in texte

    def test_un_identifiant_refuse_par_resos_est_introuvable(self, demo, monkeypatch):
        """resOS a lu la demande et l'a rejetée (4xx) : ce n'est pas une panne, et
        « réessayez dans un instant » ferait réessayer pour rien."""
        from app.connecteurs.resos import ConnecteurResos

        async def refuse(self, reservation_id):
            raise connecteurs.Refus("identifiant mal formé")

        monkeypatch.setattr(ConnecteurResos, "lire", refuse)
        tenant, _ = demo
        page = _client().get(_fiche(tenant, "abc"))
        assert page.status_code == 404 and "injoignable" not in page.text

    def test_resos_injoignable(self, demo):
        tenant, etat = demo
        ref = etat.reserver(date=JOUR, time="20:00", nom="Lefèvre")
        etat.simuler("panne")
        page = _client().get(_fiche(tenant, ref))
        assert page.status_code == 502 and "Carnet resOS injoignable" in page.text
