"""Tests du client TTS moshi-server (voix Moshi 1.6B) et du sélecteur build_tts.

Tout est mocké : de faux modules `websockets` et `msgpack` sont injectés, aucune
connexion n'est ouverte. Le serveur Rust tourne sur Modal ; ici on vérifie le
protocole (un message par mot puis Eos), la musique d'attente pendant un démarrage à
froid, la pré-connexion, et qu'une panne réseau devient une ErrorFrame plutôt qu'un
appel qui explose. Exécuter depuis api/ avec : pytest tests/ -v
"""
import asyncio

import pytest

from app.voice import bot


async def _collect(gen):
    return [frame async for frame in gen]


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
    def test_moshi_server_selected(self, monkeypatch):
        # Le service s'instancie sans websockets/msgpack (imports paresseux) : la
        # connexion n'a lieu qu'à la première phrase.
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        from app.voice.moshi_server_tts import MoshiServerTTSService

        assert isinstance(bot.build_tts(), MoshiServerTTSService)

    def test_sans_url_leve(self, monkeypatch):
        """Pas de serveur de voix, pas de pipeline : une erreur franche au démarrage de
        l'appel vaut mieux qu'un appel muet — et la supervision l'annonce déjà."""
        monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
        with pytest.raises(ValueError, match="MOSHI_TTS_URL"):
            bot.build_tts()
