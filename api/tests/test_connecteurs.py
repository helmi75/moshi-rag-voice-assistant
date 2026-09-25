"""Le carnet de réservations par établissement : le nôtre ou resOS (SCRUM-83 à 85).

resOS n'a pas de bac à sable : tout passe par `connecteurs/bac_a_sable.py`, calqué sur la doc
publique. Ces tests visent d'abord ce qui doit être IMPOSSIBLE — l'assistante qui
annonce une réservation que personne ne verra, ou qui lit à un appelant la réservation
d'un autre — puis le contrat : le modèle doit lire la même chose quel que soit le carnet.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest

from app import calls, connecteurs, db, horloge, llm, reservations, tenants
from app.connecteurs import resos

from app.connecteurs import bac_a_sable, sante
from app.connecteurs.bac_a_sable import CLE_DE_TEST, Etat, creer_app

APPELANT = "+33612345678"
AUTRE = "+33699999999"


def _dans(jours: int = 3) -> str:
    return (horloge.aujourd_hui() + timedelta(days=jours)).isoformat()


@pytest.fixture()
def base(tmp_path):
    # Chaque base jetable réutilise les mêmes id d'établissement : sans remise à zéro,
    # le coupe-circuit ouvert par un test ferait échouer le suivant.
    sante.reinitialiser()
    connecteurs.oublier_horaires()
    bac_a_sable.oublier_tout()
    with patch.object(db, "DB_PATH", str(tmp_path / "carnet.db")):
        db.init_db()
        yield
    sante.reinitialiser()
    connecteurs.oublier_horaires()
    bac_a_sable.oublier_tout()


@pytest.fixture()
def faux(monkeypatch):
    """Le faux resOS, branché à la place du réseau."""
    etat = Etat()
    monkeypatch.setattr(resos, "_transport", httpx.ASGITransport(app=creer_app(etat)))
    monkeypatch.setenv("RESOS_API_URL", "http://faux-resos/v1")
    return etat


@pytest.fixture()
def resto_resos(base, faux, monkeypatch):
    resto = tenants.create_tenant("Chez resOS", "+33199000777")
    resto = tenants.update_tenant(resto.id, booking_provider="resos")
    monkeypatch.setenv("RESOS_API_KEYS", f"999=autre-cle, {resto.id}={CLE_DE_TEST}")
    return resto


@pytest.fixture()
def resto_interne(base):
    return tenants.create_tenant("Chez Nous", "+33199000778")


def _outil(tenant, nom, args, numero=APPELANT):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, numero)))


def _creer(tenant, *, heure="20:00", couverts=2, jour=None, numero=APPELANT):
    return _outil(tenant, "create_reservation", {
        "customer_name": "Dupont", "date": jour or _dans(), "time": heure,
        "party_size": couverts, "notes": "table au calme"}, numero)


class TestLeChoixDuCarnet:
    def test_le_parc_existant_reste_sur_notre_carnet(self, resto_interne):
        assert connecteurs.fournisseur(resto_interne) == connecteurs.INTERNE
        assert type(connecteurs.pour(resto_interne)).__name__ == "ConnecteurInterne"

    def test_un_etablissement_resos_ecrit_dans_resos(self, resto_resos, faux):
        """Le garde-fou de SCRUM-83 : forcer notre carnet pour un établissement resOS
        doit se voir — la réservation serait chez nous, invisible du restaurant."""
        reponse = _creer(resto_resos)
        assert reponse["status"] == "pending_restaurant_approval"
        assert len(faux.reservations) == 1
        assert reservations.list_reservations(resto_resos.id) == []

    @pytest.mark.parametrize("valeur", [None, "", "bidon", "RESOS "])
    def test_une_valeur_inconnue_reste_sur_notre_carnet(self, valeur):
        tenant = type("T", (), {"booking_provider": valeur})()
        attendu = connecteurs.RESOS if valeur == "RESOS " else connecteurs.INTERNE
        assert connecteurs.fournisseur(tenant) == attendu

    def test_la_cle_de_chaque_etablissement(self, monkeypatch):
        monkeypatch.setenv("RESOS_API_KEYS", " 3 = abc , 7=def,,8=")
        assert resos.cle_pour(3) == "abc"
        assert resos.cle_pour(7) == "def"
        assert resos.cle_pour(8) is None
        assert resos.cle_pour(4) is None


class TestLeMemeContratPourLeModele:
    """Le modèle ne doit pas savoir où part la réservation : mêmes clés, mêmes statuts."""

    def test_disponibilite(self, resto_interne, resto_resos):
        for tenant in (resto_interne, resto_resos):
            reponse = _outil(tenant, "check_availability",
                             {"date": _dans(), "time": "20:00", "party_size": 2})
            assert reponse["available"] is True, tenant.name

    def test_retrouver_modifier_annuler(self, resto_interne, resto_resos):
        for tenant in (resto_interne, resto_resos):
            creee = _creer(tenant)
            trouvees = _outil(tenant, "find_reservation", {})["reservations"]
            assert [r["reservation_id"] for r in trouvees] == [creee["reservation_id"]]
            assert set(trouvees[0]) == {"reservation_id", "customer_name", "date", "time",
                                        "party_size", "notes"}
            modifiee = _outil(tenant, "modify_reservation",
                              {"reservation_id": str(creee["reservation_id"]), "party_size": 4})
            assert modifiee["status"] == "modified" and modifiee["party_size"] == 4
            annulee = _outil(tenant, "cancel_reservation",
                             {"reservation_id": creee["reservation_id"]})
            assert annulee["status"] == "cancelled", tenant.name
            assert _outil(tenant, "find_reservation", {})["reservations"] == []


class TestCreerDansResos:
    def test_une_demande_a_valider_jamais_une_confirmation(self, resto_resos, faux):
        """Décision du 25/09 : le restaurant valide. « C'est confirmé » serait faux."""
        reponse = _creer(resto_resos)
        assert reponse["status"] == "pending_restaurant_approval"
        assert "confirm" not in reponse["status"]
        assert "PAS « confirmé »" in reponse["consigne"]

    def test_ce_que_voit_le_restaurant_dans_resos(self, resto_resos, faux):
        _creer(resto_resos)
        (booking,) = faux.reservations.values()
        assert booking["status"] == "request"
        assert booking["source"] == "phone"
        assert booking["guest"]["phone"] == APPELANT
        assert booking["people"] == 2 and booking["time"] == "20:00"
        assert booking["comment"] == "table au calme"
        assert "assistante" in booking["note"]
        # resOS n'écrit pas en français : aucun SMS ni e-mail de sa part.
        assert booking["guest"]["notificationSms"] is False
        assert booking["guest"]["notificationEmail"] is False

    def test_l_appel_garde_la_reference_resos(self, resto_resos, faux):
        """Un identifiant resOS n'a pas de ligne chez nous : la clé étrangère le
        refuserait, et l'appel ne serait jamais clôturé."""
        calls.start_call("CA-resos", resto_resos.id)
        reference = _creer(resto_resos)["reservation_id"]
        call_id = calls.finish_call("CA-resos", "completed", None, reference)
        ligne = db.get_conn().execute(
            "SELECT reservation_id, reservation_externe FROM calls WHERE id = ?",
            (call_id,)).fetchone()
        assert ligne["reservation_id"] is None
        assert ligne["reservation_externe"] == reference
        assert calls.totals(days=1, tenant_id=resto_resos.id)["n_with_reservation"] == 1


