"""Données personnelles : conservation, purge, effacement, information (#22).

Une durée de conservation écrite dans un registre et jamais appliquée est PIRE que pas
de durée du tout : elle donne une réponse fausse à qui la demande. Ces tests vérifient
donc que la purge efface vraiment, qu'elle n'efface que ce qu'elle doit, et qu'aucun
chemin de décroché ne saute la mention d'information.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import db, rgpd, tenants


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "rgpd.db")):
        db.init_db()
        yield


@pytest.fixture()
def resto(base):
    return tenants.create_tenant("Chez RGPD", "+33199000555")


def _jadis(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _appel(tenant_id, sid, *, jours, numero="+33612345678", transcript='[{"role":"user"}]'):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, started_at, caller_number,
                                  transcript, summary, duration_seconds, estimated_cost)
               VALUES (?, ?, ?, ?, ?, 'résumé', 120, 0.12)""",
            (sid, tenant_id, _jadis(jours), numero, transcript),
        )


def _reservation(tenant_id, *, jours_avant, nom="Dupont", tel="+33612345678"):
    date = (datetime.now(timezone.utc) - timedelta(days=jours_avant)).date().isoformat()
    with db.get_conn() as conn:
        return conn.execute(
            """INSERT INTO reservations (tenant_id, customer_name, customer_phone,
                                         date, time, party_size)
               VALUES (?, ?, ?, ?, '20:00', 2)""",
            (tenant_id, nom, tel, date),
        ).lastrowid


def _lire(sid):
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM calls WHERE call_sid = ?", (sid,)).fetchone())


class TestPurgeDesAppels:
    def test_un_transcript_ancien_disparait(self, resto):
        _appel(resto.id, "A-vieux", jours=40)
        rgpd.purger()
        ligne = _lire("A-vieux")
        assert ligne["transcript"] is None and ligne["summary"] is None

    def test_un_transcript_recent_reste(self, resto):
        _appel(resto.id, "A-neuf", jours=3)
        rgpd.purger()
        assert _lire("A-neuf")["transcript"] is not None

    def test_la_ligne_d_appel_survit_a_l_anonymisation(self, resto):
        """Supprimer les lignes détruirait l'historique de facturation : le compteur de
        forfait (#31) compte des lignes dans `calls`. On vide les champs, pas la ligne."""
        _appel(resto.id, "A-vieux", jours=400)
        rgpd.purger()
        ligne = _lire("A-vieux")
        assert ligne["caller_number"] is None      # l'appelant redevient inconnu…
        assert ligne["duration_seconds"] == 120    # …l'appel reste comptable
        assert ligne["estimated_cost"] == 0.12
        assert ligne["tenant_id"] == resto.id

    def test_le_numero_survit_au_transcript(self, resto):
        """Le transcript ne sert qu'au diagnostic ; le numéro sert à rappeler un client.
        Deux usages, deux durées."""
        _appel(resto.id, "A-median", jours=45)  # > 30 jours, < 90 jours
        rgpd.purger()
        ligne = _lire("A-median")
        assert ligne["transcript"] is None
        assert ligne["caller_number"] == "+33612345678"

    def test_les_durees_sont_reglables(self, resto, monkeypatch):
        monkeypatch.setenv("RETENTION_TRANSCRIPT_JOURS", "1")
        _appel(resto.id, "A-hier", jours=2)
        rgpd.purger()
        assert _lire("A-hier")["transcript"] is None


class TestPurgeDesReservations:
    def test_une_reservation_ancienne_est_supprimee(self, resto):
        _reservation(resto.id, jours_avant=400)
        rgpd.purger()
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0

    def test_une_reservation_a_venir_est_epargnee(self, resto):
        """Purger sur la DATE et non sur la création : une table réservée six mois à
        l'avance ne doit pas disparaître avant d'avoir eu lieu."""
        _reservation(resto.id, jours_avant=-180)  # dans 6 mois
        rgpd.purger()
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


