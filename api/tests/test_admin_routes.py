"""Routes de la plateforme admin : auth, CSRF, scoping par rôle, CRUD, non-régression
des webhooks Twilio (jamais d'auth sur le chemin des appels)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import calls, reservations, tenants, users
from app.main import app


@pytest.fixture()
def client():
    """Client FRAIS par test (cookie jar isolé — les sessions ne fuient pas)."""
    return TestClient(app)


@pytest.fixture()
def resto():
    """Tenant + compte restaurateur jetables."""
    tenant = tenants.create_tenant("Chez Test", f"+3361{id(object()) % 10_000_000:07d}")
    user = users.create_user(f"resto{tenant.id}@test.fr", "resto-pass", users.ROLE_RESTAURATEUR, tenant.id)
    yield tenant, user
    tenants.delete_tenant(tenant.id)


def _login(client: TestClient, email="admin@test.local", password="test-admin-pass"):
    resp = client.post("/admin/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303, f"login attendu 303, reçu {resp.status_code}"
    return client


def _csrf(client: TestClient) -> str:
    """Récupère le token CSRF de la session en visitant une page."""
    client.get("/admin/")
    import base64, json as jsonlib

    # Le cookie de session Starlette est du JSON signé base64 : payload avant le 1er point.
    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    return jsonlib.loads(base64.b64decode(raw))["csrf"]


class TestAuth:
    def test_login_page(self, client):
        assert client.get("/admin/login").status_code == 200

    def test_login_ok_then_dashboard(self, client):
        _login(client)
        resp = client.get("/admin/")
        assert resp.status_code == 200
        assert "Tableau de bord" in resp.text

    def test_login_bad_password(self, client):
        resp = client.post("/admin/login", data={"email": "admin@test.local", "password": "wrong"})
        assert resp.status_code == 401
        assert "Identifiants invalides" in resp.text

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/admin/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/login"

    def test_unauthenticated_htmx_gets_hx_redirect(self, client):
        resp = client.get("/admin/", headers={"HX-Request": "true"}, follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers.get("hx-redirect") == "/admin/login"

    def test_logout(self, client):
        _login(client)
        token = _csrf(client)
        resp = client.post("/admin/logout", data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert client.get("/admin/", follow_redirects=False).status_code == 303


class TestCsrf:
    def test_post_without_csrf_is_403(self, client):
        _login(client)
        resp = client.post("/admin/tenants", data={"name": "X", "phone_number": "+33100000099"})
        assert resp.status_code == 403

    def test_post_with_header_csrf_ok(self, client, resto):
        tenant, _ = resto
        _login(client)
        token = _csrf(client)
        resp = client.post(
            f"/admin/reservations/{_seed_resa(tenant.id)}/delete",
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200


class TestTwilioUntouched:
    """Non-régression : le chemin des appels ne doit JAMAIS exiger session/CSRF."""

    def test_health_open(self, client):
        assert client.get("/health").status_code == 200

    def test_twilio_voice_open(self, client):
        resp = client.post("/twilio/voice", data={"To": "+33100000000", "CallSid": "CAtest"})
        assert resp.status_code == 200
        assert "<?xml" in resp.text

    def test_reservations_api_open(self, client):
        demo = tenants.get_by_phone("+33100000000")
        assert client.get(f"/tenants/{demo.id}/reservations").status_code == 200


class TestTenantsCrud:
    def test_create_edit_delete(self, client):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/admin/tenants",
            data={"name": "La Cantine", "phone_number": "+33612340001", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        tenant = next(t for t in tenants.list_all() if t.name == "La Cantine")

        resp = client.post(
            f"/admin/tenants/{tenant.id}",
            data={"name": "La Cantine 2", "phone_number": "+33612340001",
                  "business_type": "restaurant", "language": "fr-FR",
                  "greeting": "", "knowledge_base": "", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert tenants.get_by_id(tenant.id).name == "La Cantine 2"

        resp = client.post(f"/admin/tenants/{tenant.id}/delete",
                           data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert tenants.get_by_id(tenant.id) is None

    def test_duplicate_phone_shows_error(self, client, resto):
        tenant, _ = resto
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/admin/tenants",
            data={"name": "Doublon", "phone_number": tenant.phone_number, "csrf_token": token},
        )
        assert resp.status_code == 409
        assert "déjà utilisé" in resp.text


class TestRestaurateurScoping:
    def test_restaurateur_cannot_list_tenants(self, client, resto):
        _, user = resto
        _login(client, user.email, "resto-pass")
        assert client.get("/admin/tenants").status_code == 403

    def test_restaurateur_sees_only_own_reservations(self, client, resto):
        tenant, user = resto
        other = tenants.create_tenant("Autre", "+33699990001")
        try:
            _seed_resa(tenant.id, name="ChezMoi")
            _seed_resa(other.id, name="ChezLautre")
            _login(client, user.email, "resto-pass")
            page = client.get("/admin/reservations").text
            assert "ChezMoi" in page and "ChezLautre" not in page
        finally:
            tenants.delete_tenant(other.id)

    def test_restaurateur_cannot_touch_other_reservation(self, client, resto):
        tenant, user = resto
        other = tenants.create_tenant("Autre2", "+33699990002")
        try:
            rid = _seed_resa(other.id)
            _login(client, user.email, "resto-pass")
            token = _csrf(client)
            resp = client.post(f"/admin/reservations/{rid}/delete",
                               headers={"X-CSRF-Token": token})
            assert resp.status_code == 403
        finally:
            tenants.delete_tenant(other.id)

    def test_restaurateur_cannot_view_other_call(self, client, resto):
        tenant, user = resto
        other = tenants.create_tenant("Autre3", "+33699990003")
        try:
            calls.start_call("CA-scope-test", other.id)
            call = calls.list_calls(other.id)[0]
            _login(client, user.email, "resto-pass")
            assert client.get(f"/admin/calls/{call['id']}").status_code == 403
        finally:
            tenants.delete_tenant(other.id)

    def test_restaurateur_edits_own_tenant_but_not_phone(self, client, resto):
        tenant, user = resto
        _login(client, user.email, "resto-pass")
        token = _csrf(client)
        resp = client.post(
            f"/admin/tenants/{tenant.id}",
            data={"name": "Renommé", "phone_number": "+33000000000",
                  "business_type": "restaurant", "language": "fr-FR",
                  "greeting": "", "knowledge_base": "", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = tenants.get_by_id(tenant.id)
        assert updated.name == "Renommé"
        assert updated.phone_number == tenant.phone_number  # numéro inchangé (SA seul)


class TestReservationsInline:
    def test_edit_fragment_and_update(self, client, resto):
        tenant, _ = resto
        rid = _seed_resa(tenant.id)
        _login(client)
        frag = client.get(f"/admin/reservations/{rid}/edit")
        assert frag.status_code == 200 and "<form" in frag.text

        token = _csrf(client)
        resp = client.post(
            f"/admin/reservations/{rid}",
            data={"customer_name": "Marcel", "customer_phone": "", "date": "2026-08-01",
                  "time": "19:30", "party_size": "4", "notes": "terrasse"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200 and "Marcel" in resp.text
        assert reservations.get_reservation(rid)["party_size"] == 4


class TestCallsViews:
    def test_journal_and_detail(self, client, resto):
        tenant, _ = resto
        calls.start_call("CA-view-test", tenant.id)
        calls.finish_call("CA-view-test", "completed",
                          [{"role": "user", "content": "Bonjour je veux réserver"}])
        _login(client)
        page = client.get(f"/admin/calls?tenant_id={tenant.id}")
        assert page.status_code == 200 and "completed" in page.text
        call = calls.list_calls(tenant.id)[0]
        detail = client.get(f"/admin/calls/{call['id']}")
        assert detail.status_code == 200
        assert "Bonjour je veux réserver" in detail.text


class TestDashboard:
    def test_dashboard_and_charts(self, client, resto):
        tenant, _ = resto
        calls.start_call("CA-dash-test", tenant.id)
        calls.finish_call("CA-dash-test")
        _login(client)
        assert client.get("/admin/").status_code == 200
        charts_resp = client.get(f"/admin/stats/charts?tenant_id={tenant.id}")
        assert charts_resp.status_code == 200
        assert "<svg" in charts_resp.text


def _seed_resa(tenant_id: int, name: str = "Testeur") -> int:
    return reservations.create_reservation(tenant_id, name, "2026-08-01", "20:00", 2)["id"]
