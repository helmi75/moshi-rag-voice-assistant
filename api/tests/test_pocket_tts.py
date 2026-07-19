"""Tests du TTS Kyutai Pocket TTS et du sélecteur de fournisseur TTS.

Le modèle Pocket TTS est entièrement mocké : aucun téléchargement, aucune
inférence réelle. Exécuter depuis api/ avec : pytest tests/ -v
"""
import asyncio

import numpy as np
import pytest

from app.voice import bot, pocket_tts


class _FakeTensor:
    """Imite le minimum d'un tenseur torch utilisé par run_tts."""

    def __init__(self, array):
        self._array = array

    def clamp(self, lo, hi):
        return _FakeTensor(np.clip(self._array, lo, hi))

    def to(self, _device):
        return self

    def numpy(self):
        return self._array


def _fake_model(chunks):
    class _Model:
        sample_rate = 24000

        def generate_audio_stream(self, state, text, copy_state=True):
            for c in chunks:
                yield _FakeTensor(c)

    return _Model()


async def _collect(gen):
    return [frame async for frame in gen]


class TestVoiceResolution:
    def test_preset_name_passed_through_as_catalog_name(self):
        # Un nom du catalogue doit rester tel quel : le modèle charge alors la
        # voix depuis son .safetensors préréglé (aucun clonage).
        assert pocket_tts._resolve_voice("estelle") == "estelle"

    def test_custom_ref_passed_through_for_cloning(self):
        for value in ("hf://kyutai/x.wav", "/data/ma_voix.wav", "https://x/a.mp3"):
            assert pocket_tts._resolve_voice(value) == value


class TestDeviceSelection:
    def test_auto_falls_back_to_cpu_without_gpu(self, monkeypatch):
        # Aucun GPU dans l'environnement de test : "auto" doit choisir le CPU.
        monkeypatch.setenv("POCKET_TTS_DEVICE", "auto")
        assert pocket_tts._select_device() == "cpu"

    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("POCKET_TTS_DEVICE", raising=False)
        # Défaut = auto -> cpu ici (pas de GPU).
        assert pocket_tts._select_device() == "cpu"

    def test_explicit_cpu(self, monkeypatch):
        monkeypatch.setenv("POCKET_TTS_DEVICE", "cpu")
        assert pocket_tts._select_device() == "cpu"

    def test_explicit_cuda_is_honored(self, monkeypatch):
        # "cuda" forcé est retourné tel quel (l'utilisateur assume la présence du GPU).
        monkeypatch.setenv("POCKET_TTS_DEVICE", "cuda")
        assert pocket_tts._select_device() == "cuda"


class TestPocketRunTTS:
    def test_yields_audio_frames_at_service_rate(self, monkeypatch):
        chunks = [np.array([0.0, 0.5, -0.5], dtype=np.float32),
                  np.array([0.25, -0.25], dtype=np.float32)]
        monkeypatch.setattr(
            pocket_tts, "_load_model_and_state",
            lambda: (_fake_model(chunks), {"state": 1}, 24000),
        )
        # resampler identité (async, comme le vrai) pour un contrôle simple des octets
        svc = pocket_tts.PocketTTSService()

        async def _identity(audio, in_rate, out_rate):
            return audio

        monkeypatch.setattr(svc._resampler, "resample", _identity)
        from pipecat.frames.frames import TTSAudioRawFrame

        frames = asyncio.run(_collect(svc.run_tts("Bonjour", "ctx1")))
        audio_frames = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert len(audio_frames) == 2
        assert all(f.context_id == "ctx1" for f in audio_frames)
        assert all(f.sample_rate == svc.sample_rate for f in audio_frames)
        # premier chunk : 3 échantillons int16 = 6 octets
        assert len(audio_frames[0].audio) == 6

    def test_model_error_yields_error_frame(self, monkeypatch):
        def _boom():
            raise RuntimeError("modèle indisponible")

        monkeypatch.setattr(pocket_tts, "_load_model_and_state", _boom)
        from pipecat.frames.frames import ErrorFrame

        svc = pocket_tts.PocketTTSService()
        frames = asyncio.run(_collect(svc.run_tts("Bonjour", "ctx1")))
        assert any(isinstance(f, ErrorFrame) for f in frames)

    def test_model_and_voice_loaded_once(self, monkeypatch):
        """Le singleton _load_model_and_state ne charge le modèle qu'une fois."""
        calls = {"load_model": 0, "get_state": 0}
        model = _fake_model([np.array([0.1], dtype=np.float32)])
        model.get_state_for_audio_prompt = lambda ref: calls.__setitem__(
            "get_state", calls["get_state"] + 1
        ) or {"state": 1}

        class _FakeTTSModel:
            @staticmethod
            def load_model(language=None):
                calls["load_model"] += 1
                return model

        import sys
        import types

        fake_pkg = types.ModuleType("pocket_tts")
        fake_pkg.TTSModel = _FakeTTSModel
        monkeypatch.setitem(sys.modules, "pocket_tts", fake_pkg)
        monkeypatch.setattr(pocket_tts, "_resolve_voice", lambda voice: "hf://ref")
        monkeypatch.setattr(pocket_tts, "_MODEL_CACHE", None)

        first = pocket_tts._load_model_and_state()
        second = pocket_tts._load_model_and_state()
        assert first is second
        assert calls["load_model"] == 1
        assert calls["get_state"] == 1


