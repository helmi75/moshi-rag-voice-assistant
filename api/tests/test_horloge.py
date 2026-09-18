"""L'heure du restaurant (app/horloge.py) : les bornes se calculent à Paris, se stockent en UTC.

Le piège que ces tests verrouillent : entre minuit et deux heures, heure de Paris, le
calendrier UTC est encore la veille. Un appel à 00 h 30 le 1er octobre tombait en
septembre pour la facture ; « aujourd'hui » désignait hier."""
from datetime import date, datetime, timezone

from app import horloge


def _fixe(monkeypatch, *args):
    monkeypatch.setattr(horloge, "maintenant",
                        lambda: datetime(*args, tzinfo=horloge.FUSEAU))


class TestBornes:
    def test_minuit_paris_devient_la_veille_en_utc_l_ete(self, monkeypatch):
        _fixe(monkeypatch, 2026, 7, 1, 12, 0)
        assert horloge.il_y_a_jours(0) == "2026-06-30T22:00:00Z"
        assert horloge.il_y_a_jours(7) == "2026-06-23T22:00:00Z"

    def test_le_premier_du_mois_a_minuit_et_demi_reste_dans_le_mois(self, monkeypatch):
        """00 h 30 à Paris le 1er octobre = 22 h 30 UTC le 30 septembre. La borne du mois
        doit être 30/09 22:00 UTC : l'appel compte bien dans le mois d'octobre."""
        _fixe(monkeypatch, 2026, 10, 1, 0, 30)
        assert horloge.aujourd_hui() == date(2026, 10, 1)
        assert horloge.debut_du_mois() == "2026-09-30T22:00:00Z"
        assert horloge.il_y_a_jours(0) == "2026-09-30T22:00:00Z"

    def test_le_changement_d_heure_ne_decale_pas_la_borne(self, monkeypatch):
        """29 mars 2026 : passage à l'heure d'été à 2 h. La veille, minuit Paris était à
        23 h UTC ; le lendemain aussi (c'est le 29 après 2 h que l'écart devient 2 h)."""
        _fixe(monkeypatch, 2026, 3, 29, 0, 30)
        assert horloge.il_y_a_jours(0) == "2026-03-28T23:00:00Z"
        assert horloge.il_y_a_jours(1) == "2026-03-27T23:00:00Z"
        _fixe(monkeypatch, 2026, 3, 30, 12, 0)
        assert horloge.il_y_a_jours(0) == "2026-03-29T22:00:00Z"

    def test_en_hiver_la_borne_est_a_23h_utc(self, monkeypatch):
        _fixe(monkeypatch, 2026, 12, 15, 9, 0)
        assert horloge.il_y_a_jours(0) == "2026-12-14T23:00:00Z"
        assert horloge.debut_du_mois() == "2026-11-30T23:00:00Z"


class TestLecture:
    def test_lire_utc_tolere_les_formes_sqlite(self):
        attendu = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
        assert horloge.lire_utc("2026-09-30T22:00:00Z") == attendu
        assert horloge.lire_utc("2026-09-30 22:00:00Z") == attendu
        assert horloge.lire_utc("2026-09-30T22:00:00") == attendu  # naïf = UTC

    def test_lire_utc_ne_leve_jamais(self):
        assert horloge.lire_utc(None) is None
        assert horloge.lire_utc("") is None
        assert horloge.lire_utc("pas une date") is None

    def test_utc_iso_convertit_un_instant_de_paris(self):
        paris = datetime(2026, 10, 1, 0, 30, tzinfo=horloge.FUSEAU)
        assert horloge.utc_iso(paris) == "2026-09-30T22:30:00Z"
        assert horloge.lire_utc(horloge.utc_iso(paris)) == paris

    def test_en_toutes_lettres(self):
        assert horloge.en_toutes_lettres(date(2026, 8, 14)) == "vendredi 14 août 2026"
        assert horloge.en_toutes_lettres(date(2026, 12, 1)).endswith("décembre 2026")
