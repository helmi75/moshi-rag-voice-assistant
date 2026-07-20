"""Tests du service STT Kyutai (module ASR de moshi-server) — aucun réseau, tout mocké.

Même esprit que test_pocket_tts.py : on injecte un faux `msgpack` (packb identité, accepte
use_single_float/use_bin_type) et un faux websocket ; on capture les frames poussées en
reproduisant le marquage `finalized` de STTService.push_frame. On pilote les méthodes du
service directement (pas de pipeline complet, donc pas de task manager requis)."""
import sys
import types

import numpy as np
import pytest


async def _collect(agen):
    return [f async for f in agen]


def _install_fake_msgpack(monkeypatch):
    """packb identité qui tolère les kwargs (use_single_float, use_bin_type) -> on assert
    des dicts bruts ; unpackb identité pour faire circuler des dicts directement."""
    fake = types.ModuleType("msgpack")
    fake.packb = lambda obj, **kw: obj
    fake.unpackb = lambda b: b
    monkeypatch.setitem(sys.modules, "msgpack", fake)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


def _make_service(monkeypatch, sample_rate=24000):
    """Service prêt à tester : sample_rate posé (normalement par start()), resampler
    identité, push_frame capturé (en conservant le marquage finalized de la base)."""
    from pipecat.frames.frames import TranscriptionFrame

    from app.voice.kyutai_stt import KyutaiSTTService

    svc = KyutaiSTTService()
    svc._sample_rate = sample_rate

    async def _identity(audio, in_rate, out_rate):
        return audio

    monkeypatch.setattr(svc._resampler, "resample", _identity)

    captured: list = []

    async def _capture(frame, direction=None):
        # Reproduit STTService.push_frame : marque le TranscriptionFrame finalisé si une
        # finalisation est en attente (request_finalize -> confirm_finalize).
        if isinstance(frame, TranscriptionFrame) and svc._finalize_pending:
            frame.finalized = True
            svc._finalize_pending = False
        captured.append(frame)

    monkeypatch.setattr(svc, "push_frame", _capture)
    return svc, captured


def _tenant(name="Chez Test"):
    return types.SimpleNamespace(name=name, language="fr-FR")


class TestWsBase:
    def test_prefers_stt_url_and_normalizes(self, monkeypatch):
        from app.voice.kyutai_stt import _stt_ws_base

        monkeypatch.delenv("MOSHI_STT_URL", raising=False)
        monkeypatch.setenv("MOSHI_TTS_URL", "https://tts.example")
        assert _stt_ws_base() == "wss://tts.example"  # repli sur l'URL TTS (même serveur)

        monkeypatch.setenv("MOSHI_STT_URL", "http://stt.local:8080")
        assert _stt_ws_base() == "ws://stt.local:8080"  # priorité STT + http->ws


class TestRunSTT:
    def test_chunks_and_sends_1920_frames(self, monkeypatch):
        _install_fake_msgpack(monkeypatch)
        svc, _ = _make_service(monkeypatch, sample_rate=24000)
        svc._ws = _FakeWS()
        # 3840 échantillons int16 @ 24 kHz -> 2 trames de 1920, aucun reste.
        audio = np.zeros(3840, dtype=np.int16).tobytes()
        assert asyncio_run(_collect(svc.run_stt(audio))) == [None]
        audio_msgs = [m for m in svc._ws.sent if m.get("type") == "Audio"]
        assert len(audio_msgs) == 2
        assert all(len(m["pcm"]) == 1920 for m in audio_msgs)
        assert svc._resample_buf.size == 0

    def test_keeps_remainder_between_calls(self, monkeypatch):
        _install_fake_msgpack(monkeypatch)
        svc, _ = _make_service(monkeypatch, sample_rate=24000)
        svc._ws = _FakeWS()
        audio = np.zeros(2880, dtype=np.int16).tobytes()  # 1 trame + 960 de reste
        asyncio_run(_collect(svc.run_stt(audio)))
        assert len([m for m in svc._ws.sent if m.get("type") == "Audio"]) == 1
        assert svc._resample_buf.size == 960

    def test_noop_without_connection(self, monkeypatch):
        svc, _ = _make_service(monkeypatch)
        svc._ws = None
        assert asyncio_run(_collect(svc.run_stt(b"\x00\x00"))) == [None]

    def test_send_failure_forces_reconnect(self, monkeypatch):
        _install_fake_msgpack(monkeypatch)
        svc, _ = _make_service(monkeypatch, sample_rate=24000)

        class _BadWS:
            async def send(self, data):
                raise RuntimeError("boom")

            async def close(self):
                pass

        svc._ws = _BadWS()
        audio = np.zeros(1920, dtype=np.int16).tobytes()
        asyncio_run(_collect(svc.run_stt(audio)))
        assert svc._ws is None  # nullifié -> la boucle de connexion reconnecte