class TestCreneauPris:
    """On ignore ce que resOS répond sur un créneau complet. Le faux ACCEPTE — le pire
    cas : sans vérification juste avant l'écriture, le restaurant recevrait une demande
    qu'il ne peut pas honorer."""

    def test_un_creneau_complet_propose_les_voisins_et_n_ecrit_rien(self, resto_resos, faux):
        jour = _dans()
        for _ in range(faux.capacite):
            faux.reserver(date=jour, time="20:00")
        avant = len(faux.reservations)
        reponse = _creer(resto_resos, jour=jour)
        assert "error" in reponse
        assert reponse["autres_horaires"] == ["19:30", "19:45", "20:15"]
        assert len(faux.reservations) == avant, "la demande est partie quand même"

    def test_la_disponibilite_propose_aussi(self, resto_resos, faux):
        jour = _dans()
        for _ in range(faux.capacite):
            faux.reserver(date=jour, time="20:00")
        reponse = _outil(resto_resos, "check_availability",
                         {"date": jour, "time": "20:00", "party_size": 2})
        assert reponse["available"] is False
        assert "19:45" in reponse["consigne"]

    def test_un_jour_ferme(self, resto_resos, faux):
        jour = _dans()
        faux.fermes.add(jour)
        reponse = _creer(resto_resos, jour=jour)
        assert reponse["autres_horaires"] == []
        assert "autre jour" in reponse["error"]

    def test_deplacer_vers_un_creneau_complet(self, resto_resos, faux):
        jour = _dans()
        creee = _creer(resto_resos, jour=jour, heure="19:00")
        for _ in range(faux.capacite):
            faux.reserver(date=jour, time="21:00")
        reponse = _outil(resto_resos, "modify_reservation",
                         {"reservation_id": creee["reservation_id"], "time": "21:00"})
        assert "error" in reponse
        assert faux.reservations[creee["reservation_id"]]["time"] == "19:00"


