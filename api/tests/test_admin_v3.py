"""Admin v3 : agrégats de parc, fiches de connaissance, numéro appelant, écrans.

Ces tests protègent la promesse tenue par l'interface : **tout ce qui est affiché est
mesuré**. D'où les assertions sur la cohérence des agrégats (total du parc = somme des
établissements, ventilation du coût = total) plutôt que sur des libellés seuls.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import calls, db, reservations, tenants, users
from app.main import app


# ---------------------------------------------------------------------------
# Couche données (base isolée)
# ---------------------------------------------------------------------------
@pytest.fixture()
def fresh_db(tmp_path):
    path = str(tmp_path / "v3.db")
    with patch.object(db, "DB_PATH", path):
        db.init_db()
        yield path


@pytest.fixture()
def two_tenants(fresh_db):
    a = tenants.create_tenant("Alpha", "+33111111111")
    b = tenants.create_tenant("Beta", "+33122222222")
    return a, b


class TestCallerNumber:
    def test_start_call_stores_caller(self, two_tenants):
        a, _ = two_tenants
        calls.start_call("CA-caller", a.id, "+33612345678")
        assert calls.list_calls(a.id)[0]["caller_number"] == "+33612345678"

    def test_caller_optional(self, two_tenants):
        """Un appel sans `From` (webhook incomplet) ne doit pas échouer."""
        a, _ = two_tenants
        calls.start_call("CA-nocaller", a.id)
        assert calls.list_calls(a.id)[0]["caller_number"] is None


class TestTotals:
    def test_window_and_capture_rate(self, two_tenants):
        a, _ = two_tenants
        resa = reservations.create_reservation(a.id, "X", "2026-07-30", "20:00", 4)
        calls.start_call("CA-1", a.id)
        calls.finish_call("CA-1", "completed", None, reservation_id=resa["id"])
        calls.start_call("CA-2", a.id)
        calls.finish_call("CA-2", "completed")

        totals = calls.totals(a.id, days=30)
        assert totals["n_calls"] == 2
        assert totals["n_with_reservation"] == 1
        assert totals["capture_rate"] == 50
        assert totals["n_reservations"] == 1
        assert totals["n_covers"] == 4
        assert totals["avg_cost"] == pytest.approx(totals["total_cost"] / 2)

    def test_previous_window_excludes_today(self, two_tenants):
        """La fenêtre décalée ne doit PAS recompter la période courante — sinon
        l'« évolution » affichée se comparerait à elle-même."""
        a, _ = two_tenants
        calls.start_call("CA-now", a.id)
        calls.finish_call("CA-now", "completed")
        assert calls.totals(a.id, days=30)["n_calls"] == 1
        assert calls.totals(a.id, days=30, offset_days=30)["n_calls"] == 0

    def test_failed_and_unfinished_are_distinct(self, two_tenants):
        a, _ = two_tenants
        calls.start_call("CA-ko", a.id)
        calls.finish_call("CA-ko", "failed")
        calls.start_call("CA-open", a.id)  # jamais clôturé (worker tué)
        totals = calls.totals(a.id, days=30)
        assert totals["n_failed"] == 1
        assert totals["n_unfinished"] == 1

    def test_empty_scope(self, two_tenants):
        a, _ = two_tenants
        totals = calls.totals(a.id, days=30)
        assert totals["n_calls"] == 0
        assert totals["capture_rate"] == 0
        assert totals["avg_cost"] == 0.0


class TestStatsByTenant:
    def test_park_total_equals_sum_of_venues(self, two_tenants):
        a, b = two_tenants
        for sid, tenant in (("CA-a1", a), ("CA-a2", a), ("CA-b1", b)):
            calls.start_call(sid, tenant.id)
            calls.finish_call(sid, "completed")

        per_tenant = calls.stats_by_tenant(days=30)
        park = calls.totals(None, days=30)
        assert per_tenant[a.id]["n_calls"] == 2
        assert per_tenant[b.id]["n_calls"] == 1
        assert sum(s["n_calls"] for s in per_tenant.values()) == park["n_calls"]
        assert sum(s["total_cost"] for s in per_tenant.values()) == pytest.approx(
            park["total_cost"]
        )

    def test_tenant_without_calls_is_absent(self, two_tenants):
        a, b = two_tenants
        calls.start_call("CA-only-a", a.id)
        assert b.id not in calls.stats_by_tenant(days=30)


