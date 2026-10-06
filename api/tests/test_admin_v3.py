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


class TestProchainesReservations:
    """Recette du 05/10/2026 : avec plus de six tables à venir, la carte « Prochaines
    réservations » montrait les six plus LOINTAINES et aucune du jour — le tri allait de
    la plus tardive à la plus proche avant de n'en garder que six."""

    def _tables(self, tenant_id, jours):
        from app import horloge
        from datetime import timedelta
        for n in jours:
            jour = (horloge.aujourd_hui() + timedelta(days=n)).isoformat()
            reservations.create_reservation(tenant_id, f"Client J+{n}", jour, "20:00", 2)

    def test_les_plus_proches_d_abord(self, resto):
        tenant, _ = resto
        self._tables(tenant.id, [9, 0, 5, 1, 7, 3, 8, 2])
        from app import horloge
        vues = reservations.list_filtered(tenant_id=tenant.id, limit=6,
                                          date_from=horloge.aujourd_hui().isoformat(),
                                          plus_proches_d_abord=True)
        assert [r["customer_name"] for r in vues] == [
            "Client J+0", "Client J+1", "Client J+2", "Client J+3", "Client J+5", "Client J+7"]

    def test_le_meme_jour_l_heure_la_plus_proche_d_abord(self, resto):
        tenant, _ = resto
        from app import horloge
        jour = horloge.aujourd_hui().isoformat()
        reservations.create_reservation(tenant.id, "Soir", jour, "20:30", 2)
        reservations.create_reservation(tenant.id, "Midi", jour, "12:15", 2)
        vues = reservations.list_filtered(tenant_id=tenant.id, date_from=jour,
                                          plus_proches_d_abord=True)
        assert [r["customer_name"] for r in vues] == ["Midi", "Soir"]

    def _il_est(self, monkeypatch, heure: int) -> None:
        """Aujourd'hui, à l'heure dite : la carte ne montre que les tables à venir."""
        from app import horloge
        reel = horloge.maintenant()
        monkeypatch.setattr(horloge, "maintenant",
                            lambda: reel.replace(hour=heure, minute=0, second=0, microsecond=0))

    def test_le_jour_meme_seulement_a_partir_de_l_heure_donnee(self, resto):
        tenant, _ = resto
        from app import horloge
        from datetime import timedelta
        jour = horloge.aujourd_hui()
        for nom, quand, heure in (("Midi", jour, "12:15"), ("Soir", jour, "20:30"),
                                  ("Pile", jour, "19:00"), ("Demain midi", jour + timedelta(days=1), "12:15")):
            reservations.create_reservation(tenant.id, nom, quand.isoformat(), heure, 2)
        vues = reservations.list_filtered(tenant_id=tenant.id, date_from=jour.isoformat(),
                                          heure_from="19:00", plus_proches_d_abord=True)
        # L'heure ne borne que le jour même : le déjeuner de demain reste « à venir ».
        assert [r["customer_name"] for r in vues] == ["Pile", "Soir", "Demain midi"]

    def test_a_19_h_les_dejeuners_servis_ne_cachent_pas_le_diner(self, client, resto, monkeypatch):
        """Six déjeuners suffisaient à remplir la carte : à 19 h elle ne montrait que des
        tables déjà servies, aucune du soir ni du lendemain."""
        tenant, _ = resto
        from app import horloge
        jour = horloge.aujourd_hui().isoformat()
        for i in range(7):
            reservations.create_reservation(tenant.id, f"Déjeuner {i}", jour, f"12:{10 + i}", 2)
        reservations.create_reservation(tenant.id, "Dîner de ce soir", jour, "20:30", 4)
        self._tables(tenant.id, [1])
        self._il_est(monkeypatch, 19)
        _login(client)
        page = client.get(f"/admin/?tenant_id={tenant.id}").text
        carte = page[page.index("Prochaines réservations"):]
        assert "Dîner de ce soir" in carte and "Client J+1" in carte
        assert "Déjeuner" not in carte

    def test_la_salle_de_controle_montre_celles_du_jour(self, client, resto, monkeypatch):
        tenant, _ = resto
        self._tables(tenant.id, [9, 0, 5, 1, 7, 3, 8, 2])
        self._il_est(monkeypatch, 10)        # avant le service : la table de 20 h est à venir
        _login(client)
        page = client.get(f"/admin/?tenant_id={tenant.id}").text
        carte = page[page.index("Prochaines réservations"):]
        assert "Client J+0" in carte and "Client J+1" in carte
        assert "Client J+9" not in carte and "Client J+8" not in carte
        assert carte.index("Client J+0") < carte.index("Client J+1") < carte.index("Client J+7")


