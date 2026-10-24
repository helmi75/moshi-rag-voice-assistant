"""La tournée quotidienne (ASSISTANTE-172) et le contrôle du crédit OpenRouter. Aucun réseau :
OpenRouter et GitHub sont doublés, l'e-mail aussi."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import db, horloge, supervision, tenants, tournee, users


@pytest.fixture(autouse=True)
def cache_vide():
    supervision.vider_cache()
    yield
    supervision.vider_cache()


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "cle-de-test")
    with patch.object(db, "DB_PATH", str(tmp_path / "tournee.db")):
        db.init_db()
        yield


class _Reponse:
    def __init__(self, statut, corps):
        self.status_code, self._corps = statut, corps

    def json(self):
        return self._corps


class FauxClient:
    """Répond par hôte ; garde les requêtes pour qu'un test les relise."""

    def __init__(self, openrouter=(200, {"data": {"total_credits": 15, "total_usage": 8.01}}),
                 github=(200, {"workflow_runs": []})):
        self.reponses = {"openrouter.ai": openrouter, "api.github.com": github}
        self.requetes = []

    async def get(self, url, headers=None, params=None):
        self.requetes.append((url, headers or {}, params or {}))
        hote = url.split("/")[2]
        valeur = self.reponses[hote]
        if isinstance(valeur, Exception):
            raise valeur
        return _Reponse(*valeur)


def _controle(cle):
    return next(c for c in supervision.etat(force=True)["controles"] if c["cle"] == cle)


def _relever(client):
    asyncio.run(supervision.relever_credit_openrouter(client))


