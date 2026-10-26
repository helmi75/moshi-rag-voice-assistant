"""scripts/depot_github.py : la CI lue en entier, la fusion refusée tant qu'elle n'est pas
verte sur le commit testé, et le jeton jamais affiché. Sans réseau : `ouvrir` est remplacé."""
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

_CHEMIN = Path(__file__).resolve().parents[2] / "scripts" / "depot_github.py"
_spec = importlib.util.spec_from_file_location("depot_github", _CHEMIN)
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)

JETON = "ghp_jeton_de_test_secret_0123456789"
SHA = "a" * 40
PREFIXE = f"{dg.API}/repos/{dg.DEPOT}"


class _Reponse(io.BytesIO):
    def __init__(self, statut, corps):
        super().__init__(json.dumps(corps).encode())
        self.status = statut


class FauxGitHub:
    """Répond selon (méthode, chemin) ; garde chaque requête pour qu'un test la relise."""

    def __init__(self, routes):
        self.routes = routes
        self.requetes = []

    def __call__(self, requete, timeout=None):
        chemin = requete.full_url.removeprefix(PREFIXE)
        self.requetes.append((requete.get_method(), chemin, requete))
        statut, corps = self.routes[(requete.get_method(), chemin)]
        if statut >= 400:
            raise urllib.error.HTTPError(requete.full_url, statut, "refus", {},
                                         io.BytesIO(json.dumps(corps).encode()))
        return _Reponse(statut, corps)

    def envoyes(self, methode):
        return [(c, json.loads(r.data)) for m, c, r in self.requetes if m == methode]


def _job(nom, statut="completed", conclusion="success", slug="github-actions", suite=1):
    return {"name": nom, "status": statut, "conclusion": conclusion, "app": {"slug": slug},
            "check_suite": {"id": suite}}


def _flux(nom="CI", statut="completed", conclusion="success", evenement="push", suite=1):
    return {"name": nom, "status": statut, "conclusion": conclusion, "event": evenement,
            "check_suite_id": suite}


def _routes_ci(sha=SHA, flux=None, jobs=None):
    """GitHub filtre les exécutions par événement : on range chacune sous le sien."""
    flux = [_flux()] if flux is None else flux
    routes = {("GET", f"/actions/runs?head_sha={sha}&event={ev}&per_page=100"):
                  (200, {"workflow_runs": [f for f in flux if f["event"] == ev]})
              for ev in dg.EVENEMENTS_CI}
    routes[("GET", f"/commits/{sha}/check-runs?per_page=100&page=1")] = (
        200, {"check_runs": [_job("tests"), _job("image")] if jobs is None else jobs})
    return routes


def _depot(routes):
    faux = FauxGitHub(routes)
    return dg.Depot(JETON, ouvrir=faux), faux


class TestLeVerdictDeLaCi:
    def test_tout_termine_sans_echec_est_vert(self):
        depot, _ = _depot(_routes_ci())
        assert depot.etat_ci(SHA)["verdict"] == dg.VERT

    def test_un_job_saute_ne_rend_pas_la_ci_rouge(self):
        depot, _ = _depot(_routes_ci(jobs=[_job("tests"), _job("image", conclusion="skipped")]))
        assert depot.etat_ci(SHA)["verdict"] == dg.VERT

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
    def test_un_job_qui_n_a_pas_reussi_rend_la_ci_rouge(self, conclusion):
        depot, _ = _depot(_routes_ci(jobs=[_job("tests"), _job("mutation", conclusion=conclusion)]))
        assert depot.etat_ci(SHA)["verdict"] == dg.ROUGE

    def test_un_workflow_en_cours_sans_job_visible_n_est_pas_vert(self):
        """La PR 150 : les jobs visibles étaient verts, un workflow tournait encore."""
        depot, _ = _depot(_routes_ci(flux=[_flux(), _flux(statut="in_progress", conclusion=None,
                                                          evenement="pull_request")]))
        assert depot.etat_ci(SHA)["verdict"] == dg.EN_COURS

    def test_rien_encore_cree_n_est_pas_un_feu_vert(self):
        depot, _ = _depot(_routes_ci(flux=[], jobs=[]))
        assert depot.etat_ci(SHA)["verdict"] == dg.AUCUNE

    def test_la_marque_d_une_autre_application_ne_compte_pas(self):
        jobs = [_job("tests"), _job("supervision", conclusion="failure", slug="autre-appli")]
        depot, _ = _depot(_routes_ci(jobs=jobs))
        assert depot.etat_ci(SHA)["verdict"] == dg.VERT

    @pytest.mark.parametrize("statut, conclusion", [("completed", "failure"),
                                                    ("in_progress", None)])
    def test_la_sonde_planifiee_sur_la_tete_de_main_ne_compte_pas(self, statut, conclusion):
        """La sonde est un workflow GitHub Actions planifié, rattaché au dernier commit de
        `main` : production en panne, ou sonde en cours, ce n'est pas la CI du code."""
        jobs = [_job("tests"), _job("image"),
                _job("sonde", statut=statut, conclusion=conclusion, suite=2)]
        depot, faux = _depot(_routes_ci(jobs=jobs))
        assert depot.etat_ci(SHA)["verdict"] == dg.VERT
        assert not any("event=schedule" in c for _, c, _ in faux.requetes)

    def test_toutes_les_pages_de_jobs_sont_lues(self):
        routes = _routes_ci(jobs=[_job(f"j{i}") for i in range(100)])
        routes[("GET", f"/commits/{SHA}/check-runs?per_page=100&page=2")] = (
            200, {"check_runs": [_job("dernier", conclusion="failure")]})
        depot, _ = _depot(routes)
        assert depot.etat_ci(SHA)["verdict"] == dg.ROUGE


