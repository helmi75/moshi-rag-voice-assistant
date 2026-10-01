"""La vigie d'un appel (ASSISTANTE-118) : elle repère la panne pendant la conversation.

Ce qui compte : deux échecs de suite sur la voix, le modèle ou la transcription rendent
la ligne au restaurant ; un seul accroc, non ; et quand l'assistante passe la main, la
ligne n'est PAS raccrochée — sinon le client serait coupé au moment d'avoir quelqu'un."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from app import renvoi
from app.voice import bot
from app.voice.vigie import Vigie

def _faux(famille):
    """Un service sans sa construction : la vigie ne regarde que sa famille."""
    classe = type(f"Faux{famille.__name__}", (famille,), {})
    classe.__abstractmethods__ = frozenset()
    return object.__new__(classe)


VOIX, MODELE, OREILLE = (_faux(c) for c in (TTSService, LLMService, STTService))
AUTRE = object()


def _erreur(processeur, fatal=False) -> ErrorFrame:
    frame = ErrorFrame(error="panne", fatal=fatal)
    frame.processor = processeur
    return frame


def _jouer(*evenements):
    """Fait défiler des (frame, source) devant une vigie ; rend (vigie, alertes)."""
    alertes = []

    async def alerter(maillon):
        alertes.append(maillon)

    vigie = Vigie(alerter)

    async def defiler():
        for frame, source in evenements:
            await vigie.on_push_frame(SimpleNamespace(frame=frame, source=source))

    asyncio.run(defiler())
    return vigie, alertes


class TestDeuxEchecsDeSuite:
    @pytest.mark.parametrize("service,maillon", [(VOIX, renvoi.VOIX), (MODELE, renvoi.MODELE),
                                                (OREILLE, renvoi.TRANSCRIPTION)])
    def test_deux_de_suite_et_l_assistante_passe_la_main(self, service, maillon):
        _, alertes = _jouer((_erreur(service), service), (_erreur(service), service))
        assert alertes == [maillon]

    def test_un_seul_accroc_ne_suffit_pas(self):
        assert _jouer((_erreur(VOIX), VOIX))[1] == []

    def test_une_phrase_reussie_remet_le_compte_a_zero(self):
        _, alertes = _jouer((_erreur(VOIX), VOIX), (BotStartedSpeakingFrame(), AUTRE),
                            (_erreur(VOIX), VOIX))
        assert alertes == []

    def test_chaque_maillon_a_son_compte(self):
        """Une erreur de voix puis une de modèle : deux accrocs, pas une panne."""
        _, alertes = _jouer((_erreur(VOIX), VOIX), (_erreur(MODELE), MODELE))
        assert alertes == []

    def test_le_modele_qui_ecrit_et_la_transcription_qui_rend_des_mots(self):
        _, alertes = _jouer(
            (_erreur(MODELE), MODELE), (LLMTextFrame("Bonjour"), MODELE), (_erreur(MODELE), MODELE),
            (_erreur(OREILLE), OREILLE), (TranscriptionFrame("oui", "client", "t"), OREILLE),
            (_erreur(OREILLE), OREILLE))
        assert alertes == []

    def test_une_erreur_qui_remonte_n_est_comptee_qu_une_fois(self):
        """La même erreur passe de maillon en maillon en remontant le pipeline."""
        erreur = _erreur(VOIX)
        assert _jouer((erreur, VOIX), (erreur, AUTRE), (erreur, AUTRE))[1] == []

    def test_une_erreur_ailleurs_n_est_pas_une_panne_de_conversation(self):
        assert _jouer((_erreur(AUTRE), AUTRE), (_erreur(AUTRE), AUTRE))[1] == []

    def test_une_seule_alerte_par_appel(self):
        vigie, alertes = _jouer(*[(_erreur(VOIX), VOIX) for _ in range(5)])
        assert alertes == [renvoi.VOIX] and vigie.alertee == renvoi.VOIX

    def test_sans_processeur_note_la_source_fait_foi(self):
        """Une ErrorFrame rendue par `run_tts` n'a pas toujours son processeur."""
        a, b = ErrorFrame(error="x"), ErrorFrame(error="y")
        assert _jouer((a, VOIX), (b, VOIX))[1] == [renvoi.VOIX]


class TestUneErreurFatale:
    def test_elle_rend_la_ligne_tout_de_suite(self):
        """Pipecat va arrêter le pipeline : il faut passer avant son raccrochage."""
        assert _jouer((_erreur(OREILLE, fatal=True), OREILLE))[1] == [renvoi.TRANSCRIPTION]

    def test_d_ou_qu_elle_vienne(self):
        assert _jouer((_erreur(AUTRE, fatal=True), AUTRE))[1] == [renvoi.PIPELINE]


