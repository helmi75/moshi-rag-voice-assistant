"""Les métriques Prometheus (ASSISTANTE-156) : ce que Grafana relève chaque minute.

Ce qu'on exige : la route ne s'ouvre qu'avec son jeton, chaque appel clos est rangé dans
la bonne issue (dont « muet », la panne du 30/07/2026), rien de personnel ne sort, et
une mesure ne fait jamais échouer un appel.

Les compteurs vivent dans le processus et s'accumulent d'un test à l'autre : on mesure
donc toujours un ÉCART, avant et après.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import calls, db, metriques, supervision, tenants
from app.main import app

JETON = "jeton-de-metriques"
ENTETE = {"Authorization": f"Bearer {JETON}"}
APPELANT = "+33612345678"
PROPOS = "Je m'appelle Jean Dupont, une table pour quatre"


@pytest.fixture(autouse=True)
def cache_vide():
    supervision.vider_cache()
    yield
    supervision.vider_cache()


@pytest.fixture()
def resto():
    return tenants.create_tenant("Chez Mesure", f"+3361{uuid.uuid4().int % 10**7:07d}")


def _valeur(nom: str, **etiquettes) -> float:
    return metriques.REGISTRE.get_sample_value(nom, etiquettes) or 0.0


def _vieillir(sid: str, secondes: int) -> None:
    """`finish_call` mesure la durée depuis `started_at` : on recule le début."""
    debut = datetime.now(timezone.utc) - timedelta(seconds=secondes)
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET started_at = ? WHERE call_sid = ?",
                     (debut.strftime("%Y-%m-%dT%H:%M:%SZ"), sid))


def _appel_clos(tenant_id: int, sid: str, *, secondes: int = 60, status="completed",
                transcript=None, journal=None, latences=None) -> None:
    calls.start_call(sid, tenant_id, APPELANT)
    _vieillir(sid, secondes)
    calls.finish_call(sid, status, transcript, None, latences, journal)


REPONDU = [{"role": "assistant", "content": "Bonjour, Chez Mesure j'écoute."},
           {"role": "user", "content": PROPOS},
           {"role": "assistant", "content": "Pour quel soir souhaitez-vous venir ?"}]
ACCUEIL_SEUL = [{"role": "assistant", "content": "Bonjour, Chez Mesure j'écoute."},
                {"role": "user", "content": "allô ? allô ?"}]


class TestRoute:
    """C'est Grafana Cloud qui vient chercher les mesures : la route est joignable de
    l'extérieur, donc fermée à qui n'a pas le jeton."""

    def test_sans_jeton_configure_la_route_n_existe_pas(self, monkeypatch):
        monkeypatch.delenv("METRIQUES_TOKEN", raising=False)
        with TestClient(app) as client:
            assert client.get("/metrics", headers=ENTETE).status_code == 404

    def test_un_jeton_absent_ou_faux_est_refuse(self, monkeypatch):
        monkeypatch.setenv("METRIQUES_TOKEN", JETON)
        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 401
            assert client.get("/metrics", headers={"Authorization": "Bearer faux"}).status_code == 401
            # Le jeton nu, sans « Bearer », ni dans l'URL : il finirait dans les journaux.
            assert client.get("/metrics", headers={"Authorization": JETON}).status_code == 401
            assert client.get(f"/metrics?token={JETON}").status_code == 401

    def test_une_session_d_admin_ne_suffit_pas(self, monkeypatch):
        """Les séries décrivent le trafic de TOUS les établissements : être connecté à
        l'admin, même en super-admin, n'en donne pas l'accès."""
        monkeypatch.setenv("METRIQUES_TOKEN", JETON)
        with TestClient(app) as client:
            assert client.post("/admin/login", data={"email": "admin@test.local",
                                                     "password": "test-admin-pass"},
                               follow_redirects=False).status_code == 303
            assert client.get("/metrics").status_code == 401

    def test_avec_le_jeton_les_mesures_sortent_au_format_prometheus(self, monkeypatch, resto):
        monkeypatch.setenv("METRIQUES_TOKEN", JETON)
        with TestClient(app) as client:
            reponse = client.get("/metrics", headers=ENTETE)
        assert reponse.status_code == 200
        assert reponse.headers["content-type"].startswith("text/plain; version=0.0.4")
        texte = reponse.text
        assert 'helmane_supervision_niveau{controle="global"}' in texte
        assert 'helmane_supervision_niveau{controle="base"}' in texte
        assert f'helmane_etablissement_info{{etablissement="{resto.id}",nom="Chez Mesure"}} 1.0' in texte
        assert "process_resident_memory_bytes" in texte


