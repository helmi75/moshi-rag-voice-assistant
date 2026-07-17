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


class TestBuildTTS:
    def test_default_is_pocket(self, monkeypatch):
        monkeypatch.delenv("TTS_PROVIDER", raising=False)
        tts = bot.build_tts()
        assert isinstance(tts, pocket_tts.PocketTTSService)

    def test_explicit_pocket(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "pocket")
        assert isinstance(bot.build_tts(), pocket_tts.PocketTTSService)

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