class _FakeCpuTensor:
    """Imite le retour de mimi.decode(...) : .cpu().numpy() -> ndarray."""

    def __init__(self, arr):
        self._arr = arr

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class _FakeMimi:
    sample_rate = 24000

    def decode(self, _frame_slice):
        # PCM factice de forme (batch=1, canal=1, échantillons=3).
        return _FakeCpuTensor(np.array([[[0.0, 0.5, -0.5]]], dtype=np.float32))


class _FakeKyutaiModel:
    """tts_model minimal : prepare_script + generate(on_frame=...) comme moshi 0.2.13."""

    mimi = _FakeMimi()

    def prepare_script(self, script, padding_between=0):
        return ["entry"]

    def generate(self, all_entries, attributes, on_frame=None, **kwargs):
        # Deux frames « prêtes » (aucune valeur == -1) -> deux morceaux audio.
        frame = np.zeros((1, 33, 1), dtype=np.int64)
        on_frame(frame)
        on_frame(frame)


class TestKyutaiRunTTS:
    def test_generate_path_yields_audio_frames(self, monkeypatch):
        # Verrouille l'API moshi 0.2.13 : run_tts doit passer par
        # tts_model.generate(..., on_frame=...) et produire des TTSAudioRawFrame.
        from app.voice import kyutai_tts

        monkeypatch.setattr(
            kyutai_tts, "_load_model_and_voice",
            lambda: (_FakeKyutaiModel(), object(), 24000),
        )
        svc = kyutai_tts.KyutaiTTSService()

        async def _identity(audio, in_rate, out_rate):
            return audio

        monkeypatch.setattr(svc._resampler, "resample", _identity)
        from pipecat.frames.frames import TTSAudioRawFrame

        frames = asyncio.run(_collect(svc.run_tts("Bonjour", "ctx-k")))
        audio_frames = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert len(audio_frames) == 2
        assert all(f.context_id == "ctx-k" for f in audio_frames)
        # 3 échantillons int16 = 6 octets
        assert len(audio_frames[0].audio) == 6

    def test_generate_error_yields_error_frame(self, monkeypatch):
        from app.voice import kyutai_tts

        class _BoomModel(_FakeKyutaiModel):
            def generate(self, *a, **k):
                raise RuntimeError("boom moshi")

        monkeypatch.setattr(
            kyutai_tts, "_load_model_and_voice",
            lambda: (_BoomModel(), object(), 24000),
        )
        from pipecat.frames.frames import ErrorFrame

        svc = kyutai_tts.KyutaiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("Bonjour", "ctx-k")))
        assert any(isinstance(f, ErrorFrame) for f in frames)


class _FakeWS:
    """Websocket factice : enregistre les envois, itère des messages en réception.

    `delay` simule l'attente avant le 1er message (cold start) : utilisé pour tester
    le déclenchement de la musique d'attente."""

    def __init__(self, messages, delay=0.0):
        self._messages = messages
        self._delay = delay
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        async def _gen():
            # Cède la main à l'event loop pour laisser la tâche d'envoi s'exécuter
            # (en réel, l'attente réseau joue ce rôle).
            await asyncio.sleep(self._delay or 0)
            for m in self._messages:
                yield m

        return _gen()