class TestIssues:
    def test_un_appel_ou_le_modele_a_repondu_est_servi(self, resto):
        avant = _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                        moteur="pipecat", issue="servi")
        _appel_clos(resto.id, "CAservi", transcript=REPONDU)
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="pipecat", issue="servi") == avant + 1

    def test_un_appel_ou_seul_l_accueil_a_parle_est_muet(self, resto):
        """La panne du 30/07/2026 : l'appel finit « normalement », l'appelant n'a eu que
        l'accueil pré-rendu. C'est la série sur laquelle poser l'alerte la plus urgente."""
        avant = _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                        moteur="pipecat", issue="muet")
        _appel_clos(resto.id, "CAmuet", transcript=ACCUEIL_SEUL)
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="pipecat", issue="muet") == avant + 1

    def test_un_appel_trop_court_pour_juger_n_est_pas_muet(self, resto):
        """Même seuil que le contrôle de supervision : un client qui raccroche au bout
        de trois secondes n'est pas une panne."""
        avant = _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                        moteur="pipecat", issue="muet")
        _appel_clos(resto.id, "CAbref", secondes=3, transcript=ACCUEIL_SEUL)
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="pipecat", issue="muet") == avant

    def test_un_appel_en_erreur_est_compte_en_echec(self, resto):
        avant = _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                        moteur="pipecat", issue="echec")
        _appel_clos(resto.id, "CAechec", status="failed", transcript=REPONDU)
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="pipecat", issue="echec") == avant + 1

    def test_le_moteur_gpt_live_est_distingue(self, resto):
        avant = _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                        moteur="gpt-live", issue="servi")
        _appel_clos(resto.id, "CAlive", transcript=REPONDU,
                    journal={"voix": {"fournisseur": "gpt-live"}})
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="gpt-live", issue="servi") == avant + 1

    def test_un_appel_passe_en_secours_est_compte_une_fois(self, resto):
        """La vigie et la fin du flux peuvent ouvrir le secours toutes deux : le premier
        motif est gardé en base, le compteur ne double pas."""
        cle = str(resto.id)
        secours = _valeur("helmane_secours_total", etablissement=cle, motif="voix")
        clos = _valeur("helmane_appels_termines_total", etablissement=cle,
                       moteur="pipecat", issue="secours")
        calls.start_call("CAsecours", resto.id, APPELANT)
        calls.ouvrir_secours("CAsecours", resto.id, APPELANT, "voix")
        calls.ouvrir_secours("CAsecours", resto.id, APPELANT, "pipeline")
        calls.finish_call("CAsecours", "completed", REPONDU)
        assert _valeur("helmane_secours_total", etablissement=cle, motif="voix") == secours + 1
        assert _valeur("helmane_secours_total", etablissement=cle, motif="pipeline") == 0
        assert _valeur("helmane_appels_termines_total", etablissement=cle,
                       moteur="pipecat", issue="secours") == clos + 1


class TestBlancs:
    def test_chaque_blanc_tombe_dans_son_seau(self, resto):
        """Les seuils de la supervision (2,5 s, 4 s) sont des bornes de seau : une alerte
        compte exactement les blancs qui les dépassent."""
        cle = str(resto.id)
        total = _valeur("helmane_blanc_secondes_count", etablissement=cle)
        rapides = _valeur("helmane_blanc_secondes_bucket", etablissement=cle, le="2.5")
        _appel_clos(resto.id, "CAblancs", transcript=REPONDU, latences=[800, 1200, 3000])
        assert _valeur("helmane_blanc_secondes_count", etablissement=cle) == total + 3
        assert _valeur("helmane_blanc_secondes_bucket", etablissement=cle, le="2.5") == rapides + 2


class TestDonneesPersonnelles:
    def test_ni_numero_ni_propos_ni_identifiant_d_appel(self, monkeypatch, resto):
        _appel_clos(resto.id, "CAconfidentiel", transcript=REPONDU)
        calls.ouvrir_secours("CAconfidentiel", resto.id, APPELANT, "voix")
        texte = metriques.exposition().decode()
        for secret in (APPELANT, "612345678", "Jean Dupont", "CAconfidentiel"):
            assert secret not in texte


class TestEtablissementDEssai:
    def test_ses_appels_portent_l_etiquette_essai(self, monkeypatch, resto):
        """Les appels automatiques de la recette ne doivent pas passer pour un client."""
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(resto.id))
        avant = _valeur("helmane_appels_termines_total", etablissement="essai",
                        moteur="pipecat", issue="servi")
        _appel_clos(resto.id, "CAessai", transcript=REPONDU)
        assert _valeur("helmane_appels_termines_total", etablissement="essai",
                       moteur="pipecat", issue="servi") == avant + 1
        assert _valeur("helmane_appels_termines_total", etablissement=str(resto.id),
                       moteur="pipecat", issue="servi") == 0