class TestTurnFlush:
    def test_flush_sends_marker_and_requests_finalize(self, monkeypatch):
        _install_fake_msgpack(monkeypatch)
        svc, _ = _make_service(monkeypatch)
        svc._ws = _FakeWS()
        asyncio_run(svc._flush_turn())
        markers = [m for m in svc._ws.sent if m.get("type") == "Marker"]
        zeros = [m for m in svc._ws.sent if m.get("type") == "Audio"]
        assert len(markers) == 1 and markers[0]["id"] == 1
        assert len(zeros) == 10  # ~0,8 s de silence pour drainer le délai modèle
        assert svc._finalize_requested is True
        assert svc._pending_marker == 1

    def test_words_accumulate_then_finalize_on_marker(self, monkeypatch):
        from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame

        _install_fake_msgpack(monkeypatch)
        svc, captured = _make_service(monkeypatch)
        svc._ws = _FakeWS()
        asyncio_run(svc._flush_turn())  # arme le Marker en attente + request_finalize

        async def _drive():
            await svc._on_message({"type": "Word", "text": "Bonjour", "start_time": 0.1})
            await svc._on_message({"type": "Word", "text": "madame", "start_time": 0.5})
            await svc._on_message({"type": "EndWord", "stop_time": 0.9})
            await svc._on_message({"type": "Marker", "id": 1})

        asyncio_run(_drive())

        interims = [f for f in captured if isinstance(f, InterimTranscriptionFrame)]
        finals = [f for f in captured if isinstance(f, TranscriptionFrame)]
        assert [f.text for f in interims] == ["Bonjour", "Bonjour madame"]
        assert len(finals) == 1
        assert finals[0].text == "Bonjour madame"
        assert finals[0].finalized is True  # -> déclenche l'inférence immédiatement
        assert svc._words == []  # remis à zéro pour le tour suivant

    def test_mismatched_marker_id_ignored(self, monkeypatch):
        from pipecat.frames.frames import TranscriptionFrame

        svc, captured = _make_service(monkeypatch)
        svc._pending_marker = 2
        svc._words = ["salut"]
        asyncio_run(svc._on_message({"type": "Marker", "id": 1}))  # id != attendu
        assert not [f for f in captured if isinstance(f, TranscriptionFrame)]
        assert svc._pending_marker == 2  # toujours en attente du bon écho

    def test_empty_turn_not_pushed(self, monkeypatch):
        svc, captured = _make_service(monkeypatch)
        svc._pending_marker = 1
        svc._finalize_requested = True
        asyncio_run(svc._on_message({"type": "Marker", "id": 1}))
        assert captured == []  # aucun mot -> aucune frame poussée


class TestBuildSTT:
    def test_selects_kyutai(self, monkeypatch):
        from app.voice.bot import build_stt
        from app.voice.kyutai_stt import KyutaiSTTService

        monkeypatch.setenv("STT_PROVIDER", "kyutai")
        stt = build_stt(_tenant(), "fr")
        assert isinstance(stt, KyutaiSTTService)

    def test_selects_deepgram(self, monkeypatch):
        pytest.importorskip("deepgram")  # extra présent en CI, pas dans le venv minimal
        from app.voice.bot import build_stt

        monkeypatch.setenv("STT_PROVIDER", "deepgram")
        stt = build_stt(_tenant(), "fr")
        assert type(stt).__name__ == "DeepgramSTTService"

    def test_unknown_provider_raises(self, monkeypatch):
        from app.voice.bot import build_stt

        monkeypatch.setenv("STT_PROVIDER", "bogus")
        with pytest.raises(ValueError):
            build_stt(_tenant(), "fr")


# asyncio.run local (évite d'importer asyncio au niveau module juste pour ça).
def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