class TestLeCreditOpenRouter:
    def test_le_solde_est_l_achete_moins_le_consomme(self, base):
        _relever(FauxClient())
        c = _controle("credit_openrouter")
        assert c["niveau"] == supervision.OK and c["mesure"]["solde"] == 6.99

    def test_sous_le_seuil_de_trois_dollars_il_faut_recharger(self, base):
        _relever(FauxClient(openrouter=(200, {"data": {"total_credits": 15, "total_usage": 12.5}})))
        c = _controle("credit_openrouter")
        assert c["niveau"] == supervision.ATTENTION and "2.50" in c["resume"]

    def test_epuise_c_est_une_panne(self, base):
        _relever(FauxClient(openrouter=(200, {"data": {"total_credits": 15, "total_usage": 15.2}})))
        assert _controle("credit_openrouter")["niveau"] == supervision.PANNE

    @pytest.mark.parametrize("reponse", [(403, {}), (200, {"data": {}}), TimeoutError()])
    def test_une_releve_ratee_n_est_pas_un_feu_vert(self, base, reponse):
        _relever(FauxClient(openrouter=reponse))
        assert _controle("credit_openrouter")["niveau"] == supervision.ATTENTION

    def test_une_releve_figee_le_dit(self, base):
        _relever(FauxClient())
        vieux = horloge.utc_iso(datetime.now(tz=horloge.FUSEAU) - timedelta(hours=30))
        with db.get_conn() as conn:
            conn.execute("UPDATE supervision SET maj_le = ? WHERE cle = 'credit_openrouter'", (vieux,))
        c = _controle("credit_openrouter")
        assert c["niveau"] == supervision.ATTENTION and "figée" in c["resume"]

    def test_le_seuil_se_regle(self, base, monkeypatch):
        monkeypatch.setenv("SUPERVISION_CREDIT_OPENROUTER_MIN", "10")
        _relever(FauxClient())
        assert _controle("credit_openrouter")["niveau"] == supervision.ATTENTION

    def test_la_cle_part_dans_l_en_tete_et_nulle_part_ailleurs(self, base):
        client = FauxClient()
        _relever(client)
        url, entetes, _ = client.requetes[0]
        assert entetes["Authorization"] == "Bearer cle-de-test" and "cle-de-test" not in url
        with db.get_conn() as conn:
            assert "cle-de-test" not in conn.execute(
                "SELECT valeur FROM supervision WHERE cle = 'credit_openrouter'").fetchone()[0]

    def test_sans_cle_rien_a_surveiller(self, base, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY")
        client = FauxClient()
        _relever(client)
        assert client.requetes == [] and _controle("credit_openrouter")["niveau"] == supervision.OK


class TestLesEchecsGitHub:
    def _runs(self, *noms_dates):
        return (200, {"workflow_runs": [{"name": n, "created_at": d, "html_url": f"u/{d}"}
                                        for n, d in noms_dates]})

    def test_une_ligne_par_workflow_pas_par_execution(self):
        client = FauxClient(github=self._runs(("Supervision de la production", "2026-10-24T05:00:00Z"),
                                              ("Supervision de la production", "2026-10-24T05:15:00Z"),
                                              ("CI", "2026-10-23T20:00:00Z")))
        r = asyncio.run(tournee.echecs_github(client, horloge.maintenant()))
        sonde = next(g for g in r["echecs"] if g["workflow"].startswith("Supervision"))
        assert len(r["echecs"]) == 2 and sonde["nombre"] == 2 and sonde["url"] == "u/2026-10-24T05:15:00Z"

    def test_seulement_main_et_seulement_les_echecs(self):
        client = FauxClient()
        asyncio.run(tournee.echecs_github(client, horloge.maintenant()))
        _, _, params = client.requetes[0]
        assert params["branch"] == "main" and params["status"] == "failure"
        assert params["created"].startswith(">=")

    @pytest.mark.parametrize("reponse", [(403, {}), ConnectionError()])
    def test_github_illisible_est_un_point_a_voir(self, reponse):
        r = asyncio.run(tournee.echecs_github(FauxClient(github=reponse), horloge.maintenant()))
        liste = tournee.points({"controles": []}, r)
        assert [p["titre"] for p in liste] == ["GitHub Actions"]
        assert liste[0]["niveau"] == supervision.ATTENTION


class TestLesPoints:
    def test_le_vert_n_est_pas_un_point_et_la_panne_vient_en_premier(self):
        etat = {"controles": [
            {"titre": "A", "niveau": "ok", "resume": "r"},
            {"titre": "B", "niveau": "attention", "resume": "r"},
            {"titre": "C", "niveau": "panne", "resume": "r"}]}
        assert [p["titre"] for p in tournee.points(etat, {"echecs": []})] == ["C", "B"]


class TestLaTournee:
    def _comptes(self):
        users.create_user("exploitant@test.local", "mot-de-passe-1", users.ROLE_SUPERADMIN)
        t = tenants.create_tenant("Chez Test", "+33199000007")
        users.create_user("resto@test.local", "mot-de-passe-2", users.ROLE_RESTAURATEUR, t.id)

    def test_un_point_a_voir_ecrit_aux_super_admins_seulement(self, base):
        self._comptes()
        envoyer = AsyncMock(return_value=True)
        with patch.object(tournee.notifications, "envoyer", envoyer):
            bilan = asyncio.run(tournee.faire(FauxClient(
                openrouter=(200, {"data": {"total_credits": 15, "total_usage": 14}}))))
        adresses, sujet, corps = envoyer.call_args.args
        assert "exploitant@test.local" in adresses and "resto@test.local" not in adresses
        assert "Crédit OpenRouter" in corps and "point(s) à voir" in sujet
        assert bilan["envoye"] and bilan["points"] >= 1

    def test_tout_vert_aucun_e_mail(self, base):
        self._comptes()
        envoyer = AsyncMock(return_value=True)
        vert = {"niveau": "ok", "controles": [{"titre": "x", "niveau": "ok", "resume": "r"}]}
        with patch.object(tournee.notifications, "envoyer", envoyer), \
             patch.object(tournee.supervision, "etat", return_value=vert):
            bilan = asyncio.run(tournee.faire(FauxClient()))
        envoyer.assert_not_called()
        assert bilan == {"jour": horloge.aujourd_hui().isoformat(), "points": 0, "envoye": False}

    def test_une_seule_tournee_par_jour(self, base):
        with patch.object(tournee.notifications, "envoyer", AsyncMock(return_value=True)):
            assert not tournee._faite_aujourd_hui()
            asyncio.run(tournee.faire(FauxClient()))
        assert tournee._faite_aujourd_hui()


class TestLHeure:
    @pytest.mark.parametrize("brut, attendu", [("08:00", (8, 0)), ("07:30", (7, 30)),
                                                ("non", None), ("n'importe quoi", (8, 0)),
                                                ("25:00", (8, 0))])
    def test_le_reglage(self, monkeypatch, brut, attendu):
        monkeypatch.setenv("SUPERVISION_TOURNEE_HEURE", brut)
        assert tournee.heure() == attendu

    def test_avant_huit_heures_c_est_ce_matin(self):
        sept = datetime(2026, 10, 24, 7, 0, tzinfo=horloge.FUSEAU)
        assert tournee.attente_secondes(sept, 8, 0) == 3600

    def test_le_changement_d_heure_ne_decale_pas_la_tournee(self):
        """Nuit du 24 au 25/10/2026 : l'heure d'hiver revient. De 9 h à 8 h le lendemain,
        il s'écoule 24 h, pas 23."""
        neuf = datetime(2026, 10, 24, 9, 0, tzinfo=horloge.FUSEAU)
        assert tournee.attente_secondes(neuf, 8, 0) == 24 * 3600


class _Fin(Exception):
    """Arrête la boucle au premier sommeil, pour lire ce qu'elle a décidé."""


class TestLaBoucle:
    def _tourner(self, monkeypatch, *, heure, faire, faite=False):
        monkeypatch.setenv("SUPERVISION_TOURNEE_HEURE", "08:00")
        pauses = []

        async def dormir(s):
            pauses.append(s)
            raise _Fin

        instant = datetime(2026, 10, 24, *heure, tzinfo=horloge.FUSEAU)
        with patch.object(tournee.horloge, "maintenant", return_value=instant), \
             patch.object(tournee, "faire", faire), \
             patch.object(tournee, "_faite_aujourd_hui", return_value=faite), \
             patch.object(tournee.asyncio, "sleep", dormir):
            with pytest.raises(_Fin):
                asyncio.run(tournee.boucle())
        return pauses[0]

    def test_redemarree_apres_huit_heures_elle_fait_la_tournee_du_jour(self, monkeypatch):
        faire = AsyncMock(return_value={"jour": "2026-10-24", "points": 0, "envoye": False})
        attente = self._tourner(monkeypatch, heure=(9, 0), faire=faire)
        faire.assert_awaited_once()
        assert attente == 23 * 3600 + 3600  # jusqu'à demain 8 h (changement d'heure compris)

    def test_jamais_deux_fois_le_meme_jour(self, monkeypatch):
        faire = AsyncMock()
        self._tourner(monkeypatch, heure=(9, 0), faire=faire, faite=True)
        faire.assert_not_awaited()

    def test_avant_huit_heures_elle_attend(self, monkeypatch):
        faire = AsyncMock()
        assert self._tourner(monkeypatch, heure=(7, 0), faire=faire) == 3600
        faire.assert_not_awaited()

    def test_un_e_mail_qui_n_est_pas_parti_se_retente_dans_l_heure(self, monkeypatch):
        faire = AsyncMock(return_value={"jour": None, "points": 2, "envoye": False})
        assert self._tourner(monkeypatch, heure=(9, 0), faire=faire) == 3600

    def test_une_tournee_qui_echoue_se_retente_dans_l_heure(self, monkeypatch):
        faire = AsyncMock(side_effect=RuntimeError("base verrouillée"))
        assert self._tourner(monkeypatch, heure=(9, 0), faire=faire) == 3600


class TestLEnvoiRate:
    def _faire(self, *, smtp_actif):
        with patch.object(tournee.notifications, "envoyer", AsyncMock(return_value=False)), \
             patch.object(tournee.notifications, "actif", return_value=smtp_actif):
            return asyncio.run(tournee.faire(FauxClient(
                openrouter=(200, {"data": {"total_credits": 15, "total_usage": 14}}))))

    def test_messagerie_en_panne_la_tournee_n_est_pas_faite(self, base):
        assert self._faire(smtp_actif=True)["jour"] is None
        assert not tournee._faite_aujourd_hui()

    def test_sans_messagerie_configuree_rien_ne_partira_elle_est_faite(self, base):
        self._faire(smtp_actif=False)
        assert tournee._faite_aujourd_hui()


class TestSansTournee:
    def test_un_credit_jamais_releve_sans_tournee_n_est_pas_vert(self, base):
        """conftest coupe la tournée : sans elle, aucune relève ne viendra."""
        assert _controle("credit_openrouter")["niveau"] == supervision.ATTENTION

    def test_une_page_pleine_d_echecs_dit_le_vrai_total(self):
        runs = [{"name": "Supervision de la production", "created_at": f"2026-10-24T{h:02d}:00:00Z",
                 "html_url": "u"} for h in range(10)]
        client = FauxClient(github=(200, {"total_count": 140, "workflow_runs": runs}))
        r = asyncio.run(tournee.echecs_github(client, horloge.maintenant()))
        point = tournee.points({"controles": []}, r)[0]
        assert point["resume"].startswith("140 ") and "plus récentes seulement" in point["detail"]