class TestUneLongueNoteNeSortPasDeLaCarte:
    def test_la_note_passe_sous_la_date_et_reste_lisible_en_entier(self, client, resto, monkeypatch):
        from app import horloge
        tenant, _ = resto
        # Avant le service : la carte ne montre que les tables à venir, celle de 20 h en est.
        reel = horloge.maintenant()
        monkeypatch.setattr(horloge, "maintenant", lambda: reel.replace(hour=10, minute=0))
        note = "Anniversaire, gâteau à la bougie, une chaise haute et une table près de la fenêtre"
        reservations.create_reservation(tenant.id, "Durand", horloge.aujourd_hui().isoformat(),
                                        "20:00", 6, notes=note)
        _login(client)
        page = client.get(f"/admin/?tenant_id={tenant.id}").text
        assert f'<span class="chip chip-warn chip-note" title="{note}">{note}</span>' in page
        feuille = open(__file__.replace("tests/test_admin_v3.py", "app/admin/static/admin.css"),
                       encoding="utf-8").read()
        regle = feuille[feuille.index(".chip-note {"):]
        regle = regle[:regle.index("}")]
        assert "text-overflow: ellipsis" in regle and "max-width: 100%" in regle
        # Sous la date, dans la colonne du nom : elle ne lui dispute plus la largeur.
        ligne = page[page.index("Durand"):page.index(note)]
        assert "couvert(s)</span>" in ligne and "</span>\n        </span>" not in ligne


class TestLAdminSansBarreFinale:
    """Derrière Caddy, l'application se voit en http : la redirection automatique de
    `/admin` vers `/admin/` partait donc vers `http://app.helmane.fr/admin/` (recette du
    05/10/2026). Une adresse relative garde le https du visiteur."""

    def test_la_redirection_ne_quitte_pas_le_https(self, client):
        reponse = client.get("/admin", follow_redirects=False)
        assert reponse.status_code == 307
        assert reponse.headers["location"] == "/admin/"


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


class TestLatencyMeasurement:
    """La latence est la seule métrique de la maquette v3 que j'avais refusé
    d'afficher faute de mesure. Elle existe désormais (migration v4) — et doit rester
    absente tant qu'aucun appel n'a été instrumenté, jamais remplacée par une valeur
    plausible."""

    def test_stored_and_aggregated(self, two_tenants):
        a, _ = two_tenants
        calls.start_call("CA-lat-1", a.id)
        calls.finish_call("CA-lat-1", "completed", turn_latencies=[900, 1100, 1300])
        calls.start_call("CA-lat-2", a.id)
        calls.finish_call("CA-lat-2", "completed", turn_latencies=[2000])

        stats = calls.latency_stats(a.id, days=30)
        assert stats["n_turns"] == 4
        assert stats["median_ms"] == 1200  # (1100 + 1300) / 2
        assert stats["p90_ms"] == 2000

    def test_none_when_nothing_measured(self, two_tenants):
        a, _ = two_tenants
        calls.start_call("CA-lat-vide", a.id)
        calls.finish_call("CA-lat-vide", "completed")
        assert calls.latency_stats(a.id, days=30) is None

    def test_corrupt_payload_is_ignored(self, two_tenants):
        """Une ligne abîmée ne doit pas faire tomber l'écran Santé."""
        a, _ = two_tenants
        calls.start_call("CA-lat-ko", a.id)
        calls.finish_call("CA-lat-ko", "completed", turn_latencies=[1000])
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET turn_latencies = 'pas du json' WHERE call_sid = ?",
                         ("CA-lat-ko",))
        assert calls.latency_stats(a.id, days=30) is None

    def test_scoped_by_tenant(self, two_tenants):
        a, b = two_tenants
        calls.start_call("CA-lat-a", a.id)
        calls.finish_call("CA-lat-a", "completed", turn_latencies=[500])
        assert calls.latency_stats(b.id, days=30) is None
        assert calls.latency_stats(a.id, days=30)["median_ms"] == 500