class TestLAttente:
    def test_attend_que_tout_soit_termine(self):
        routes = _routes_ci(jobs=[_job("tests", statut="in_progress", conclusion=None)])
        depot, faux = _depot(routes)
        pauses = []

        def dormir(s):
            pauses.append(s)
            routes[("GET", f"/commits/{SHA}/check-runs?per_page=100&page=1")] = (
                200, {"check_runs": [_job("tests")]})

        etat = depot.attendre_ci(SHA, pause=20, dormir=dormir, horloge=lambda: 0)
        assert etat["verdict"] == dg.VERT and pauses == [20]

    def test_au_bout_du_delai_rend_en_cours_et_sort_en_2(self, capsys):
        routes = _routes_ci(jobs=[_job("tests", statut="queued", conclusion=None)])
        depot, _ = _depot(routes)
        temps = iter([0, 5, 2000])
        etat = depot.attendre_ci(SHA, delai=10, dormir=lambda s: None, horloge=lambda: next(temps))
        assert etat["verdict"] == dg.EN_COURS
        assert dg.main(["ci", SHA], depot=depot) == 2


class TestLaFusion:
    def _routes_pr(self, ci=None, etat="open"):
        routes = ci or _routes_ci(flux=[_flux(), _flux(evenement="pull_request", suite=3)],
                                  jobs=[_job("tests"), _job("image"), _job("tests", suite=3)])
        routes[("GET", "/pulls/7")] = (200, {"state": etat, "merged": False,
                                             "head": {"sha": SHA}})
        routes[("PUT", "/pulls/7/merge")] = (200, {"merged": True, "sha": "b" * 40})
        return routes

    def test_fusionne_par_commit_de_fusion_sur_le_commit_teste(self):
        depot, faux = _depot(self._routes_pr())
        assert dg.main(["fusionner", "7"], depot=depot) == 0
        assert faux.envoyes("PUT") == [("/pulls/7/merge", {"merge_method": "merge", "sha": SHA})]

    @pytest.mark.parametrize("jobs", [
        [_job("tests"), _job("image", statut="in_progress", conclusion=None)],
        [_job("tests", conclusion="failure")],
        [],
    ])
    def test_refuse_tant_que_la_ci_n_est_pas_verte(self, jobs, capsys):
        flux = [] if not jobs else None
        depot, faux = _depot(self._routes_pr(ci=_routes_ci(flux=flux, jobs=jobs)))
        assert dg.main(["fusionner", "7"], depot=depot) == 1
        assert faux.envoyes("PUT") == []
        assert "pas de fusion" in capsys.readouterr().err

    def test_attend_l_execution_qui_teste_la_fusion(self, capsys):
        """PR ouverte juste après la CI du push : l'exécution `pull_request` n'existe pas
        encore, et le vert du push ne teste pas le résultat de la fusion."""
        depot, faux = _depot(self._routes_pr(ci=_routes_ci()))
        assert dg.main(["fusionner", "7"], depot=depot) == 1
        assert faux.envoyes("PUT") == []
        assert "en cours" in capsys.readouterr().err

    def test_refuse_une_pr_fermee(self):
        depot, faux = _depot(self._routes_pr(etat="closed"))
        assert dg.main(["fusionner", "7"], depot=depot) == 1
        assert faux.envoyes("PUT") == []

    def test_une_branche_qui_a_bouge_entre_temps_est_un_refus(self, capsys):
        routes = self._routes_pr()
        routes[("PUT", "/pulls/7/merge")] = (409, {"message": "Head branch was modified"})
        depot, _ = _depot(routes)
        assert dg.main(["fusionner", "7"], depot=depot) == 1
        assert "Head branch was modified" in capsys.readouterr().err