class TestResosInjoignable:
    """Le seul mauvais comportement impardonnable : « c'est enregistré » alors que non."""

    def _jamais_annonce(self, reponse):
        assert "error" in reponse
        assert "N'annonce RIEN" in reponse["error"]
        assert "status" not in reponse

    def test_panne(self, resto_resos, faux):
        faux.en_panne = True
        self._jamais_annonce(_creer(resto_resos))

    def test_trop_lent(self, resto_resos, faux, monkeypatch):
        monkeypatch.setattr(resos, "DELAI_SECONDES", 0.05)
        faux.retard = 0.3
        self._jamais_annonce(_creer(resto_resos))

    def test_cle_absente(self, resto_resos, faux, monkeypatch):
        monkeypatch.setenv("RESOS_API_KEYS", "")
        self._jamais_annonce(_creer(resto_resos))
        assert list(faux.requetes) == [], "sans clé, on n'appelle même pas resOS"

    def test_cle_refusee(self, resto_resos, faux):
        faux.cle = "une-autre-cle"
        self._jamais_annonce(_creer(resto_resos))

    @pytest.mark.parametrize("outil,args", [
        ("find_reservation", {}),
        ("cancel_reservation", {"reservation_id": "abc"}),
        ("check_availability", {"date": "2099-01-01", "time": "20:00", "party_size": 2}),
    ])
    def test_aucun_outil_ne_fait_semblant(self, resto_resos, faux, outil, args):
        faux.en_panne = True
        self._jamais_annonce(_outil(resto_resos, outil, args))

    def test_une_requete_rejetee_n_est_pas_une_reussite(self, resto_resos, faux):
        """422 de resOS (paramètre manquant) : on sait que rien n'est écrit."""
        reponse = _outil(resto_resos, "create_reservation", {
            "customer_name": "Dupont", "date": _dans(), "time": "20:00", "party_size": 0})
        assert "error" in reponse and "status" not in reponse


class TestLesReservationsDesAutres:
    def test_retrouver_ne_rend_que_ce_numero(self, resto_resos, faux):
        faux.reserver(date=_dans(), time="20:00", phone=AUTRE, nom="Voisin")
        mienne = faux.reserver(date=_dans(), time="21:00", phone=APPELANT, nom="Dupont")
        trouvees = _outil(resto_resos, "find_reservation", {})["reservations"]
        assert [r["reservation_id"] for r in trouvees] == [mienne]

    def test_meme_si_resos_ignorait_le_filtre(self, resto_resos, faux, monkeypatch):
        """Le filtre `customQuery` n'a jamais été éprouvé sur la vraie API. S'il était
        ignoré, resOS rendrait tout le carnet : on revérifie le numéro nous-mêmes."""
        faux.reserver(date=_dans(), time="20:00", phone=AUTRE, nom="Voisin")
        mienne = faux.reserver(date=_dans(), time="21:00", phone=APPELANT)
        monkeypatch.setattr(bac_a_sable, "_filtre", lambda booking, expression: True)
        trouvees = _outil(resto_resos, "find_reservation", {})["reservations"]
        assert [r["reservation_id"] for r in trouvees] == [mienne]

    @pytest.mark.parametrize("outil", ["modify_reservation", "cancel_reservation"])
    def test_on_ne_touche_pas_a_celle_d_un_autre(self, resto_resos, faux, outil):
        victime = faux.reserver(date=_dans(), time="20:00", phone=AUTRE)
        reponse = _outil(resto_resos, outil, {"reservation_id": victime, "party_size": 8})
        assert "error" in reponse
        assert faux.reservations[victime]["status"] == "approved"
        assert faux.reservations[victime]["people"] == 2

    def test_un_identifiant_ne_sort_pas_de_bookings(self, resto_resos, faux):
        """L'identifiant vient du modèle : il est échappé avant d'entrer dans l'URL."""
        _outil(resto_resos, "cancel_reservation", {"reservation_id": "../bookingFlow/times"})
        chemins = [chemin for _, chemin, _ in faux.requetes]
        assert chemins and all(c.startswith("/v1/bookings/") for c in chemins), chemins

    def test_une_reservation_passee_ou_annulee_ne_se_modifie_plus(self, resto_resos, faux):
        annulee = faux.reserver(date=_dans(), time="20:00", phone=APPELANT, status="canceled")
        passee = faux.reserver(date=_dans(-2), time="20:00", phone=APPELANT)
        for identifiant in (annulee, passee):
            reponse = _outil(resto_resos, "cancel_reservation", {"reservation_id": identifiant})
            assert "error" in reponse