class TestJamaisAuxDepensDeLAppel:
    def test_une_mesure_qui_casse_ne_fait_pas_echouer_la_cloture(self, resto):
        calls.start_call("CAcasse", resto.id, APPELANT)
        with patch.object(metriques.APPELS_TERMINES, "labels", side_effect=RuntimeError("boum")):
            identifiant = calls.finish_call("CAcasse", "completed", REPONDU)
        assert identifiant is not None
        with db.get_conn() as conn:
            fin = conn.execute("SELECT ended_at FROM calls WHERE call_sid = 'CAcasse'").fetchone()
        assert fin["ended_at"] is not None

    def test_une_supervision_illisible_n_empeche_pas_la_releve(self, monkeypatch):
        """Pas de mesure n'est pas un feu vert : la série « global » disparaît plutôt que
        d'afficher 0, et les compteurs sortent quand même."""
        monkeypatch.setattr(supervision, "etat", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        texte = metriques.exposition().decode()
        assert 'controle="global"' not in texte
        assert "helmane_appels_en_cours" in texte


class TestAppelsEnCours:
    """La jauge suit le flux audio, pour les deux moteurs, et redescend quelle que soit
    la façon dont l'appel se termine — sinon elle afficherait des appels fantômes."""

    def _jouer(self, run_bot):
        from tests.test_voice_stream import _attendre_le_bot, _twilio_start_message

        with patch("app.main._get_bot_runner", return_value=run_bot):
            with TestClient(app).websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps(_twilio_start_message(call_sid="CAjauge")))
                _attendre_le_bot(run_bot)

    def test_la_jauge_monte_pendant_l_appel_et_redescend_apres(self):
        tenant = tenants.get_by_phone("+33100000000")
        cle = str(tenant.id)
        avant = _valeur("helmane_appels_en_cours", etablissement=cle)
        pendant = []

        async def bot(*_args, **_kwargs):
            pendant.append(_valeur("helmane_appels_en_cours", etablissement=cle))

        self._jouer(AsyncMock(side_effect=bot))
        assert pendant == [avant + 1]
        assert _valeur("helmane_appels_en_cours", etablissement=cle) == avant

    def test_elle_redescend_aussi_quand_le_pipeline_tombe(self):
        tenant = tenants.get_by_phone("+33100000000")
        cle = str(tenant.id)
        avant = _valeur("helmane_appels_en_cours", etablissement=cle)
        self._jouer(AsyncMock(side_effect=RuntimeError("pipeline en panne")))
        assert _valeur("helmane_appels_en_cours", etablissement=cle) == avant


class TestErreursAvecLeVraiPipeline:
    """Une voix Mistral en panne dans un vrai pipeline Pipecat : chaque phrase perdue est
    comptée sur le maillon « voix », vue par la vigie, sans rien sur le chemin de l'audio."""

    def test_chaque_phrase_perdue_est_comptee_sur_la_voix(self, monkeypatch):
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineParams, PipelineTask

        from app import renvoi
        from app.voice import bot, voxtral_tts
        from app.voice.vigie import Vigie

        async def reseau_en_panne(client, texte, voix_id):
            raise voxtral_tts.VoxtralErreur("HTTP 503 : service indisponible")
            yield  # pragma: no cover — fait de cette fonction un générateur

        monkeypatch.setattr(voxtral_tts, "flux_pcm", reseau_en_panne)
        monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
        voxtral_tts.oublier_echecs()
        avant = _valeur("helmane_erreurs_maillon_total", etablissement="77", maillon="voix")
        serialiseur = SimpleNamespace(garder_la_ligne=False)

        async def scenario():
            voix = voxtral_tts.VoxtralTTSService("fr_marie_neutral")
            contexte = {}

            async def alerter(maillon):
                bot.passer_la_main(maillon, call_sid="CAmesure", tenant_id=77,
                                   serializer=serialiseur, task=contexte["task"])

            contexte["task"] = PipelineTask(
                Pipeline([voix]), observers=[Vigie(alerter, 77)],
                params=PipelineParams(audio_out_sample_rate=8000))
            await contexte["task"].queue_frames([
                TTSSpeakFrame("Bonjour, je regarde nos disponibilités."),
                TTSSpeakFrame("Pour combien de personnes ?"),
            ])
            await asyncio.wait_for(PipelineRunner(handle_sigint=False).run(contexte["task"]), 20)

        asyncio.run(scenario())
        assert _valeur("helmane_erreurs_maillon_total", etablissement="77",
                       maillon="voix") >= avant + 2
        assert renvoi.motif_a_la_fin_du_flux("CAmesure") == renvoi.VOIX
        voxtral_tts.oublier_echecs()
