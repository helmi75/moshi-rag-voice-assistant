"""La voix Marie de Voxtral (Mistral) (SCRUM-94 à 97).

Aucun appel réseau : l'API de Mistral est jouée par un transport httpx qui rend des
Server-Sent Events à la forme exacte de ceux relevés le 28/09/2026
(`event: speech.audio.delta` / `data: {"type": "speech.audio.delta", "audio_data": …}`,
PCM float32 24 kHz en base64).
"""
import asyncio
import base64
import json
import wave
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

MARIE = "voxtral/fr_marie_happy"


@pytest.fixture(autouse=True, scope="module")
def _base():
    """Importer l'application initialise la base : sans lui, ce fichier lancé seul
    trouverait une base sans tables (mêmes précautions que test_carnet_resos.py)."""
    from app.main import app  # noqa: F401


@pytest.fixture(autouse=True)
def _propre(monkeypatch, tmp_path):
    monkeypatch.setenv("GREETING_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
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
    def test_marie_se_resout_avec_la_cle(self):
        assert voices.resolve(_tenant()) == MARIE
        assert voices.label_for(_tenant()) == "Marie · joyeuse"

    def test_une_ancienne_voix_moshi_enregistree_est_servie_par_marie(self):
        """Retrait de Moshi (ASSISTANTE-103) : la base garde l'ancien identifiant, la
        lecture le résout vers la voix par défaut — pas de réécriture, pas de silence."""
        ancienne = "unmute-prod-website/developpeuse-3.wav"
        assert voices.resolve(_tenant(voice=ancienne)) == voices.default_id()
        assert voices.resolve(_tenant(voice="dossier-bidon/inconnue.wav")) == voices.default_id()
        assert voices.resolve(_tenant(voice=None)) == voices.default_id()

    def test_build_tts_d_une_ancienne_voix_moshi_rend_marie(self):
        from app.voice import bot

        tts = bot.build_tts(_tenant(voice="cml-tts/fr/4937_3731_000004-0001_enhanced.wav"))
        assert isinstance(tts, voxtral_tts.VoxtralTTSService)
        assert tts._voix_id == voices.get(voices.default_id()).voxtral_id

    def test_une_url_moshi_posee_n_a_aucun_effet(self, monkeypatch):
        """Aucun appel ne tente de joindre moshi-server : la variable est ignorée, et
        sans clé Mistral le pipeline refuse franchement au lieu de chercher un GPU."""
        from app.voice import bot

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        assert isinstance(bot.build_tts(_tenant()), voxtral_tts.VoxtralTTSService)
        monkeypatch.delenv("MISTRAL_API_KEY")
        with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
            bot.build_tts(_tenant())
        import importlib.util

        assert importlib.util.find_spec("app.voice.moshi_server_tts") is None


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
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
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


class TestSupervision:
    @pytest.fixture()
    def marie(self):
        t = tenants.create_tenant("Chez Marie", f"+3361{id(object()) % 10_000_000:07d}")
        tenants.update_tenant(t.id, voice=MARIE)
        yield t
        tenants.delete_tenant(t.id)

    def test_cle_retiree_est_une_panne(self, marie, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        controle = supervision._controle_voxtral()
        assert controle.niveau == supervision.PANNE and "MISTRAL_API_KEY" in controle.detail

    def test_des_echecs_dans_l_heure_se_voient(self, marie):
        voxtral_tts.noter_echec("HTTP 503 : surcharge")
        controle = supervision._controle_voxtral()
        assert controle.niveau == supervision.ATTENTION and "503" in controle.detail

    def test_tout_va_bien(self, marie):
        assert supervision._controle_voxtral().niveau == supervision.OK


class TestAdmin:
    @pytest.fixture()
    def resto(self, tmp_path, monkeypatch):
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

    def test_toutes_les_voix_mistral_se_choisissent_et_s_ecoutent(self, resto):
        page = self._client().get(f"/admin/tenants/{resto.id}/voice").text
        for v in voices.catalogue():
            assert f'value="{v.id}"' in page
        assert "/admin/voix/fr_marie_happy/extrait.wav" in page
        assert "Marie — Français" in page and "Paul — Anglais (États-Unis)" in page

    def test_choisir_une_voix(self, resto, monkeypatch):
        monkeypatch.setattr(g, "ensure_greeting_wav", AsyncMock())
        reponse = self._client().post(f"/admin/tenants/{resto.id}/voice",
                                      data={"voice": MARIE}, follow_redirects=False)
        assert reponse.status_code == 303
        assert tenants.get_by_id(resto.id).voice == MARIE

    def test_sans_cle_les_voix_sont_grisees_et_refusees(self, resto, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        client = self._client()
        page = client.get(f"/admin/tenants/{resto.id}/voice").text
        assert "Clé Mistral absente" in page and "disabled" in page
        reponse = client.post(f"/admin/tenants/{resto.id}/voice", data={"voice": MARIE})
        assert reponse.status_code == 422 and "clé Mistral" in reponse.text
        assert tenants.get_by_id(resto.id).voice != MARIE


class TestExtraitDEcoute:
    """Un extrait par voix, rendu à la première écoute puis gardé : ~0,15 c par voix."""

    def _client(self):
        return TestAdmin._client(self)

    def test_rendu_une_fois_puis_garde(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VOIX_EXTRAITS_DIR", str(tmp_path / "extraits"))
        rendus = []

        async def faux_rendu(texte, voix_id):
            rendus.append((texte, voix_id))
            return _ton(0.5)

        monkeypatch.setattr(voxtral_tts, "rendre_pcm", faux_rendu)
        client = self._client()
        for _ in range(2):
            reponse = client.get("/admin/voix/en_paul_happy/extrait.wav")
            assert reponse.status_code == 200 and reponse.content[:4] == b"RIFF"
        assert len(rendus) == 1 and rendus[0][0].startswith("Hello")
        assert rendus[0][1] == voices.get("voxtral/en_paul_happy").voxtral_id

    def test_une_voix_inconnue_n_est_jamais_rendue(self, monkeypatch):
        monkeypatch.setattr(voxtral_tts, "rendre_pcm", AsyncMock(side_effect=AssertionError))
        assert self._client().get("/admin/voix/inventee/extrait.wav").status_code == 404


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


class TestStatutDeLAccueil:
    def test_on_n_annonce_pas_de_gpu_pour_marie(self):
        from app.admin import deps

        gabarit = deps.templates.env.get_template("voice/_greeting_status.html")
        marie = gabarit.render(tenant=_tenant(), greeting_ready=False)
        assert "quelques secondes" in marie and "GPU" not in marie