class TestPurgeElleMeme:
    def test_idempotente(self, resto):
        _appel(resto.id, "A-vieux", jours=400)
        premier = rgpd.purger()
        second = rgpd.purger()
        assert premier.total > 0
        assert second.total == 0, "relancée aussitôt, la purge ne doit plus rien trouver"

    def test_elle_compte_ce_qu_elle_fait(self, resto):
        """Des compteurs, pas un « ok » : une purge qui ne trouve rien et une purge qui
        ne tourne pas se ressemblent trop."""
        for i in range(3):
            _appel(resto.id, f"A{i}", jours=400)
        _reservation(resto.id, jours_avant=400)
        r = rgpd.purger()
        assert r.transcripts == 3 and r.numeros == 3 and r.reservations == 1

    def test_elle_laisse_une_trace_verifiable(self, resto):
        """Sans trace, « on conserve 30 jours » reste une intention. La supervision lit
        cette trace (#24)."""
        _appel(resto.id, "A-vieux", jours=400)
        rgpd.purger()
        valeur, quand = rgpd.derniere_purge()
        assert valeur["transcripts"] == 1
        assert quand is not None


class TestDroitALEffacement:
    def test_efface_appels_et_reservations_d_un_numero(self, resto):
        _appel(resto.id, "A-lui", jours=1, numero="+33611111111")
        _appel(resto.id, "A-autre", jours=1, numero="+33622222222")
        _reservation(resto.id, jours_avant=1, tel="+33611111111")
        _reservation(resto.id, jours_avant=1, tel="+33622222222")

        rgpd.effacer_appelant("+33611111111")

        assert _lire("A-lui")["caller_number"] is None
        assert _lire("A-lui")["transcript"] is None
        assert _lire("A-autre")["caller_number"] == "+33622222222"  # l'autre est intact
        with db.get_conn() as conn:
            restants = [r[0] for r in conn.execute(
                "SELECT customer_phone FROM reservations").fetchall()]
        assert restants == ["+33622222222"]

    @pytest.mark.parametrize("vide", ["", "   ", None])
    def test_refuse_d_effacer_au_hasard(self, base, vide):
        """Un numéro vide effacerait toutes les lignes dont `caller_number` est NULL —
        c'est-à-dire précisément celles déjà anonymisées, plus rien d'identifiable, mais
        surtout un `DELETE` non maîtrisé sur les réservations."""
        with pytest.raises(ValueError):
            rgpd.effacer_appelant(vide)


class TestMentionDInformation:
    class _Tenant:
        greeting = "Bonjour, restaurant Le Test."

    def test_la_mention_suit_l_accueil(self):
        texte = rgpd.accueil(self._Tenant())
        assert texte.startswith("Bonjour, restaurant Le Test.")
        assert "assistant vocal" in texte

    def test_desactivable_mais_par_decision_explicite(self, monkeypatch):
        monkeypatch.setenv("RGPD_MENTION", "0")
        assert rgpd.accueil(self._Tenant()) == "Bonjour, restaurant Le Test."

    def test_active_par_defaut(self, monkeypatch):
        monkeypatch.delenv("RGPD_MENTION", raising=False)
        assert rgpd.mention_active() is True

    def test_aucun_chemin_de_decroche_ne_saute_la_mention(self):
        """Le vrai risque n'est pas d'oublier la mention, c'est qu'UN chemin sur quatre
        l'oublie — et que ce soit celui qu'un vrai appel emprunte. On vérifie donc que
        plus aucun code ne parle `tenant.greeting` directement."""
        import pathlib
        import re

        racine = pathlib.Path(__file__).resolve().parents[1] / "app"
        fautifs = []
        for source in racine.rglob("*.py"):
            if source.name == "rgpd.py":
                continue  # c'est lui qui compose le texte
            for numero, ligne in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"TTSSpeakFrame\(tenant\.greeting|_say_and_gather\(tenant\.greeting"
                             r"|_render_pcm\(tenant\.greeting", ligne):
                    fautifs.append(f"{source.name}:{numero}")
        assert not fautifs, (
            "ces chemins prononcent l'accueil sans la mention d'information : " 
            + ", ".join(fautifs))

    def test_la_mention_entre_dans_la_cle_de_cache(self, monkeypatch):
        """Sinon, modifier la mention laisserait tourner les WAV déjà rendus — l'ancienne
        version continuerait d'être jouée, en silence, indéfiniment."""
        from app.voice import greeting as greeting_mod

        tenant = tenants.Tenant(
            id=1, name="T", business_type="restaurant", phone_number="+33100000001",
            language="fr-FR", greeting="Bonjour.", knowledge_base="")
        monkeypatch.setenv("RGPD_MENTION", "1")
        avec = greeting_mod._cache_path(tenant)
        monkeypatch.setenv("RGPD_MENTION", "0")
        sans = greeting_mod._cache_path(tenant)
        assert avec != sans
