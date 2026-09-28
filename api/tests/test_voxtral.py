"""La voix Marie de Voxtral (Mistral), à côté des voix Moshi (SCRUM-94 à 97).

Aucun appel réseau : l'API de Mistral est jouée par un transport httpx qui rend des
Server-Sent Events à la forme exacte de ceux relevés le 28/09/2026
(`event: speech.audio.delta` / `data: {"type": "speech.audio.delta", "audio_data": …}`,
PCM float32 24 kHz en base64).
"""
import asyncio
import base64
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

from app import supervision, tenants
from app.tenants import Tenant
from app.voice import greeting as g
from app.voice import voices, voxtral_tts

MARIE = "voxtral/marie-joyeuse"


@pytest.fixture(autouse=True, scope="module")
def _base():
    """Importer l'application initialise la base : sans lui, ce fichier lancé seul
    trouverait une base sans tables (mêmes précautions que test_carnet_resos.py)."""
    from app.main import app  # noqa: F401


@pytest.fixture(autouse=True)
def _propre(monkeypatch, tmp_path):
    monkeypatch.setenv("GREETING_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    monkeypatch.delenv("MOSHI_TTS_VOICE", raising=False)
    voxtral_tts.oublier_echecs()
    yield
    voxtral_tts.oublier_echecs()


def _tenant(voice=MARIE, greeting="Bonjour, restaurant, un instant s'il vous plaît."):
    return Tenant(id=1, name="Resto", business_type="restaurant", phone_number="+33100000000",
                  language="fr-FR", greeting=greeting, knowledge_base="", voice=voice)


def _sse(*morceaux: np.ndarray) -> bytes:
    lignes = []
    for m in morceaux:
        donnees = {"type": "speech.audio.delta",
                   "audio_data": base64.b64encode(m.astype("<f4").tobytes()).decode()}
        lignes += ["event: speech.audio.delta", f"data: {json.dumps(donnees)}", ""]
    lignes += ["event: speech.audio.done", 'data: {"type": "speech.audio.done"}', ""]
    return ("\n".join(lignes) + "\n").encode()


def _mistral(*reponses):
    """Un faux Mistral qui rend les réponses dans l'ordre ; garde les requêtes vues."""
    vues = []
    file = list(reponses)

    def repondre(requete):
        vues.append(json.loads(requete.content))
        statut, corps = file.pop(0)
        return httpx.Response(statut, content=corps,
                              headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(repondre), vues


def _ton(secondes=0.1):
    return (np.sin(np.linspace(0, 200, int(24000 * secondes))) * 0.5).astype(np.float32)


class TestCatalogue:
    def test_marie_en_quatre_tons_sans_triste_ni_colere(self):
        labels = [v.label for v in voices.voix_voxtral()]
        assert labels == ["Marie · neutre", "Marie · joyeuse", "Marie · curieuse",
                          "Marie · enthousiaste"]
        assert all(v.fournisseur == voices.VOXTRAL and len(v.voxtral_id) == 36
                   for v in voices.voix_voxtral())

    def test_marie_se_resout_avec_la_cle(self):
        assert voices.resolve(_tenant()) == MARIE
        assert voices.label_for(_tenant()) == "Marie · joyeuse"

    def test_sans_cle_on_retombe_sur_la_voix_moshi_par_defaut(self, monkeypatch):
        """Jamais le silence : un appel ne doit pas dépendre d'une clé absente."""
        monkeypatch.delenv("MISTRAL_API_KEY")
        assert voices.resolve(_tenant()) == voices.DEFAULT_VOICE

    def test_le_defaut_du_parc_reste_une_voix_moshi(self, monkeypatch):
        monkeypatch.setenv("MOSHI_TTS_VOICE", MARIE)
        assert voices.default_id() == voices.DEFAULT_VOICE

    def test_build_tts_choisit_voxtral_meme_sans_serveur_moshi(self, monkeypatch):
        from app.voice import bot

        monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
        tts = bot.build_tts(_tenant())
        assert isinstance(tts, voxtral_tts.VoxtralTTSService)
        assert tts._voix_id == voices.get(MARIE).voxtral_id


def _service(transport):
    service = voxtral_tts.VoxtralTTSService(voix_id="id-marie")
    service._client = httpx.AsyncClient(transport=transport)
    service._sample_rate = 8000
    return service


def _frames(service, texte="Je vous écoute."):
    async def tout():
        return [f async for f in service.run_tts(texte, "ctx")]
    return asyncio.run(tout())


class TestServiceDeVoix:
    def test_le_flux_devient_de_l_audio_8_khz_pour_twilio(self):
        transport, vues = _mistral((200, _sse(_ton(0.1), _ton(0.1))))
        frames = _frames(_service(transport))
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert audio and all(f.sample_rate == 8000 for f in audio)
        # 0,2 s de voix rendue à 24 kHz → ~0,2 s à 8 kHz, en int16.
        assert abs(sum(len(f.audio) for f in audio) / 2 / 8000 - 0.2) < 0.05
        assert vues[0]["voice_id"] == "id-marie" and vues[0]["stream"] is True
        assert vues[0]["response_format"] == "pcm"

    def test_une_panne_passagere_est_reessayee_une_fois(self):
        transport, vues = _mistral((503, b"surcharge"), (200, _sse(_ton())))
        frames = _frames(_service(transport))
        assert any(isinstance(f, TTSAudioRawFrame) for f in frames)
        assert not any(isinstance(f, ErrorFrame) for f in frames)
        assert len(vues) == 2 and len(voxtral_tts.echecs_depuis(60)) == 1

    def test_deux_echecs_donnent_une_erreur_et_sont_comptes(self):
        transport, _ = _mistral((401, b"cle refusee"), (401, b"cle refusee"))
        frames = _frames(_service(transport))
        assert isinstance(frames[-1], ErrorFrame) and "401" in frames[-1].error
        assert len(voxtral_tts.echecs_depuis(60)) == 2


class TestAccueilEtDecroche:
    def test_l_accueil_est_rendu_par_voxtral(self, monkeypatch):
        """Pas de websocket vers le GPU : l'accueil de Marie vient de Mistral."""
        monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
        demandes = []

        async def faux_rendu(texte, voix_id):
            demandes.append(voix_id)
            return _ton(0.3)

        monkeypatch.setattr(voxtral_tts, "rendre_pcm", faux_rendu)
        chemin = asyncio.run(g.ensure_greeting_wav(_tenant()))
        assert chemin is not None and chemin.exists()
        assert demandes[0] == voices.get(MARIE).voxtral_id
        with wave.open(str(chemin)) as w:
            assert w.getframerate() == 8000

    def test_ni_reveil_ni_musique_meme_annonce_froid(self, monkeypatch):
        """Même si l'appelant de l'intro croit le GPU froid, Marie n'a rien à réveiller :
        « Je vous écoute. » suit directement l'accueil."""
        reveils = []

        async def faux_reveil():
            reveils.append(1)
            return True

        async def sans_attente(secondes):
            return None

        monkeypatch.setattr(g, "warmup_moshi_server", faux_reveil)
        monkeypatch.setattr(g.asyncio, "sleep", sans_attente)
        chemin = g._cache_path(_tenant())
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(chemin), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 4000)
        task = SimpleNamespace(queue_frames=AsyncMock())
        sortie = SimpleNamespace(send_audio=AsyncMock())
        asyncio.run(g.run_switchboard_intro(task, sortie, _tenant(), None, chaud=False))
        textes = [f.text for appel in task.queue_frames.await_args_list for f in appel.args[0]
                  if type(f).__name__ == "TTSSpeakFrame"]
        assert reveils == [] and textes == ["Je vous écoute."]
        assert sortie.send_audio.await_count == 25  # l'accueil seul, aucune musique

    def test_une_voix_moshi_garde_son_reveil(self, monkeypatch):
        assert g.voix_sans_gpu(_tenant(voice=voices.DEFAULT_VOICE)) is False
        assert g.voix_sans_gpu(_tenant()) is True


class TestSupervision:
    @pytest.fixture()
    def marie(self):
        t = tenants.create_tenant("Chez Marie", f"+3361{id(object()) % 10_000_000:07d}")
        tenants.update_tenant(t.id, voice=MARIE)
        yield t
        tenants.delete_tenant(t.id)

    def test_sans_objet_si_personne_n_a_choisi_marie(self):
        assert supervision._controle_voxtral().niveau == supervision.OK

    def test_cle_retiree_apres_le_choix_est_une_panne(self, marie, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        controle = supervision._controle_voxtral()
        assert controle.niveau == supervision.PANNE and "Chez Marie" in controle.detail

    def test_des_echecs_dans_l_heure_se_voient(self, marie):
        voxtral_tts.noter_echec("HTTP 503 : surcharge")
        controle = supervision._controle_voxtral()
        assert controle.niveau == supervision.ATTENTION and "503" in controle.detail

    def test_tout_va_bien(self, marie):
        assert supervision._controle_voxtral().niveau == supervision.OK


class TestAdmin:
    @pytest.fixture()
    def resto(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOLD_MUSIC_DIR", str(tmp_path / "hold"))
        tenant = tenants.create_tenant("Voix Marie", f"+3363{id(object()) % 10_000_000:07d}")
        yield tenant
        tenants.delete_tenant(tenant.id)

    def _client(self):
        import base64

        from app.main import app

        client = TestClient(app)
        assert client.post("/admin/login", data={"email": "admin@test.local",
                                                 "password": "test-admin-pass"},
                           follow_redirects=False).status_code == 303
        client.get("/admin/")
        brut = client.cookies.get("session").split(".")[0]
        brut += "=" * (-len(brut) % 4)
        client.headers["X-CSRF-Token"] = json.loads(base64.b64decode(brut))["csrf"]
        return client

    def test_marie_et_ses_tons_s_ecoutent_sur_la_page(self, resto):
        page = self._client().get(f"/admin/tenants/{resto.id}/voice").text
        for v in voices.voix_voxtral():
            assert f'value="{v.id}"' in page
        assert "/admin/static/voix/marie-joyeuse.wav?v=" in page

    def test_choisir_marie(self, resto, monkeypatch):
        monkeypatch.setattr(g, "ensure_greeting_wav", AsyncMock())
        reponse = self._client().post(f"/admin/tenants/{resto.id}/voice",
                                      data={"voice": MARIE}, follow_redirects=False)
        assert reponse.status_code == 303
        assert tenants.get_by_id(resto.id).voice == MARIE

    def test_sans_cle_marie_est_grisee_et_refusee(self, resto, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        client = self._client()
        page = client.get(f"/admin/tenants/{resto.id}/voice").text
        assert "clé Mistral absente" in page and "disabled" in page
        reponse = client.post(f"/admin/tenants/{resto.id}/voice", data={"voice": MARIE})
        assert reponse.status_code == 422 and "clé Mistral" in reponse.text
        assert tenants.get_by_id(resto.id).voice != MARIE

    def test_les_extraits_existent(self):
        import pathlib

        dossier = pathlib.Path(__file__).resolve().parents[1] / "app" / "admin" / "static" / "voix"
        for v in voices.voix_voxtral():
            with wave.open(str(dossier / f"{v.id.split('/')[1]}.wav")) as w:
                assert w.getframerate() == 8000 and w.getnframes() > 8000


class TestDansLePipeline:
    def test_une_phrase_traverse_le_vrai_pipeline_pipecat(self):
        """Le service tourne dans une vraie tâche Pipecat : la cadence vient du
        StartFrame (8 kHz, celle de Twilio), et la phrase est encadrée par le début et la
        fin de parole du TTS — ce que lisent l'agrégateur et le journal de bord."""
        from pipecat.frames.frames import TTSSpeakFrame, TTSStartedFrame, TTSStoppedFrame
        from pipecat.pipeline.task import PipelineParams
        from pipecat.tests.utils import run_test

        transport, vues = _mistral((200, _sse(_ton(0.2), _ton(0.2))))
        service = voxtral_tts.VoxtralTTSService(voix_id="id-marie")
        service._client = httpx.AsyncClient(transport=transport)
        descendues, _ = asyncio.run(run_test(
            service, frames_to_send=[TTSSpeakFrame("Je vous écoute.")],
            pipeline_params=PipelineParams(audio_out_sample_rate=8000)))
        types = [type(f) for f in descendues]
        assert TTSStartedFrame in types and TTSStoppedFrame in types
        audio = [f for f in descendues if isinstance(f, TTSAudioRawFrame)]
        assert audio and {f.sample_rate for f in audio} == {8000}
        # Vidé en fin de phrase : toute la voix rendue arrive, rien n'est gardé en réserve.
        assert abs(sum(len(f.audio) for f in audio) / 2 / 8000 - 0.4) < 0.01
        assert vues[0]["input"] == "Je vous écoute."