class TestCostBreakdown:
    def test_shares_sum_to_the_total(self, two_tenants):
        a, _ = two_tenants
        calls.start_call("CA-cost", a.id)
        calls.finish_call("CA-cost", "completed")
        rows = calls.cost_breakdown(None, days=30)
        total = calls.totals(None, days=30)["total_cost"]
        assert sum(r["amount"] for r in rows) == pytest.approx(total, rel=1e-6)
        assert sum(r["share"] for r in rows) == pytest.approx(100, abs=2)


class TestOutcomeFilter:
    def test_filters_use_real_columns(self, two_tenants):
        a, _ = two_tenants
        resa = reservations.create_reservation(a.id, "Y", "2026-07-30", "20:00", 2)
        calls.start_call("CA-r", a.id)
        calls.finish_call("CA-r", "completed", None, reservation_id=resa["id"])
        calls.start_call("CA-i", a.id)
        calls.finish_call("CA-i", "completed")
        calls.start_call("CA-f", a.id)
        calls.finish_call("CA-f", "failed")

        assert len(calls.list_calls(a.id, outcome="reservation")) == 1
        assert len(calls.list_calls(a.id, outcome="info")) == 1
        assert len(calls.list_calls(a.id, outcome="failed")) == 1
        assert len(calls.list_calls(a.id, outcome="inconnu")) == 3  # filtre ignoré


class TestCoversBySlot:
    def test_groups_and_sums_party_size(self, two_tenants):
        a, _ = two_tenants
        reservations.create_reservation(a.id, "A", "2026-07-30", "20:00", 2)
        reservations.create_reservation(a.id, "B", "2026-07-30", "20:00", 4)
        reservations.create_reservation(a.id, "C", "2026-07-30", "21:00", 3)
        slots = reservations.covers_by_slot(a.id, "2026-07-30")
        assert [(s["time"], s["covers"]) for s in slots] == [("20:00", 6), ("21:00", 3)]
        assert reservations.covers_by_slot(a.id, "2026-07-31") == []


class TestKnowledgeSections:
    def test_splits_on_markdown_titles(self):
        sections = tenants.parse_knowledge_sections(
            "## Horaires\nOuvert du mardi au dimanche, midi et soir.\n\n## Carte\n"
        )
        assert [s["title"] for s in sections] == ["Horaires", "Carte"]
        assert sections[0]["filled"] is True
        assert sections[1]["filled"] is False  # section vide = à compléter

    def test_preamble_becomes_a_section(self):
        sections = tenants.parse_knowledge_sections("Texte libre sans aucun titre.")
        assert len(sections) == 1 and sections[0]["title"] == "Général"

    def test_empty_base_has_no_section(self):
        assert tenants.parse_knowledge_sections("") == []
        assert tenants.parse_knowledge_sections(None) == []

    def test_title_only_base_has_no_phantom_preamble(self):
        sections = tenants.parse_knowledge_sections("## Menus\n78 € la formule.")
        assert len(sections) == 1 and sections[0]["title"] == "Menus"


# ---------------------------------------------------------------------------
# Écrans (base de test partagée, comme test_admin_routes)
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant(
        "Chez V3", f"+3362{id(object()) % 10_000_000:07d}",
        knowledge_base="## Horaires\nOuvert tous les jours de 12h à 23h.\n\n## Groupes\n",
    )
    user = users.create_user(
        f"v3-{tenant.id}@test.fr", "resto-pass", users.ROLE_RESTAURATEUR, tenant.id
    )
    yield tenant, user
    tenants.delete_tenant(tenant.id)


