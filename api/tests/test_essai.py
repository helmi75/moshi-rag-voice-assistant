"""L'établissement d'essai de la recette automatique (appels lancés sans téléphone).

Ses appels portent de vrais identifiants Twilio (`CA…`) : le préfixe du banc ne les
distingue pas. Ils ne doivent fausser ni les chiffres du parc, ni les quotas, ni la
supervision, et ils sont effacés après chaque recette, sans jamais toucher un autre
établissement.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import calls, db, essai, messages, quotas, reservations, supervision, tenants
from app.voice import enregistrement


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path / "audio"))
    with patch.object(db, "DB_PATH", str(tmp_path / "essai.db")):
        db.init_db()
        supervision.vider_cache()
        yield
        supervision.vider_cache()


@pytest.fixture()
def deux(base):
    """(établissement d'essai, vrai établissement)."""
    return (tenants.create_tenant("Banc d'essai", "+33199000901"),
            tenants.create_tenant("Chez Vrai", "+33199000902"))


def _il_y_a(minutes: float) -> str:
    date = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")


def _appel(tenant_id, sid, *, status="completed", transcript=None, duree=60.0,
           cout=0.1, resa=False) -> int:
    resa_id = None
    if resa:
        resa_id = reservations.create_reservation(
            tenant_id, "Dupont", "2099-01-01", "20:00", 2)["id"]
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, started_at, ended_at,
                                  duration_seconds, status, transcript, estimated_cost,
                                  cout_telephonie, voix_fournisseur, reservation_id,
                                  recording_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'voxtral', ?, 800)""",
            (sid, tenant_id, _il_y_a(10), _il_y_a(9), duree, status,
             json.dumps(transcript) if transcript else None, cout, cout, resa_id))
        return cur.lastrowid


def _poser_audio(tenant_id, call_id):
    fichiers = []
    for piste in enregistrement.PISTES:
        chemin = enregistrement.chemin(tenant_id, call_id, piste)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(b"\xff" * 80)
        fichiers.append(chemin)
    return fichiers


def _compter(table, tenant_id):
    with db.get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?",
                            (tenant_id,)).fetchone()[0]


class TestReconnaissance:
    def test_sans_la_variable_aucun_etablissement_n_est_d_essai(self, deux, monkeypatch):
        monkeypatch.delenv("RECETTE_ETABLISSEMENT", raising=False)
        assert essai.etablissement_id() is None
        assert essai.est_d_essai(deux[0].id) is False

    @pytest.mark.parametrize("valeur", ["", "  ", "abc", "1.5"])
    def test_une_valeur_illisible_vaut_pas_d_essai(self, deux, monkeypatch, valeur):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", valeur)
        assert essai.etablissement_id() is None

    def test_la_variable_designe_l_etablissement(self, deux, monkeypatch):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(deux[0].id))
        assert essai.est_d_essai(deux[0].id) is True
        assert essai.est_d_essai(deux[1].id) is False


class TestExclusionDuParc:
    @pytest.fixture(autouse=True)
    def appels(self, deux, monkeypatch):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(deux[0].id))
        _appel(deux[0].id, "CA-essai", resa=True, cout=5.0)
        _appel(deux[1].id, "CA-vrai", resa=True, cout=0.1)
        self.essai, self.vrai = deux

    def test_totals_du_parc_ignore_l_essai(self):
        t = calls.totals(None)
        assert t["n_calls"] == 1 and t["n_reservations"] == 1
        assert t["total_cost"] == pytest.approx(0.1)

    def test_totals_de_l_etablissement_d_essai_le_montre(self):
        assert calls.totals(self.essai.id)["n_calls"] == 1

    def test_stats_daily_du_parc_ignore_l_essai(self):
        jours = calls.stats_daily(None)
        assert sum(j["n_calls"] for j in jours) == 1
        assert sum(j["n_reservations"] for j in jours) == 1
        assert sum(j["n_calls"] for j in calls.stats_daily(self.essai.id)) == 1

    def test_stats_by_tenant_ignore_l_essai(self):
        assert self.essai.id not in calls.stats_by_tenant()

    def test_par_moteur_ignore_l_essai(self):
        assert calls.par_moteur(None)["classique"]["n_calls"] == 1
        assert calls.par_moteur(self.essai.id)["classique"]["n_calls"] == 1

    def test_cost_breakdown_ignore_l_essai(self):
        total = sum(l["amount"] for l in calls.cost_breakdown(None))
        assert total == pytest.approx(0.1)
        assert sum(l["amount"] for l in calls.cost_breakdown(self.essai.id)) \
            == pytest.approx(5.0)

    def test_count_calls_ignore_l_essai(self):
        assert calls.count_calls(None) == 1
        assert calls.count_calls(self.essai.id) == 1

    def test_enregistrements_stats_ignore_l_essai(self):
        assert calls.enregistrements_stats()["clos"] == 1
        assert calls.enregistrements_stats(self.essai.id)["clos"] == 1

    def test_latency_stats_ignore_l_essai(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET turn_latencies = '[100]' WHERE tenant_id = ?",
                         (self.essai.id,))
        assert calls.latency_stats(None) is None
        assert calls.latency_stats(self.essai.id)["n_turns"] == 1

    def test_secours_recents_ignore_l_essai(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET secours_motif = 'voix', status = 'renvoye'")
        assert [a["tenant_id"] for a in calls.secours_recents(24)] == [self.vrai.id]

    def test_la_liste_des_appels_du_super_admin_les_garde(self):
        assert len(calls.list_calls(None)) == 2

    def test_sans_la_variable_rien_n_est_exclu(self, monkeypatch):
        monkeypatch.delenv("RECETTE_ETABLISSEMENT")
        assert calls.totals(None)["n_calls"] == 2
        assert calls.count_calls(None) == 2
        assert self.essai.id in calls.stats_by_tenant()


class TestQuotas:
    def test_l_etablissement_d_essai_n_a_pas_de_decompte(self, deux, monkeypatch):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(deux[0].id))
        for tenant in deux:
            with db.get_conn() as conn:
                conn.execute(
                    "INSERT INTO calls (call_sid, tenant_id, started_at, duration_seconds) "
                    "VALUES (?, ?, ?, 600)",
                    (f"CA-q{tenant.id}", tenant.id, datetime.now(timezone.utc).replace(
                        day=1, hour=12).strftime("%Y-%m-%dT%H:%M:%SZ")))
        par = quotas.etat_par_tenant(deux)
        assert par[deux[0].id].appels == 0
        assert par[deux[1].id].appels == 1
        assert quotas.etat(deux[0]).appels == 0
        assert all(a["tenant_id"] != deux[0].id for a in quotas.alertes(deux))


class TestSupervision:
    def test_un_appel_d_essai_echoue_ne_change_pas_le_verdict(self, deux, monkeypatch):
        for i in range(3):
            _appel(deux[0].id, f"CA-ko{i}", status="failed")
        monkeypatch.delenv("RECETTE_ETABLISSEMENT", raising=False)
        supervision.vider_cache()
        avant = {c["cle"]: c for c in supervision.etat(force=True)["controles"]}
        assert avant["appels_echoues"]["niveau"] == supervision.PANNE
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(deux[0].id))
        supervision.vider_cache()
        apres = {c["cle"]: c for c in supervision.etat(force=True)["controles"]}
        assert apres["appels_echoues"]["niveau"] == supervision.OK
        assert apres["appels_echoues"]["mesure"]["appels"] == 0
        assert apres["appels_muets"]["niveau"] == supervision.OK


class TestPurge:
    def _semer(self, tenant):
        cid = _appel(tenant.id, f"CA-p{tenant.id}", resa=True)
        messages.create_message(tenant.id, "Rappeler", call_id=cid)
        return cid, _poser_audio(tenant.id, cid)

    def test_purge_efface_l_essai_et_rien_d_autre(self, deux, monkeypatch):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(deux[0].id))
        _, fichiers_essai = self._semer(deux[0])
        _, fichiers_vrai = self._semer(deux[1])
        comptes = essai.purger()
        assert comptes == {"appels": 1, "reservations": 1, "messages": 1,
                           "enregistrements": 2}
        for table in ("calls", "reservations", "messages"):
            assert _compter(table, deux[0].id) == 0
            assert _compter(table, deux[1].id) == 1
        assert not any(f.exists() for f in fichiers_essai)
        assert all(f.exists() for f in fichiers_vrai)

    def test_purge_sans_variable_ne_supprime_rien(self, deux, monkeypatch):
        monkeypatch.delenv("RECETTE_ETABLISSEMENT", raising=False)
        _, fichiers = self._semer(deux[0])
        assert essai.purger() == {"appels": 0, "reservations": 0, "messages": 0,
                                  "enregistrements": 0}
        assert _compter("calls", deux[0].id) == 1
        assert all(f.exists() for f in fichiers)