class _FakeConnect:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def _install_fake_ws(monkeypatch, messages, delay=0.0):
    """Injecte de faux modules `websockets` et `msgpack` pour run_tts (imports paresseux).
    msgpack.unpackb renvoie l'objet tel quel : on fait circuler des dicts directement.
    `delay` diffère le 1er message (simule un cold start) pour tester l'attente."""
    import sys
    import types

    ws = _FakeWS(messages, delay=delay)
    fake_websockets = types.ModuleType("websockets")
    # **kwargs : accepte open_timeout (cold start) et tout futur paramètre de connexion.
    fake_websockets.connect = lambda uri, additional_headers=None, max_size=None, **kwargs: _FakeConnect(ws)

    fake_msgpack = types.ModuleType("msgpack")
    fake_msgpack.packb = lambda obj: obj
    fake_msgpack.unpackb = lambda b: b

    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    monkeypatch.setitem(sys.modules, "msgpack", fake_msgpack)
    return ws


class TestMoshiServerRunTTS:
    def test_streams_audio_frames_from_websocket(self, monkeypatch):
        from app.voice.moshi_server_tts import MoshiServerTTSService

        messages = [
            {"type": "Audio", "pcm": [0.0, 0.5, -0.5]},
            {"type": "Ready"},  # message non-audio -> ignoré
            {"type": "Audio", "pcm": [0.25, -0.25]},
        ]
        ws = _install_fake_ws(monkeypatch, messages)

        svc = MoshiServerTTSService()

        async def _identity(audio, in_rate, out_rate):
            return audio

        monkeypatch.setattr(svc._resampler, "resample", _identity)
        from pipecat.frames.frames import TTSAudioRawFrame

        frames = asyncio.run(_collect(svc.run_tts("Bonjour tout le monde", "ctx-m")))
        audio_frames = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert len(audio_frames) == 2
        assert all(f.context_id == "ctx-m" for f in audio_frames)
        assert len(audio_frames[0].audio) == 6  # 3 échantillons int16
        assert len(ws.sent) == 5  # 4 mots (Text) + 1 Eos envoyés
        assert ws.sent[-1] == {"type": "Eos"}

    _HOLD = b"\x11\x22\x33\x44"  # frame de musique d'attente reconnaissable

    def _svc_with_hold(self, monkeypatch):
        from app.voice.moshi_server_tts import MoshiServerTTSService

        svc = MoshiServerTTSService()

        async def _identity(audio, in_rate, out_rate):
            return audio

        monkeypatch.setattr(svc._resampler, "resample", _identity)

        async def _hold():
            return [self._HOLD]

        monkeypatch.setattr(svc, "_hold_music_frames", _hold)
        return svc

    def test_hold_music_fills_while_waiting(self, monkeypatch):
        """1er audio réel retardé (cold start) -> la musique d'attente meuble le blanc,
        puis s'arrête dès l'arrivée du vrai audio."""
        from pipecat.frames.frames import TTSAudioRawFrame

        monkeypatch.setenv("MOSHI_HOLD_AFTER_SECONDS", "0")
        _install_fake_ws(monkeypatch, [{"type": "Audio", "pcm": [0.5]}], delay=0.25)
        svc = self._svc_with_hold(monkeypatch)

        frames = asyncio.run(_collect(svc.run_tts("bonjour", "ctx")))
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        hold = [f for f in audio if f.audio == self._HOLD]
        real = [f for f in audio if f.audio != self._HOLD]

        assert hold, "la musique d'attente aurait dû meubler le délai"
        assert len(real) == 1, "le vrai audio doit finir par être joué"
        # La musique s'arrête au 1er audio réel : aucune frame d'attente après lui.
        assert audio.index(real[0]) == len(hold), "l'attente doit précéder le vrai audio"

    def test_preconnect_is_used_by_run_tts(self, monkeypatch):
        """Une pré-connexion (LLMFullResponseStartFrame) est consommée par run_tts
        et une suivante est planifiée pour la phrase d'après."""
        from pipecat.frames.frames import TTSAudioRawFrame

        _install_fake_ws(monkeypatch, [{"type": "Audio", "pcm": [0.5]}])
        svc = self._svc_with_hold(monkeypatch)

        async def _run():
            svc._ensure_preconnect()          # simule le départ du LLM
            await asyncio.sleep(0)            # laisse la connexion s'ouvrir
            assert svc._next_ws is not None
            frames = [f async for f in svc.run_tts("bonjour", "ctx")]
            # La pré-connexion a été consommée puis re-planifiée (chaînage).
            assert svc._next_ws is not None
            svc._discard_preconnect()
            await asyncio.sleep(0)
            return frames

        frames = asyncio.run(_run())
        assert any(isinstance(f, TTSAudioRawFrame) for f in frames)

    def test_stale_preconnect_is_replaced(self, monkeypatch):
        _install_fake_ws(monkeypatch, [])
        svc = self._svc_with_hold(monkeypatch)

        async def _run():
            svc._ensure_preconnect()
            first = svc._next_ws
            await asyncio.sleep(0)
            svc._next_ws_time -= svc._PRECONNECT_TTL + 1  # vieillit artificiellement
            svc._ensure_preconnect()
            assert svc._next_ws is not first, "une pré-connexion périmée doit être remplacée"
            svc._discard_preconnect()
            await asyncio.sleep(0)

        asyncio.run(_run())

    def test_no_hold_music_when_response_is_fast(self, monkeypatch):
        """Réponse immédiate (GPU chaud) -> jamais de musique d'attente."""
        from pipecat.frames.frames import TTSAudioRawFrame

        monkeypatch.setenv("MOSHI_HOLD_AFTER_SECONDS", "3")
        _install_fake_ws(monkeypatch, [{"type": "Audio", "pcm": [0.5, -0.5]}])
        svc = self._svc_with_hold(monkeypatch)

        frames = asyncio.run(_collect(svc.run_tts("bonjour", "ctx")))
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert all(f.audio != self._HOLD for f in audio), "pas d'attente si la réponse est rapide"
        assert len(audio) == 1

    def test_websocket_error_yields_error_frame(self, monkeypatch):
        import sys
        import types

        from app.voice.moshi_server_tts import MoshiServerTTSService

        def _boom(*a, **k):
            raise RuntimeError("connexion refusée")

        fake_websockets = types.ModuleType("websockets")
        fake_websockets.connect = _boom
        fake_msgpack = types.ModuleType("msgpack")
        fake_msgpack.packb = lambda obj: obj
        fake_msgpack.unpackb = lambda b: b
        monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
        monkeypatch.setitem(sys.modules, "msgpack", fake_msgpack)

        from pipecat.frames.frames import ErrorFrame

        svc = MoshiServerTTSService()
        frames = asyncio.run(_collect(svc.run_tts("Bonjour", "ctx-m")))
        assert any(isinstance(f, ErrorFrame) for f in frames)


