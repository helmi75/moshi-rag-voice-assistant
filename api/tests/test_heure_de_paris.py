"""Tout ce que l'admin affiche est à l'heure de Paris (01/10/2026).

La base stocke en UTC ; les pages découpaient ces horodatages tels quels. Un appel de
00 h 30 à Paris s'affichait donc la veille à 22 h 30 (été) ou 23 h 30 (hiver), et une
réservation annulée après minuit portait la date de la veille. Les premiers clients sont
parisiens : une heure fausse de deux heures, c'est un appel qu'on ne retrouve pas."""
import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import calls, db, horloge, reservations, tenants, users
from app.admin import deps, presenters
from app.main import app

HIVER = "2030-01-31T23:30:00Z"   # 1er février, 00 h 30 à Paris (UTC+1)
ETE = "2030-07-14T22:15:00Z"     # 15 juillet, 00 h 15 à Paris (UTC+2)
JOUR = "2030-03-13"


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
    tenant = tenants.create_tenant("Chez Paris", f"+3364{id(object()) % 10_000_000:07d}")
    compte = users.create_user(f"paris-{tenant.id}@test.fr", "resto-pass",
                               users.ROLE_RESTAURATEUR, tenant.id)
    yield tenant, compte
    tenants.delete_tenant(tenant.id)


def _appel(tenant, debut: str) -> int:
    sid = f"CA{uuid.uuid4().hex[:30]}"
    calls.start_call(sid, tenant.id, "+33611223344")
    call_id = calls.finish_call(sid, "completed", [{"role": "user", "content": "Bonjour."}])
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET started_at = ? WHERE id = ?", (debut, call_id))
    return call_id


class TestLHorloge:
    def test_hiver_et_ete(self):
        assert horloge.au_restaurant(HIVER).strftime("%Y-%m-%d %H:%M") == "2030-02-01 00:30"
        assert horloge.au_restaurant(ETE).strftime("%Y-%m-%d %H:%M") == "2030-07-15 00:15"

    def test_le_format_de_sqlite_aussi(self):
        """`datetime('now')` écrit « 2030-01-31 23:30:00 », sans T ni Z."""
        assert horloge.au_restaurant("2030-01-31 23:30:00").strftime("%d/%m %H:%M") == "01/02 00:30"

    @pytest.mark.parametrize("brut", [None, "", "pas une date"])
    def test_un_horodatage_illisible_ne_leve_pas(self, brut):
        assert horloge.au_restaurant(brut) is None


class TestLesAppels:
    def test_la_ligne_d_appel_change_de_jour_avec_paris(self):
        vue = presenters.call_view({"started_at": HIVER})
        assert (vue["date_label"], vue["time_label"]) == ("2030-02-01", "00:30")
        vue = presenters.call_view({"started_at": ETE})
        assert (vue["date_label"], vue["time_label"]) == ("2030-07-15", "00:15")

    def test_un_horodatage_illisible_s_affiche_tel_quel(self):
        """Comme avant : la page de diagnostic doit s'ouvrir même sur un appel abîmé."""
        brut = "n'importe quoi"
        vue = presenters.call_view({"started_at": brut})
        assert (vue["date_label"], vue["time_label"]) == (brut[:10], brut[11:16])
        vide = presenters.call_view({})
        assert (vide["date_label"], vide["time_label"]) == ("", "")

    def test_le_journal_la_fiche_et_la_salle_de_controle(self, resto):
        tenant, compte = resto
        call_id = _appel(tenant, ETE)
        client = _client(compte.email, "resto-pass")
        for page in ("/admin/calls", f"/admin/calls/{call_id}", "/admin/"):
            texte = client.get(page).text
            assert "2030-07-15 00:15" in texte, page
            assert "22:15" not in texte and "2030-07-14" not in texte, page

    def test_le_diagnostic_aussi(self, resto):
        tenant, _ = resto
        call_id = _appel(tenant, HIVER)
        texte = _client().get(f"/admin/calls/{call_id}/diagnostic").text
        assert "2030-02-01 à 00:30" in texte and "23:30" not in texte


class TestLesAnnulations:
    @pytest.fixture()
    def annulee(self, resto):
        tenant, compte = resto
        resa = reservations.create_reservation(tenant.id, "Durand", JOUR, "20:00", 2)
        reservations.cancel_reservation(resa["id"])
        with db.get_conn() as conn:
            conn.execute("UPDATE reservations SET cancelled_at = ? WHERE id = ?",
                         (HIVER, resa["id"]))
        return tenant, _client(compte.email, "resto-pass"), resa

    def test_la_carte_du_jour(self, annulee):
        _, client, _ = annulee
        jour = client.get(f"/admin/reservations?vue=jour&jour={JOUR}").text
        assert "Annulée le 01/02/2030." in jour and "31/01/2030" not in jour

    def test_la_liste(self, annulee):
        _, client, _ = annulee
        liste = client.get("/admin/reservations?vue=liste").text
        assert "annulée 2030-02-01" in liste
        assert 'title="Annulée le 01/02/2030 à 00:30"' in liste
        assert "2030-01-31" not in liste

    def test_la_fiche(self, annulee):
        tenant, client, resa = annulee
        fiche = client.get(f"/admin/tenants/{tenant.id}/reservations/{resa['id']}").text
        assert "le 01/02/2030 à 00:30" in fiche and "31/01" not in fiche


class TestLeReste:
    def test_sante_et_couts_ne_parle_plus_en_utc(self):
        page = _client().get("/admin/health").text
        assert "(heure de Paris)" in page and " UTC." not in page

    def test_la_date_de_creation_d_un_compte(self, resto):
        tenant, compte = resto
        with db.get_conn() as conn:
            conn.execute("UPDATE users SET created_at = ? WHERE id = ?", (HIVER, compte.id))
        page = _client().get(f"/admin/tenants/{tenant.id}/users").text
        assert "2030-02-01" in page and "2030-01-31" not in page

    def test_les_filtres_rendent_tel_quel_ce_qu_ils_ne_lisent_pas(self):
        filtres = deps.templates.env.filters
        assert filtres["jour_paris"]("pas une date") == "pas une date"
        assert filtres["heure_paris"](None) == ""
        assert (filtres["date_paris"](ETE), filtres["jour_paris"](ETE),
                filtres["heure_paris"](ETE)) == ("2030-07-15", "15/07/2030", "00:15")


def test_plus_aucun_decoupage_d_horodatage_dans_les_pages():
    """Le garde-fou de fond : un `[:10]` ou `[11:16]` sur une colonne `_at` / `_le`
    réaffiche l'UTC. Les seuls découpages permis portent sur des valeurs déjà locales
    (`date_label`, la date d'une réservation)."""
    import re

    fautifs = []
    for gabarit in deps.TEMPLATES_DIR.rglob("*.html"):
        for n, ligne in enumerate(gabarit.read_text().splitlines(), 1):
            if re.search(r"\w+_(?:at|le)\[", ligne):
                fautifs.append(f"{gabarit.name}:{n}")
    assert fautifs == []