class TestLaPr:
    def test_ouvre_vers_main(self, tmp_path, capsys):
        corps = tmp_path / "corps.md"
        corps.write_text("## Pourquoi\n")
        depot, faux = _depot({("POST", "/pulls"): (201, {"number": 9, "html_url": "u"})})
        assert dg.main(["pr", "--titre", "T", "--corps-fichier", str(corps),
                        "--branche", "claude/x"], depot=depot) == 0
        assert faux.envoyes("POST") == [("/pulls", {"title": "T", "head": "claude/x",
                                                    "base": "main", "body": "## Pourquoi\n"})]

    def test_rend_la_pr_deja_ouverte(self, tmp_path, capsys):
        corps = tmp_path / "corps.md"
        corps.write_text("x")
        depot, _ = _depot({("POST", "/pulls"): (422, {"message": "A pull request already exists"}),
                           ("GET", "/pulls?state=open&head=helmi75:claude/x"):
                               (200, [{"number": 9, "html_url": "u"}])})
        assert dg.main(["pr", "--titre", "T", "--corps-fichier", str(corps),
                        "--branche", "claude/x"], depot=depot) == 0
        assert "PR 9 déjà ouverte" in capsys.readouterr().out

    def test_refuse_depuis_main(self, tmp_path):
        corps = tmp_path / "corps.md"
        corps.write_text("x")
        depot, faux = _depot({})
        assert dg.main(["pr", "--titre", "T", "--corps-fichier", str(corps),
                        "--branche", "main"], depot=depot) == 1
        assert faux.requetes == []


class TestLeCommit:
    def test_un_nom_qui_ressemble_a_une_option_n_en_est_pas_une(self, capsys):
        depot, faux = _depot({("GET", "/commits/--show-toplevel"): (404, {"message": "Not Found"})})
        assert dg.main(["ci", "--", "--show-toplevel"], depot=depot) == 1
        assert "Not Found" in capsys.readouterr().err

    def test_une_plage_n_est_pas_un_commit(self, capsys):
        depot, _ = _depot({("GET", "/commits/HEAD~1..HEAD"): (422, {"message": "No commit"})})
        assert dg.main(["ci", "HEAD~1..HEAD"], depot=depot) == 1


class TestLeJeton:
    @pytest.fixture(autouse=True)
    def sans_gh_token(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)

    def test_gh_token_d_abord_pour_github_actions(self, tmp_path, monkeypatch):
        # ASSISTANTE-176 : un déploiement lancé depuis GitHub n'a pas de ~/.git-credentials.
        monkeypatch.setenv("GH_TOKEN", JETON)
        assert dg.lire_jeton(tmp_path / "absent") == JETON

    def test_un_gh_token_vide_ne_compte_pas(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "  ")
        with pytest.raises(dg.RefusGitHub, match="aucun jeton"):
            dg.lire_jeton(tmp_path / "absent")

    def test_lu_dans_git_credentials(self, tmp_path):
        fichier = tmp_path / "cred"
        fichier.write_text(f"https://autre:x@gitlab.com\nhttps://helmi75:{JETON}@github.com\n")
        assert dg.lire_jeton(fichier) == JETON

    def test_absent_est_un_refus_clair(self, tmp_path):
        with pytest.raises(dg.RefusGitHub, match="aucun jeton"):
            dg.lire_jeton(tmp_path / "absent")

    def test_jamais_affiche_meme_quand_github_refuse(self, capsys):
        routes = {("GET", "/pulls/7"): (401, {"message": "Bad credentials"})}
        depot, faux = _depot(routes)
        assert dg.main(["fusionner", "7"], depot=depot) == 1
        sortie = capsys.readouterr()
        assert JETON not in sortie.out + sortie.err
        assert "Bad credentials" in sortie.err
        # …et c'est bien lui qui est envoyé, dans l'en-tête seulement.
        _, chemin, requete = faux.requetes[0]
        assert requete.get_header("Authorization") == f"Bearer {JETON}" and JETON not in chemin

    def test_jamais_affiche_quand_github_est_injoignable(self, capsys):
        def injoignable(requete, timeout=None):
            raise urllib.error.URLError(f"connexion refusée {requete.get_header('Authorization')}")

        depot = dg.Depot(JETON, ouvrir=injoignable)
        assert dg.main(["ci", SHA], depot=depot) == 1
        sortie = capsys.readouterr()
        assert JETON not in sortie.out + sortie.err and "injoignable" in sortie.err