class TestModifierDansResos:
    def test_les_notes_deviennent_une_note_interne(self, resto_resos, faux):
        creee = _creer(resto_resos)
        _outil(resto_resos, "modify_reservation",
               {"reservation_id": creee["reservation_id"], "notes": "chaise haute"})
        assert faux.notes == [(creee["reservation_id"], "Demande du client : chaise haute")]

    def test_l_annulation_passe_le_statut_a_canceled(self, resto_resos, faux):
        creee = _creer(resto_resos)
        _outil(resto_resos, "cancel_reservation", {"reservation_id": creee["reservation_id"]})
        assert faux.reservations[creee["reservation_id"]]["status"] == "canceled"


class TestLeFauxRespecteLaDoc:
    """Si le faux s'écartait de la doc, tous les tests ci-dessus mentiraient."""

    def _client(self, faux, cle=CLE_DE_TEST):
        return httpx.AsyncClient(transport=resos._transport, base_url="http://faux-resos/v1",
                                 auth=(cle, ""))

    def test_formes_des_reponses(self, faux):
        async def scenario():
            async with self._client(faux) as client:
                cree = await client.post("/bookings", json={
                    "date": _dans(), "time": "20:00", "people": 2,
                    "guest": {"name": "Neo", "phone": "+4500000000"}})
                identifiant = cree.json()
                modifie = await client.put(f"/bookings/{identifiant}", json={"people": 3})
                filtre = await client.get("/bookings", params={
                    "customQuery": 'guest.phone:"4500000000"'})
                sans_cle = await self._client(faux, cle="faux").get("/healthcheck")
                return identifiant, modifie.json(), filtre.json(), sans_cle.status_code

        identifiant, modifie, filtre, code = asyncio.run(scenario())
        assert isinstance(identifiant, str) and len(identifiant) == 17
        assert modifie is True
        assert [b["_id"] for b in filtre] == [identifiant]
        assert filtre[0]["status"] == "request", "statut par défaut documenté"
        assert code == 401


class TestLeChoixDansLAdmin:
    """Le carnet décide où partent les réservations d'un client : réglé par le
    super-admin, jamais par le restaurateur, et la clé ne passe jamais par l'écran."""

    @pytest.fixture(autouse=True)
    def _application(self):
        # La base de test (et le super-admin) naissent à l'import de l'application.
        from app.main import app

        return app

    def _client_connecte(self, email, mot_de_passe):
        import base64

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        assert client.post("/admin/login", data={"email": email, "password": mot_de_passe},
                           follow_redirects=False).status_code == 303
        client.get("/admin/")
        brut = client.cookies.get("session").split(".")[0]
        brut += "=" * (-len(brut) % 4)
        return client, json.loads(base64.b64decode(brut))["csrf"]

    def _envoyer(self, client, csrf, tenant, carnet):
        return client.post(
            f"/admin/tenants/{tenant.id}",
            data={"name": tenant.name, "phone_number": tenant.phone_number,
                  "business_type": "restaurant", "language": "fr-FR", "greeting": "",
                  "knowledge_base": "", "booking_provider": carnet},
            headers={"X-CSRF-Token": csrf}, follow_redirects=False)

    def test_le_restaurateur_ne_choisit_pas_son_carnet(self):
        from app import users

        tenant = tenants.create_tenant("Chez Malin", "+33199000881")
        try:
            users.create_user(f"malin{tenant.id}@test.fr", "resto-pass-carnet",
                              users.ROLE_RESTAURATEUR, tenant.id)
            client, csrf = self._client_connecte(f"malin{tenant.id}@test.fr",
                                                 "resto-pass-carnet")
            assert self._envoyer(client, csrf, tenant, "resos").status_code == 303
            assert tenants.get_by_id(tenant.id).booking_provider is None
        finally:
            tenants.delete_tenant(tenant.id)

    def test_le_super_admin_le_choisit_et_voit_si_la_cle_est_posee(self, monkeypatch):
        tenant = tenants.create_tenant("Chez Patron", "+33199000882")
        try:
            client, csrf = self._client_connecte("admin@test.local", "test-admin-pass")
            self._envoyer(client, csrf, tenant, "resos")
            assert tenants.get_by_id(tenant.id).booking_provider == "resos"

            page = client.get(f"/admin/tenants/{tenant.id}/edit").text
            assert "Clé API resOS absente" in page
            monkeypatch.setenv("RESOS_API_KEYS", f"{tenant.id}=cle-tres-secrete")
            page = client.get(f"/admin/tenants/{tenant.id}/edit").text
            assert "Clé API resOS posée" in page
            assert "cle-tres-secrete" not in page, "la clé ne s'affiche jamais"

            self._envoyer(client, csrf, tenant, "carnet-invente")
            assert tenants.get_by_id(tenant.id).booking_provider == "resos", (
                "une valeur hors liste ne doit pas s'écrire")
        finally:
            tenants.delete_tenant(tenant.id)