def _login(client, email="admin@test.local", password="test-admin-pass"):
    resp = client.post("/admin/login", data={"email": email, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


class TestHealthScreen:
    def test_superadmin_sees_configuration_not_latency(self, client):
        _login(client)
        page = client.get("/admin/health")
        assert page.status_code == 200
        assert "Pile vocale" in page.text
        # Aucune latence n'est instrumentée : l'écran ne doit pas en inventer une.
        assert "Latence médiane" not in page.text

    def test_restaurateur_is_forbidden(self, client, resto):
        tenant, user = resto
        _login(client, user.email, "resto-pass")
        assert client.get("/admin/health").status_code == 403


class TestParkScreen:
    def test_lists_venues_with_real_aggregates(self, client, resto):
        tenant, _ = resto
        calls.start_call("CA-park", tenant.id, "+33600000001")
        calls.finish_call("CA-park", "completed")
        _login(client)
        page = client.get("/admin/")
        assert page.status_code == 200
        assert "Vue du parc" in page.text and tenant.name in page.text

    def test_superadmin_can_switch_to_a_venue_view(self, client, resto):
        tenant, _ = resto
        _login(client)
        page = client.get(f"/admin/?tenant_id={tenant.id}")
        assert page.status_code == 200 and "Salle de contrôle" in page.text

    def test_restaurateur_lands_on_control_room(self, client, resto):
        tenant, user = resto
        _login(client, user.email, "resto-pass")
        page = client.get("/admin/")
        assert page.status_code == 200
        assert "Salle de contrôle" in page.text and "Vue du parc" not in page.text


class TestCallsScreen:
    def test_caller_number_is_displayed(self, client, resto):
        tenant, _ = resto
        calls.start_call("CA-shown", tenant.id, "+33698765432")
        calls.finish_call("CA-shown", "completed",
                          [{"role": "user", "content": "Une table pour deux"}])
        _login(client)
        page = client.get(f"/admin/calls?tenant_id={tenant.id}")
        assert "+33698765432" in page.text
        assert "Une table pour deux" in page.text  # extrait = 1re phrase du client

    def test_unknown_caller_is_labelled_not_invented(self, client, resto):
        tenant, _ = resto
        calls.start_call("CA-anon", tenant.id)
        calls.finish_call("CA-anon", "completed")
        _login(client)
        page = client.get(f"/admin/calls?tenant_id={tenant.id}")
        assert "Numéro inconnu" in page.text

    def test_outcome_filter_is_scoped(self, client, resto):
        tenant, user = resto
        calls.start_call("CA-filter", tenant.id, "+33611111111")
        calls.finish_call("CA-filter", "failed")
        _login(client, user.email, "resto-pass")
        page = client.get("/admin/calls?outcome=failed")
        assert page.status_code == 200 and "+33611111111" in page.text
        assert "Aucun appel" not in page.text


class TestKnowledgeScreen:
    def test_sections_are_rendered_as_cards(self, client, resto):
        tenant, user = resto
        _login(client, user.email, "resto-pass")
        page = client.get(f"/admin/tenants/{tenant.id}/knowledge")
        assert page.status_code == 200
        assert "Horaires" in page.text and "Groupes" in page.text
        assert "À compléter" in page.text  # la section « Groupes » est vide

    def test_cross_tenant_is_forbidden(self, client, resto):
        tenant, user = resto
        other = tenants.create_tenant("Autre V3", f"+3363{id(object()) % 10_000_000:07d}")
        try:
            _login(client, user.email, "resto-pass")
            assert client.get(f"/admin/tenants/{other.id}/knowledge").status_code == 403
        finally:
            tenants.delete_tenant(other.id)


class TestShellDoesNotShadowRouteContext:
    """Régression : les `{% set %}` de la coquille (base.html) vivent dans le contexte
    PARTAGÉ avec les blocs enfants. Nommés `tenant`/`user`, ils écrasaient les
    variables passées par la route — d'où un titre vide et des liens
    `/admin/tenants//edit` pour le super-admin, dont le state.tenant est None."""

    @pytest.mark.parametrize("screen", ["voice", "knowledge", "edit"])
    def test_tenant_of_the_route_wins(self, client, resto, screen):
        tenant, _ = resto
        _login(client)
        page = client.get(f"/admin/tenants/{tenant.id}/{screen}")
        assert page.status_code == 200
        assert tenant.name in page.text
        assert "/admin/tenants//" not in page.text


class TestReservationRowScoping:
    def test_inline_edit_does_not_leak_other_tenants(self, client, resto):
        """Régression : le fragment de ligne renvoyait la colonne « Établissement »
        même à un restaurateur — colonnes décalées ET nom d'un autre client visible."""
        tenant, user = resto
        other = tenants.create_tenant("Voisin Secret", f"+3364{id(object()) % 10_000_000:07d}")
        resa = reservations.create_reservation(tenant.id, "Client", "2026-07-30", "20:00", 2)
        try:
            _login(client, user.email, "resto-pass")
            row = client.get(f"/admin/reservations/{resa['id']}/row")
            assert row.status_code == 200
            assert "Voisin Secret" not in row.text
            assert tenant.name not in row.text
        finally:
            tenants.delete_tenant(other.id)