class TestLaLigneNEstPasRaccrochee:
    @pytest.fixture()
    def serialiseur(self, monkeypatch):
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "jeton")
        s = bot.serialiseur_twilio("MZ1", "CA1")
        s._hang_up_call = AsyncMock()
        return s

    @pytest.mark.parametrize("fin", [EndFrame, CancelFrame])
    def test_une_fin_normale_raccroche(self, serialiseur, fin):
        asyncio.run(serialiseur.serialize(fin()))
        serialiseur._hang_up_call.assert_awaited_once()

    @pytest.mark.parametrize("fin", [EndFrame, CancelFrame])
    def test_un_renvoi_garde_la_ligne(self, serialiseur, fin):
        serialiseur.garder_la_ligne = True
        assert asyncio.run(serialiseur.serialize(fin())) is None
        serialiseur._hang_up_call.assert_not_awaited()

    def test_passer_la_main(self):
        """Dans l'ordre : garder la ligne, demander le renvoi, prévenir les appels
        suivants, arrêter le pipeline."""
        serialiseur = SimpleNamespace(garder_la_ligne=False)
        task = SimpleNamespace(cancel=AsyncMock())

        async def scenario():
            bot.passer_la_main(renvoi.VOIX, call_sid="CAmain", tenant_id=3,
                               serializer=serialiseur, task=task)
            assert serialiseur.garder_la_ligne is True   # avant toute attente
            await asyncio.sleep(0.01)

        asyncio.run(scenario())
        task.cancel.assert_awaited_once()
        assert renvoi.motif_a_la_fin_du_flux("CAmain") == renvoi.VOIX
        assert renvoi.panne_recente(99) == renvoi.VOIX


class TestAvecLeVraiPipeline:
    """Les tests ci-dessus font défiler des trames à la main. Celui-ci fait tourner un
    vrai pipeline Pipecat avec la vraie voix Mistral, dont le réseau est en panne : c'est
    la preuve que l'erreur arrive bien jusqu'à la vigie, sous la forme attendue, et que
    rendre la ligne depuis l'observateur ne bloque pas le pipeline."""

    def test_mistral_en_panne_l_appel_est_rendu_sans_raccrocher(self, monkeypatch):
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineParams, PipelineTask

        from app.voice import voxtral_tts

        async def reseau_en_panne(client, texte, voix_id):
            raise voxtral_tts.VoxtralErreur("HTTP 503 : service indisponible")
            yield  # pragma: no cover — fait de cette fonction un générateur

        monkeypatch.setattr(voxtral_tts, "flux_pcm", reseau_en_panne)
        monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
        voxtral_tts.oublier_echecs()
        serialiseur = SimpleNamespace(garder_la_ligne=False)
        vues = []

        async def scenario():
            voix = voxtral_tts.VoxtralTTSService("fr_marie_neutral")
            contexte = {}

            async def alerter(maillon):
                vues.append(maillon)
                bot.passer_la_main(maillon, call_sid="CApipeline", tenant_id=1,
                                   serializer=serialiseur, task=contexte["task"])

            contexte["task"] = PipelineTask(
                Pipeline([voix]), observers=[Vigie(alerter)],
                params=PipelineParams(audio_out_sample_rate=8000))
            await contexte["task"].queue_frames([
                TTSSpeakFrame("Bonjour, je regarde nos disponibilités."),
                TTSSpeakFrame("Pour combien de personnes ?"),
                TTSSpeakFrame("Vous êtes toujours là ?"),
            ])
            # Rien ne termine cet appel, sinon la vigie : si elle ne voit rien, le
            # délai échoue le test au lieu de le laisser tourner.
            await asyncio.wait_for(PipelineRunner(handle_sigint=False).run(contexte["task"]), 20)

        asyncio.run(scenario())
        assert vues == [renvoi.VOIX]                       # à la deuxième phrase perdue
        assert serialiseur.garder_la_ligne is True
        assert renvoi.motif_a_la_fin_du_flux("CApipeline") == renvoi.VOIX
        assert renvoi.panne_recente(42) == renvoi.VOIX
        # Chaque phrase a été essayée deux fois avant d'être abandonnée.
        assert len(voxtral_tts.echecs_depuis(60)) in (4, 6)
        voxtral_tts.oublier_echecs()