class TestBuildTTS:
    def test_default_is_pocket(self, monkeypatch):
        monkeypatch.delenv("TTS_PROVIDER", raising=False)
        tts = bot.build_tts()
        assert isinstance(tts, pocket_tts.PocketTTSService)

    def test_explicit_pocket(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "pocket")
        assert isinstance(bot.build_tts(), pocket_tts.PocketTTSService)

    def test_kyutai_selected(self, monkeypatch):
        # Le provider kyutai s'instancie sans charger `moshi` (imports paresseux).
        monkeypatch.setenv("TTS_PROVIDER", "kyutai")
        from app.voice.kyutai_tts import KyutaiTTSService

        assert isinstance(bot.build_tts(), KyutaiTTSService)

    def test_moshi_server_selected(self, monkeypatch):
        # Le provider moshi_server s'instancie sans websockets/msgpack (imports paresseux).
        monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
        from app.voice.moshi_server_tts import MoshiServerTTSService

        assert isinstance(bot.build_tts(), MoshiServerTTSService)

    def test_cartesia_selected(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "cartesia")
        monkeypatch.setenv("CARTESIA_API_KEY", "fake")
        monkeypatch.setenv("CARTESIA_VOICE_ID", "fake-voice")
        from pipecat.services.cartesia.tts import CartesiaTTSService

        assert isinstance(bot.build_tts(), CartesiaTTSService)

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "espeak")
        with pytest.raises(ValueError, match="TTS_PROVIDER inconnu"):
            bot.build_tts()
